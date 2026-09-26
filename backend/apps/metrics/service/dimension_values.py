"""Known dimension values for governed metric planning.

The planner can only filter correctly when it knows how a dimension is stored
("east" vs "东部"). Values are sampled from the datasource once per published
version (after publish, on an administrator's refresh, or lazily the first time
a version is planned) and kept in ``metric_dimension_value``. Administrators may
replace a dimension's list with labelled values, which sampling never overwrites.

Sampling is system-level and ignores row permissions, so values are only shown to
the planner for users without row rules on the metric table.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlmodel import Session, select

from apps.datasource.models.datasource import CoreDatasource, CoreTable
from apps.metrics.models.metric import (
    MetricDefinition,
    MetricDimensionValue,
    MetricVersion,
)
from apps.metrics.service.query_planner import (
    _field_catalog,
    _row_permission_filters,
    compile_dimension_sample,
)
from common.core.config import settings
from common.core.db import engine

MAX_VALUE_LENGTH = 64
LAZY_RETRY_SECONDS = 3600
_log = logging.getLogger("adaptive.metrics")
_inflight: set[int] = set()
_last_attempt: dict[int, float] = {}
_state_lock = threading.Lock()


def _value_limit() -> int:
    return max(1, min(settings.GRAPH_DIMENSION_VALUE_LIMIT, 200))


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value else None  # drop NaN
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _default_execute(datasource: CoreDatasource, sql: str) -> list[Any]:
    # Lazy import: the execution stack loads drivers and the optional xpack package.
    import sqlbot_xpack  # noqa: F401

    from apps.db.db import exec_sql

    raw = exec_sql(datasource, sql)
    fields = raw.get("fields") or []
    if not fields:
        return []
    return [row.get(fields[0]) for row in raw.get("data") or []]


def _upsert(session: Session, existing: Optional[MetricDimensionValue], **values) -> MetricDimensionValue:
    row = existing or MetricDimensionValue(
        metric_version_id=values["metric_version_id"], dimension=values["dimension"]
    )
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_at = datetime.now()
    session.add(row)
    return row


def sample_version_dimension_values(
    session: Session,
    version_id: int,
    user_id: Optional[int] = None,
    execute: Optional[Callable[[CoreDatasource, str], list[Any]]] = None,
) -> list[MetricDimensionValue]:
    """Sample every declared dimension of one version; the caller commits."""
    execute = execute or _default_execute
    # Imported here so the pure compiler module stays free of execution imports;
    # xpack must load before apps.db.db (legacy import order, see main.py).
    import sqlbot_xpack  # noqa: F401

    from apps.db.db import check_sql_read

    version = session.get(MetricVersion, version_id)
    if version is None:
        return []
    metric = session.get(MetricDefinition, version.metric_id)
    datasource = session.get(CoreDatasource, metric.datasource_id) if metric else None
    if metric is None or datasource is None:
        return []
    catalog = _field_catalog(session, metric.datasource_id, version.required_tables)
    existing = {
        row.dimension: row
        for row in session.exec(
            select(MetricDimensionValue).where(MetricDimensionValue.metric_version_id == version_id)
        ).all()
    }
    limit = _value_limit()
    results = []
    for dimension in version.dimensions:
        current = existing.get(dimension)
        if current is not None and current.source == "manual":
            results.append(current)
            continue
        status, values = "failed", []
        try:
            sql = compile_dimension_sample(version, datasource, catalog, dimension, limit + 1)
            safe, _reason = check_sql_read(sql, datasource)
            if not safe:
                raise ValueError("dimension_sample_not_read_only")
            raw_values = execute(datasource, sql)
            if len(raw_values) > limit:
                status = "high_cardinality"
            else:
                status = "ok"
                values = [
                    {"value": scalar, "label": None}
                    for scalar in (_json_scalar(item) for item in raw_values)
                    if scalar is not None and len(str(scalar)) <= MAX_VALUE_LENGTH
                ]
        except Exception:
            _log.warning("metric_dimension_sampling_failed version=%s dimension=%s",
                         version_id, dimension, exc_info=True)
        results.append(_upsert(
            session, current, metric_version_id=version_id, dimension=dimension,
            values=values, status=status, source="sampled", updated_by=user_id,
        ))
    session.flush()
    return results


def _run_in_background(version_id: int, user_id: Optional[int]) -> None:
    try:
        with Session(engine) as session:
            sample_version_dimension_values(session, version_id, user_id)
            session.commit()
    except Exception:
        _log.warning("metric_dimension_sampling_crashed version=%s", version_id, exc_info=True)
    finally:
        with _state_lock:
            _inflight.discard(version_id)


def auto_sampling_enabled() -> bool:
    return settings.GRAPH_EXPERIMENT_ENABLED and settings.GRAPH_DIMENSION_SAMPLING_ENABLED


def schedule_dimension_sampling(version_id: int, user_id: Optional[int] = None,
                                lazy: bool = False) -> bool:
    """Queue sampling off the request path; lazy triggers retry at most hourly."""
    if not auto_sampling_enabled():
        return False
    now = time.monotonic()
    with _state_lock:
        if version_id in _inflight:
            return False
        if lazy and now - _last_attempt.get(version_id, float("-inf")) < LAZY_RETRY_SECONDS:
            return False
        _inflight.add(version_id)
        _last_attempt[version_id] = now
    from common.utils.embedding_threads import executor

    executor.submit(_run_in_background, version_id, user_id)
    return True


def _entry(item: dict[str, Any]) -> Any:
    return {"value": item["value"], "label": item["label"]} if item.get("label") else item["value"]


def planning_dimension_values(
    session: Session,
    candidates: list[dict[str, Any]],
    current_user: Any,
    datasource_id: int,
) -> dict[int, dict[str, list[Any]]]:
    """Values per candidate version for the planning prompt.

    Candidates on tables where the user has row rules get none. Versions that were
    never sampled are queued for background sampling and answered without values.
    """
    if not candidates:
        return {}
    datasource = session.get(CoreDatasource, datasource_id)
    if datasource is None:
        return {}
    wanted = {name.casefold() for candidate in candidates for name in candidate["required_tables"]}
    tables = [
        table.table_name
        for table in session.exec(
            select(CoreTable).where(CoreTable.ds_id == datasource_id, CoreTable.checked.is_(True))
        ).all()
        if table.table_name.casefold() in wanted
    ]
    restricted = {
        item["table"].casefold()
        for item in _row_permission_filters(session, current_user, datasource, tables)
    }
    version_ids = [int(candidate["metric_version_id"]) for candidate in candidates]
    rows = session.exec(
        select(MetricDimensionValue).where(MetricDimensionValue.metric_version_id.in_(version_ids))
    ).all()
    by_version: dict[int, dict[str, list[Any]]] = {}
    seen_versions = set()
    for row in rows:
        seen_versions.add(int(row.metric_version_id))
        if row.status == "ok" and row.values:
            by_version.setdefault(int(row.metric_version_id), {})[row.dimension] = [
                _entry(item) for item in row.values
            ]
    result = {}
    for candidate in candidates:
        version_id = int(candidate["metric_version_id"])
        if version_id not in seen_versions:
            schedule_dimension_sampling(version_id, lazy=True)
        if any(name.casefold() in restricted for name in candidate["required_tables"]):
            continue
        if by_version.get(version_id):
            result[version_id] = by_version[version_id]
    return result


# --- Administrator operations -------------------------------------------------

def _version_in_workspace(session: Session, metric_id: int, version_id: int,
                          oid: Optional[int]) -> MetricVersion:
    from fastapi import HTTPException

    from apps.metrics.crud.metric import _get_definition, _oid

    metric = _get_definition(session, metric_id, _oid(oid))
    version = session.get(MetricVersion, version_id)
    if version is None or version.metric_id != metric.id:
        raise HTTPException(404, "Metric version was not found")
    return version


def _declared_dimension(version: MetricVersion, dimension: str) -> str:
    from fastapi import HTTPException

    match = next((name for name in version.dimensions if name.casefold() == dimension.casefold()), None)
    if match is None:
        raise HTTPException(422, f"Dimension '{dimension}' is not declared by metric version {version.version}")
    return match


def _read(row: MetricDimensionValue) -> dict[str, Any]:
    return {"dimension": row.dimension, "values": list(row.values), "status": row.status,
            "source": row.source, "updated_at": row.updated_at}


def _rows(session: Session, version_id: int) -> list[MetricDimensionValue]:
    return list(session.exec(
        select(MetricDimensionValue)
        .where(MetricDimensionValue.metric_version_id == version_id)
        .order_by(MetricDimensionValue.dimension)
    ).all())


def list_dimension_values(session: Session, metric_id: int, version_id: int,
                          oid: Optional[int]) -> list[dict[str, Any]]:
    _version_in_workspace(session, metric_id, version_id, oid)
    return [_read(row) for row in _rows(session, version_id)]


def set_manual_dimension_values(session: Session, metric_id: int, version_id: int, dimension: str,
                                values: list[dict[str, Any]], oid: Optional[int],
                                user_id: int) -> dict[str, Any]:
    version = _version_in_workspace(session, metric_id, version_id, oid)
    dimension = _declared_dimension(version, dimension)
    existing = next((row for row in _rows(session, version_id) if row.dimension == dimension), None)
    row = _upsert(session, existing, metric_version_id=version_id, dimension=dimension,
                  values=values, status="ok", source="manual", updated_by=user_id)
    session.flush()
    return _read(row)


def clear_manual_dimension_values(session: Session, metric_id: int, version_id: int, dimension: str,
                                  oid: Optional[int]) -> None:
    """Drop an administrator list; the dimension is sampled again on next refresh or use."""
    version = _version_in_workspace(session, metric_id, version_id, oid)
    dimension = _declared_dimension(version, dimension)
    for row in _rows(session, version_id):
        if row.dimension == dimension and row.source == "manual":
            session.delete(row)
    session.flush()


def _refresh_in_own_session(version_id: int, user_id: int) -> None:
    with Session(engine) as session:
        sample_version_dimension_values(session, version_id, user_id)
        session.commit()


async def refresh_dimension_values(session: Session, metric_id: int, version_id: int,
                                   oid: Optional[int], user_id: int) -> list[dict[str, Any]]:
    """Sample now (off the event loop); manual lists are kept."""
    import asyncio

    _version_in_workspace(session, metric_id, version_id, oid)
    await asyncio.to_thread(_refresh_in_own_session, version_id, user_id)
    session.expire_all()
    return [_read(row) for row in _rows(session, version_id)]

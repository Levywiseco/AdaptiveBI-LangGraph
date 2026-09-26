from __future__ import annotations

import json
from datetime import datetime
from html import escape
from typing import Any, Optional

import sqlglot
from fastapi import HTTPException
from sqlalchemy import Text, and_, cast, func, or_, update
from sqlglot import exp
from sqlmodel import Session, select

from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable
from apps.metrics.models.metric import MetricDefinition, MetricVersion
from apps.metrics.schemas.metric import (
    MetricCreate,
    MetricDefinitionUpdate,
    MetricVersionCreate,
)

BLOCKED_EXPRESSION_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
)

SQLGLOT_DIALECTS = {
    "pg": "postgres",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlserver": "tsql",
    "oracle": "oracle",
    "dm": "oracle",
    "ck": "clickhouse",
    "clickhouse": "clickhouse",
    "sqlite": "sqlite",
    "hive": "hive",
    "redshift": "redshift",
    "doris": "mysql",
    "starrocks": "mysql",
}


def _now() -> datetime:
    return datetime.now()


def _oid(value: Optional[int]) -> int:
    return int(value or 1)


def _http_error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def validate_metric_expression(expression: str, datasource_type: Optional[str] = None) -> exp.Expression:
    """Parse a metric expression as one SELECT projection and reject statement injection."""

    value = expression.strip()
    if not value:
        raise _http_error(422, "Metric expression cannot be empty")
    if ";" in value or "--" in value or "/*" in value or "*/" in value:
        raise _http_error(422, "Metric expression cannot contain statement separators or SQL comments")

    dialect = SQLGLOT_DIALECTS.get(datasource_type.casefold()) if datasource_type else None
    try:
        parsed = sqlglot.parse_one(f"SELECT {value}", read=dialect)
    except Exception as exc:
        raise _http_error(422, f"Metric expression is not valid SQL: {exc}")

    if not isinstance(parsed, exp.Select):
        raise _http_error(422, "Metric expression must be a SQL scalar expression")
    disallowed_clauses = {
        "from_": "FROM",
        "where": "WHERE",
        "group": "GROUP BY",
        "having": "HAVING",
        "order": "ORDER BY",
        "limit": "LIMIT",
        "offset": "OFFSET",
        "joins": "JOIN",
        "with_": "WITH",
    }
    used_clauses = [label for key, label in disallowed_clauses.items() if parsed.args.get(key)]
    if used_clauses:
        raise _http_error(
            422,
            f"Metric expression must not contain query clauses: {', '.join(used_clauses)}",
        )
    if any(parsed.find(node_type) is not None for node_type in BLOCKED_EXPRESSION_NODES):
        raise _http_error(422, "Metric expression contains a forbidden SQL operation")
    if len(list(parsed.find_all(exp.Select))) != 1:
        raise _http_error(422, "Metric expression cannot contain a subquery")
    return parsed


def _get_datasource(session: Session, datasource_id: int, oid: int) -> CoreDatasource:
    datasource = session.exec(
        select(CoreDatasource).where(
            and_(CoreDatasource.id == datasource_id, CoreDatasource.oid == oid)
        )
    ).first()
    if not datasource:
        raise _http_error(404, "Datasource was not found in the current workspace")
    return datasource


def _get_definition(
    session: Session,
    metric_id: int,
    oid: int,
    *,
    include_archived: bool = False,
) -> MetricDefinition:
    statement = select(MetricDefinition).where(
        and_(MetricDefinition.id == metric_id, MetricDefinition.oid == oid)
    )
    if not include_archived:
        statement = statement.where(MetricDefinition.status != "archived")
    metric = session.exec(statement).first()
    if not metric:
        raise _http_error(404, "Metric was not found in the current workspace")
    return metric


def _get_versions(session: Session, metric_id: int) -> list[MetricVersion]:
    return list(
        session.exec(
            select(MetricVersion)
            .where(MetricVersion.metric_id == metric_id)
            .order_by(MetricVersion.version.desc())
        ).all()
    )


def _version_to_dict(version: Optional[MetricVersion]) -> Optional[dict[str, Any]]:
    return version.model_dump() if version else None


def _serialize_metric(
    session: Session,
    metric: MetricDefinition,
    *,
    include_versions: bool = False,
) -> dict[str, Any]:
    versions = _get_versions(session, int(metric.id))
    latest = versions[0] if versions else None
    current = next((item for item in versions if item.id == metric.current_version_id), None)
    datasource = session.get(CoreDatasource, metric.datasource_id)
    result = metric.model_dump()
    result.update(
        datasource_name=datasource.name if datasource else None,
        current_version=_version_to_dict(current),
        latest_version=_version_to_dict(latest),
        has_draft=any(item.status == "draft" for item in versions),
    )
    if include_versions:
        result["versions"] = [_version_to_dict(item) for item in versions]
    return result


def _ensure_unique_code(
    session: Session,
    oid: int,
    code: str,
    *,
    exclude_metric_id: Optional[int] = None,
) -> None:
    statement = select(MetricDefinition.id).where(
        and_(MetricDefinition.oid == oid, MetricDefinition.code == code)
    )
    if exclude_metric_id is not None:
        statement = statement.where(MetricDefinition.id != exclude_metric_id)
    if session.exec(statement).first() is not None:
        raise _http_error(409, f"Metric code '{code}' already exists in this workspace")


def _new_version(
    metric_id: int,
    version_number: int,
    payload: dict[str, Any],
    user_id: int,
) -> MetricVersion:
    return MetricVersion(
        metric_id=metric_id,
        version=version_number,
        expression=payload["expression"],
        aggregation=payload.get("aggregation") or "SUM",
        time_field=payload.get("time_field"),
        grain=payload.get("grain"),
        required_tables=payload.get("required_tables") or [],
        dimensions=payload.get("dimensions") or [],
        filters=payload.get("filters") or [],
        join_rules=payload.get("join_rules") or [],
        unit=payload.get("unit"),
        effective_from=payload.get("effective_from"),
        effective_to=payload.get("effective_to"),
        created_by=user_id,
        created_at=_now(),
        status="draft",
        validation_status="syntax_valid",
        validation_message="SQL expression syntax passed; business result review is still required",
    )


def create_metric(
    session: Session,
    payload: MetricCreate,
    oid: Optional[int],
    user_id: int,
) -> dict[str, Any]:
    workspace_id = _oid(oid)
    datasource = _get_datasource(session, payload.datasource_id, workspace_id)
    _ensure_unique_code(session, workspace_id, payload.code)
    validate_metric_expression(payload.expression, datasource.type)

    metric = MetricDefinition(
        oid=workspace_id,
        code=payload.code,
        name=payload.name,
        aliases=payload.aliases,
        description=payload.description,
        datasource_id=payload.datasource_id,
        owner_user_id=user_id,
        status="draft",
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(metric)
    session.flush()
    session.refresh(metric)

    version = _new_version(int(metric.id), 1, payload.model_dump(), user_id)
    session.add(version)
    session.flush()
    session.refresh(version)
    return _serialize_metric(session, metric, include_versions=True)


def list_metrics(
    session: Session,
    oid: Optional[int],
    current_page: int,
    page_size: int,
    keyword: Optional[str] = None,
    status: Optional[str] = None,
    datasource_id: Optional[int] = None,
) -> dict[str, Any]:
    page = max(current_page, 1)
    size = min(max(page_size, 1), 100)
    conditions = [MetricDefinition.oid == _oid(oid)]
    if status:
        conditions.append(MetricDefinition.status == status)
    else:
        conditions.append(MetricDefinition.status != "archived")
    if datasource_id is not None:
        conditions.append(MetricDefinition.datasource_id == datasource_id)
    if keyword and keyword.strip():
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                MetricDefinition.name.ilike(pattern),
                MetricDefinition.code.ilike(pattern),
                cast(MetricDefinition.aliases, Text).ilike(pattern),
            )
        )

    count_statement = select(func.count()).select_from(MetricDefinition).where(and_(*conditions))
    total = int(session.execute(count_statement).scalar_one())
    statement = (
        select(MetricDefinition)
        .where(and_(*conditions))
        .order_by(MetricDefinition.updated_at.desc(), MetricDefinition.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    items = [_serialize_metric(session, metric) for metric in session.exec(statement).all()]
    return {
        "current_page": page,
        "page_size": size,
        "total_count": total,
        "total_pages": (total + size - 1) // size,
        "data": items,
    }


def get_metric(session: Session, metric_id: int, oid: Optional[int]) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid), include_archived=True)
    return _serialize_metric(session, metric, include_versions=True)


VERSION_FIELDS = {
    "expression",
    "aggregation",
    "time_field",
    "grain",
    "required_tables",
    "dimensions",
    "filters",
    "join_rules",
    "unit",
    "effective_from",
    "effective_to",
}


def create_metric_version(
    session: Session,
    metric_id: int,
    payload: MetricVersionCreate,
    oid: Optional[int],
    user_id: int,
) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid))
    datasource = _get_datasource(session, metric.datasource_id, metric.oid)
    versions = _get_versions(session, metric_id)
    latest = versions[0] if versions else None
    provided = payload.model_dump(exclude_unset=True)

    if latest and latest.status == "draft":
        merged = latest.model_dump()
        merged.update(provided)
        validate_metric_expression(merged["expression"], datasource.type)
        if merged.get("effective_from") and merged.get("effective_to"):
            if merged["effective_to"] <= merged["effective_from"]:
                raise _http_error(422, "effective_to must be later than effective_from")
        for field in VERSION_FIELDS:
            if field in provided:
                setattr(latest, field, provided[field])
        latest.validation_status = "syntax_valid"
        latest.validation_message = "SQL expression syntax passed; business result review is still required"
        session.add(latest)
    else:
        if not latest and not provided.get("expression"):
            raise _http_error(422, "expression is required for the first metric version")
        merged = latest.model_dump() if latest else {}
        merged.update(provided)
        validate_metric_expression(merged["expression"], datasource.type)
        if merged.get("effective_from") and merged.get("effective_to"):
            if merged["effective_to"] <= merged["effective_from"]:
                raise _http_error(422, "effective_to must be later than effective_from")
        next_number = (latest.version + 1) if latest else 1
        session.add(_new_version(metric_id, next_number, merged, user_id))

    metric.updated_at = _now()
    session.add(metric)
    session.flush()
    session.refresh(metric)
    return _serialize_metric(session, metric, include_versions=True)


def update_metric(
    session: Session,
    metric_id: int,
    payload: MetricDefinitionUpdate,
    oid: Optional[int],
    user_id: int,
) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid))
    values = payload.model_dump(exclude_unset=True, exclude={"calculation"})
    if "code" in values:
        _ensure_unique_code(session, metric.oid, values["code"], exclude_metric_id=metric_id)
    if "datasource_id" in values:
        _get_datasource(session, values["datasource_id"], metric.oid)
        if metric.current_version_id and values["datasource_id"] != metric.datasource_id:
            raise _http_error(409, "A published metric cannot be moved to another datasource")
    for field, value in values.items():
        setattr(metric, field, value)
    metric.updated_at = _now()
    session.add(metric)
    session.flush()

    if payload.calculation is not None:
        return create_metric_version(session, metric_id, payload.calculation, metric.oid, user_id)
    return _serialize_metric(session, metric, include_versions=True)


def _split_field_reference(value: str) -> tuple[Optional[str], str]:
    cleaned = value.strip().strip('"`[]')
    parts = [part.strip('"`[]') for part in cleaned.split(".") if part]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (None, parts[-1])


STRUCTURED_FIELD_KEYS = {
    "field",
    "column",
    "left_field",
    "right_field",
    "source_field",
    "target_field",
}


def _structured_field_references(value: Any) -> list[tuple[Optional[str], str]]:
    references: list[tuple[Optional[str], str]] = []
    if isinstance(value, list):
        for item in value:
            references.extend(_structured_field_references(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in STRUCTURED_FIELD_KEYS and isinstance(item, str) and item.strip():
                references.append(_split_field_reference(item))
            elif isinstance(item, (dict, list)):
                references.extend(_structured_field_references(item))
    return references


def _version_references(
    version: MetricVersion,
    parsed: exp.Expression,
) -> list[tuple[Optional[str], str]]:
    references: list[tuple[Optional[str], str]] = [
        (column.table or None, column.name) for column in parsed.find_all(exp.Column)
    ]
    references.extend(
        _split_field_reference(value)
        for value in [*version.dimensions, *([version.time_field] if version.time_field else [])]
    )
    references.extend(_structured_field_references(version.filters))
    references.extend(_structured_field_references(version.join_rules))
    return references


def _validate_schema_references(
    session: Session,
    metric: MetricDefinition,
    version: MetricVersion,
    parsed: exp.Expression,
) -> None:
    if not version.required_tables:
        raise _http_error(422, "At least one required table is needed before publishing")

    table_rows = list(
        session.exec(
            select(CoreTable).where(
                and_(CoreTable.ds_id == metric.datasource_id, CoreTable.checked.is_(True))
            )
        ).all()
    )
    table_by_name = {table.table_name.casefold(): table for table in table_rows}
    required_names = {name.casefold() for name in version.required_tables}
    missing_tables = sorted(required_names - set(table_by_name))
    if missing_tables:
        raise _http_error(422, f"Unknown or disabled required tables: {', '.join(missing_tables)}")

    required_rows = [table_by_name[name] for name in required_names]
    fields = list(
        session.exec(
            select(CoreField).where(
                and_(
                    CoreField.table_id.in_([table.id for table in required_rows]),
                    CoreField.checked.is_(True),
                )
            )
        ).all()
    )
    table_name_by_id = {table.id: table.table_name.casefold() for table in required_rows}
    fields_by_table: dict[str, set[str]] = {name: set() for name in required_names}
    for field in fields:
        table_name = table_name_by_id.get(field.table_id)
        if table_name:
            fields_by_table[table_name].add(field.field_name.casefold())

    references = _version_references(version, parsed)

    for table_name, field_name in references:
        field_key = field_name.casefold()
        if table_name:
            table_key = table_name.casefold()
            if table_key not in required_names:
                raise _http_error(422, f"Field '{table_name}.{field_name}' uses a table outside required_tables")
            if field_key not in fields_by_table.get(table_key, set()):
                raise _http_error(422, f"Unknown or disabled field '{table_name}.{field_name}'")
            continue
        matching_tables = [name for name, names in fields_by_table.items() if field_key in names]
        if not matching_tables:
            raise _http_error(422, f"Unknown or disabled field '{field_name}'")
        if len(matching_tables) > 1:
            raise _http_error(422, f"Ambiguous field '{field_name}'; qualify it with the table name")


def publish_metric_version(
    session: Session,
    metric_id: int,
    version_id: int,
    review_note: str,
    oid: Optional[int],
    user_id: int,
) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid))
    versions = _get_versions(session, metric_id)
    version = next((item for item in versions if item.id == version_id), None)
    if not version:
        raise _http_error(404, "Metric version was not found")
    if version.status != "draft":
        raise _http_error(409, "Only a draft metric version can be published")
    if versions and versions[0].id != version.id:
        raise _http_error(409, "Only the latest metric version can be published")

    datasource = _get_datasource(session, metric.datasource_id, metric.oid)
    parsed = validate_metric_expression(version.expression, datasource.type)
    _validate_schema_references(session, metric, version, parsed)

    now = _now()
    session.exec(
        update(MetricVersion)
        .where(
            and_(
                MetricVersion.metric_id == metric_id,
                MetricVersion.status == "published",
            )
        )
        .values(status="superseded")
    )
    version.status = "published"
    version.validation_status = "approved"
    version.validation_message = "Schema and expression checks passed; published by a workspace administrator"
    version.review_note = review_note
    version.reviewed_by = user_id
    version.reviewed_at = now
    version.published_at = now
    if version.effective_from is None:
        version.effective_from = now
    session.add(version)

    metric.current_version_id = version.id
    metric.status = "published"
    metric.updated_at = now
    session.add(metric)
    session.flush()
    session.refresh(metric)
    return _serialize_metric(session, metric, include_versions=True)


def archive_metric(session: Session, metric_id: int, oid: Optional[int]) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid))
    metric.status = "archived"
    metric.updated_at = _now()
    session.add(metric)
    session.flush()
    return _serialize_metric(session, metric, include_versions=True)


def restore_metric(session: Session, metric_id: int, oid: Optional[int]) -> dict[str, Any]:
    metric = _get_definition(session, metric_id, _oid(oid), include_archived=True)
    metric.status = "published" if metric.current_version_id else "draft"
    metric.updated_at = _now()
    session.add(metric)
    session.flush()
    return _serialize_metric(session, metric, include_versions=True)


def _bigrams(text: str) -> set[str]:
    chars = [char for char in text.casefold() if not char.isspace()]
    return {left + right for left, right in zip(chars, chars[1:])}


def _match_score(question: str, metric: MetricDefinition, fuzzy: bool = False) -> int:
    """Lexical relevance. ``fuzzy`` adds Chinese character-bigram overlap, which
    whitespace tokens cannot provide (e.g. 下单客户数 vs 下单的客户有多少)."""
    normalized_question = question.casefold().strip()
    question_grams = _bigrams(normalized_question) if fuzzy else set()
    score = 0
    for position, term in enumerate([metric.name, metric.code, *metric.aliases]):
        normalized_term = term.casefold().strip()
        if not normalized_term:
            continue
        if normalized_question == normalized_term:
            candidate = 1000 - position
        elif normalized_term in normalized_question:
            candidate = 500 + min(len(normalized_term), 100) - position
        elif normalized_question in normalized_term and len(normalized_question) >= 2:
            candidate = 100 + len(normalized_question) - position
        else:
            question_tokens = set(normalized_question.replace("_", " ").split())
            term_tokens = set(normalized_term.replace("_", " ").split())
            candidate = 25 * len(question_tokens & term_tokens) - position
            term_grams = _bigrams(normalized_term) if fuzzy else set()
            if term_grams:
                coverage = len(term_grams & question_grams) / len(term_grams)
                if coverage >= 0.5:
                    candidate = max(candidate, 50 + round(40 * coverage) - position)
        score = max(score, candidate)
    return score


def _visible_fields_for_user(
    session: Session,
    datasource_id: int,
    current_user: Any,
) -> Optional[dict[str, set[str]]]:
    if current_user is None:
        return None
    if getattr(current_user, "id", None) == 1:
        tables = session.exec(
            select(CoreTable).where(
                and_(CoreTable.ds_id == datasource_id, CoreTable.checked.is_(True))
            )
        ).all()
        table_names = {table.id: table.table_name.casefold() for table in tables}
        result = {name: set() for name in table_names.values()}
        if table_names:
            fields = session.exec(
                select(CoreField).where(
                    and_(
                        CoreField.table_id.in_(list(table_names)),
                        CoreField.checked.is_(True),
                    )
                )
            ).all()
            for field in fields:
                table_name = table_names.get(field.table_id)
                if table_name:
                    result[table_name].add(field.field_name.casefold())
        return result
    # Keep this import lazy while the optional xpack package wires permission modules.
    from apps.datasource.crud.datasource import get_table_obj_by_ds

    datasource = session.get(CoreDatasource, datasource_id)
    if not datasource:
        return {}
    return {
        item.table.table_name.casefold(): {
            field.field_name.casefold() for field in (item.fields or [])
        }
        for item in get_table_obj_by_ds(session, current_user, datasource)
    }


def _metric_version_visible(
    version: MetricVersion,
    visible_fields: Optional[dict[str, set[str]]],
    datasource_type: Optional[str],
) -> bool:
    if visible_fields is None:
        return True
    required_names = {name.casefold() for name in version.required_tables}
    if not required_names.issubset(set(visible_fields)):
        return False
    try:
        parsed = validate_metric_expression(version.expression, datasource_type)
    except HTTPException:
        return False
    for table_name, field_name in _version_references(version, parsed):
        field_key = field_name.casefold()
        if table_name:
            if field_key not in visible_fields.get(table_name.casefold(), set()):
                return False
        elif not any(field_key in visible_fields.get(name, set()) for name in required_names):
            return False
    return True


def _rank_published_metrics(
    session: Session,
    question: str,
    oid: Optional[int],
    datasource_id: Optional[int],
    limit: int,
    current_user: Any,
    hybrid: bool = False,
) -> list[tuple[int, MetricDefinition, MetricVersion]]:
    """Rank visible published metrics. ``hybrid`` (governed planning only) adds
    character-bigram and embedding recall; the legacy SQL prompt keeps its behavior."""
    if not datasource_id or not question.strip():
        return []
    now = _now()
    rows = session.exec(
        select(MetricDefinition, MetricVersion)
        .join(MetricVersion, MetricDefinition.current_version_id == MetricVersion.id)
        .where(
            and_(
                MetricDefinition.oid == _oid(oid),
                MetricDefinition.datasource_id == datasource_id,
                MetricDefinition.status == "published",
                MetricVersion.status == "published",
                or_(MetricVersion.effective_from.is_(None), MetricVersion.effective_from <= now),
                or_(MetricVersion.effective_to.is_(None), MetricVersion.effective_to > now),
            )
        )
    ).all()
    datasource = session.get(CoreDatasource, datasource_id)
    visible_fields = _visible_fields_for_user(session, datasource_id, current_user)
    visible = [
        (metric, version)
        for metric, version in rows
        if _metric_version_visible(
            version,
            visible_fields,
            datasource.type if datasource else None,
        )
    ]
    similarities: dict[int, float] = {}
    if hybrid:
        from apps.metrics.service.recall import vector_score, vector_similarities

        similarities = {
            version_id: vector_score(similarity)
            for version_id, similarity in vector_similarities(question, visible).items()
        }
    return sorted(
        (
            (score, metric, version)
            for metric, version in visible
            if (score := max(
                _match_score(question, metric, fuzzy=hybrid),
                similarities.get(int(version.id), 0),
            )) > 0
        ),
        key=lambda item: (-item[0], item[1].code),
    )[:min(max(limit, 1), 20)]


def get_metric_candidates(
    session: Session,
    question: str,
    oid: Optional[int],
    datasource_id: Optional[int],
    limit: int = 5,
    current_user: Any = None,
    hybrid: bool = True,
) -> list[dict[str, Any]]:
    """Return safe structured candidates for governed model planning.

    ``hybrid=False`` reproduces the legacy exact-match recall (evaluation baseline).
    """
    ranked = _rank_published_metrics(
        session, question, oid, datasource_id, limit, current_user, hybrid=hybrid
    )
    return [
        {
            "metric_id": int(metric.id),
            "metric_code": metric.code,
            "metric_name": metric.name,
            "aliases": list(metric.aliases),
            "description": metric.description,
            "metric_version_id": int(version.id),
            "metric_version": version.version,
            "dimensions": list(version.dimensions),
            "time_field": version.time_field,
            "grain": version.grain,
            "unit": version.unit,
            "required_tables": list(version.required_tables),
            "score": score,
        }
        for score, metric, version in ranked
    ]


def _render_metric_prompt(
    ranked: list[tuple[int, MetricDefinition, MetricVersion]],
    *,
    inherited: bool = False,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    if not ranked:
        return "", [], []
    matched: list[dict[str, Any]] = []
    table_names: list[str] = []
    parts = [
        '<metric-catalog authority="published-business-definition">',
        "Use a matching metric exactly as defined. Do not silently rewrite its expression, time field, filters, grain, or joins.",
        "If the question conflicts with a published definition, explain the conflict or ask for clarification.",
    ]
    if inherited:
        parts.append("The metric below is inherited from structured state in this same chat because the current message is a short follow-up.")
    for score, metric, version in ranked:
        matched.append(
            {
                "id": metric.id,
                "code": metric.code,
                "name": metric.name,
                "words": [metric.name, *metric.aliases],
                "description": metric.description,
                "version_id": version.id,
                "version": version.version,
                "score": score,
                "required_tables": version.required_tables,
                "inherited": inherited,
            }
        )
        table_names.extend(version.required_tables)
        parts.extend(
            [
                f'<metric id="{metric.id}" code="{escape(metric.code)}" version="{version.version}">',
                f"<name>{escape(metric.name)}</name>",
                f"<aliases>{escape(', '.join(metric.aliases))}</aliases>",
                f"<description>{escape(metric.description or '')}</description>",
                f"<aggregation>{escape(version.aggregation)}</aggregation>",
                f"<expression>{escape(version.expression)}</expression>",
                f"<required-tables>{escape(', '.join(version.required_tables))}</required-tables>",
                f"<dimensions>{escape(', '.join(version.dimensions))}</dimensions>",
                f"<time-field>{escape(version.time_field or '')}</time-field>",
                f"<grain>{escape(version.grain or '')}</grain>",
                f"<filters>{escape(json.dumps(version.filters, ensure_ascii=False))}</filters>",
                f"<join-rules>{escape(json.dumps(version.join_rules, ensure_ascii=False))}</join-rules>",
                f"<unit>{escape(version.unit or '')}</unit>",
                "</metric>",
            ]
        )
    parts.append("</metric-catalog>")
    return "\n".join(parts), matched, list(dict.fromkeys(table_names))


def get_metric_prompt(
    session: Session,
    question: str,
    oid: Optional[int],
    datasource_id: Optional[int],
    limit: int = 5,
    current_user: Any = None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Return matching, effective, published metric versions for SQL generation."""

    ranked = _rank_published_metrics(
        session, question, oid, datasource_id, limit, current_user
    )
    return _render_metric_prompt(ranked)


def get_metric_prompt_by_refs(
    session: Session,
    refs: list[dict[str, Any]],
    oid: Optional[int],
    datasource_id: Optional[int],
    *,
    current_user: Any = None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Resolve exact metric versions saved in structured state for a short follow-up."""

    if not refs or not datasource_id:
        return "", [], []
    datasource = session.get(CoreDatasource, datasource_id)
    visible_fields = _visible_fields_for_user(session, datasource_id, current_user)
    ranked: list[tuple[int, MetricDefinition, MetricVersion]] = []
    for position, ref in enumerate(refs[:5]):
        metric_id = ref.get("id")
        version_id = ref.get("version_id")
        if not metric_id or not version_id:
            continue
        row = session.exec(
            select(MetricDefinition, MetricVersion)
            .join(MetricVersion, MetricVersion.metric_id == MetricDefinition.id)
            .where(
                and_(
                    MetricDefinition.id == metric_id,
                    MetricDefinition.oid == _oid(oid),
                    MetricDefinition.datasource_id == datasource_id,
                    MetricDefinition.status == "published",
                    MetricVersion.id == version_id,
                    MetricVersion.status.in_(["published", "superseded"]),
                )
            )
        ).first()
        if not row:
            continue
        metric, version = row
        if _metric_version_visible(
            version,
            visible_fields,
            datasource.type if datasource else None,
        ):
            ranked.append((100 - position, metric, version))
    return _render_metric_prompt(ranked, inherited=True)

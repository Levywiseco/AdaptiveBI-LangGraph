from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import sqlglot
from fastapi import HTTPException
from sqlalchemy import and_
from sqlglot import exp
from sqlmodel import Session, select

from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable
from apps.metrics.crud.metric import (
    SQLGLOT_DIALECTS,
    _get_definition,
    _metric_version_visible,
    _split_field_reference,
    _visible_fields_for_user,
    validate_metric_expression,
)
from apps.metrics.models.metric import MetricDefinition, MetricVersion
from apps.metrics.schemas.metric import MetricQueryFilter, MetricQueryPlanRequest

COMPILER_VERSION = "metric-plan-v1"


def _error(message: str, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def _field_key(value: str) -> tuple[str | None, str]:
    table, field = _split_field_reference(value)
    return (table.casefold() if table else None, field.casefold())


def _field_catalog(
    session: Session,
    datasource_id: int,
    required_tables: list[str],
) -> dict[str, set[str]]:
    required_names = {name.casefold() for name in required_tables}
    available_tables = list(
        session.exec(
            select(CoreTable).where(
                and_(
                    CoreTable.ds_id == datasource_id,
                    CoreTable.checked.is_(True),
                )
            )
        ).all()
    )
    table_rows = [
        table for table in available_tables if table.table_name.casefold() in required_names
    ]
    table_name_by_id = {int(table.id): table.table_name for table in table_rows}
    catalog = {table.table_name: set() for table in table_rows}
    if not table_name_by_id:
        return catalog
    fields = session.exec(
        select(CoreField).where(
            and_(
                CoreField.table_id.in_(list(table_name_by_id)),
                CoreField.checked.is_(True),
            )
        )
    ).all()
    for field in fields:
        table_name = table_name_by_id.get(int(field.table_id))
        if table_name:
            catalog[table_name].add(field.field_name)
    return catalog


def _resolve_column(
    reference: str,
    catalog: dict[str, set[str]],
) -> exp.Column:
    table_name, field_name = _field_key(reference)
    if table_name:
        actual_table = next((name for name in catalog if name.casefold() == table_name), None)
        if actual_table is None:
            raise _error(f"Field '{reference}' uses a table outside required_tables")
        actual_field = next(
            (name for name in catalog[actual_table] if name.casefold() == field_name),
            None,
        )
        if actual_field is None:
            raise _error(f"Unknown or disabled field '{reference}'")
        return exp.column(actual_field, table=actual_table, quoted=True)

    matches = [
        (table, actual_field)
        for table, fields in catalog.items()
        if (
            actual_field := next(
                (name for name in fields if name.casefold() == field_name),
                None,
            )
        )
        is not None
    ]
    if not matches:
        raise _error(f"Unknown or disabled field '{reference}'")
    if len(matches) > 1:
        raise _error(f"Ambiguous field '{reference}'; qualify it with the table name")
    return exp.column(matches[0][1], table=matches[0][0], quoted=True)


def _literal(value: Any) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int):
        return exp.Literal.number(str(value))
    if isinstance(value, (float, Decimal)):
        if not math.isfinite(float(value)):
            raise _error("Filter numbers must be finite")
        return exp.Literal.number(str(value))
    if isinstance(value, datetime):
        return exp.Literal.string(value.isoformat(sep=" "))
    if isinstance(value, date):
        return exp.Literal.string(value.isoformat())
    if isinstance(value, str):
        if len(value) > 4000:
            raise _error("Filter string values cannot exceed 4000 characters")
        return exp.Literal.string(value)
    raise _error("Filter values must be JSON scalar values")


def _filter_value(payload: MetricQueryFilter | dict[str, Any]) -> tuple[str, str, Any]:
    if isinstance(payload, MetricQueryFilter):
        data = payload.model_dump()
    elif isinstance(payload, dict):
        data = payload
    else:
        raise _error("Metric filters must be objects")

    field = data.get("field") or data.get("column")
    if not isinstance(field, str) or not field.strip():
        raise _error("Every metric filter requires a field")
    raw_operator = data.get("operator", "=")
    if not isinstance(raw_operator, str):
        raise _error(f"Filter operator for '{field}' must be a string")
    operator = raw_operator.strip().casefold().replace("-", "_").replace(" ", "_")
    operator = {"==": "=", "<>": "!="}.get(operator, operator)
    value = data.get("value", data.get("values"))
    return field.strip(), operator, value


def _compile_filter(
    payload: MetricQueryFilter | dict[str, Any],
    catalog: dict[str, set[str]],
) -> tuple[exp.Expression, dict[str, Any]]:
    field, operator, value = _filter_value(payload)
    column = _resolve_column(field, catalog)
    normalized = {"field": field, "operator": operator, "value": value}

    if value is None and operator in {"=", "is_null"}:
        normalized["operator"] = "is_null"
        return exp.Is(this=column, expression=exp.Null()), normalized
    if value is None and operator in {"!=", "is_not_null"}:
        normalized["operator"] = "is_not_null"
        return exp.Not(this=exp.Is(this=column, expression=exp.Null())), normalized
    if value is None:
        raise _error(f"Filter '{field}' with operator '{operator}' requires a value")

    binary_types: dict[str, type[exp.Binary]] = {
        "=": exp.EQ,
        "!=": exp.NEQ,
        ">": exp.GT,
        ">=": exp.GTE,
        "<": exp.LT,
        "<=": exp.LTE,
        "like": exp.Like,
    }
    if operator in binary_types:
        if isinstance(value, (list, dict)):
            raise _error(f"Filter '{field}' with operator '{operator}' requires one scalar value")
        if operator == "like" and not isinstance(value, str):
            raise _error(f"Filter '{field}' with operator 'like' requires a string")
        return binary_types[operator](this=column, expression=_literal(value)), normalized
    if operator == "not_like":
        if not isinstance(value, str):
            raise _error(f"Filter '{field}' with operator 'not_like' requires a string")
        return exp.Not(this=exp.Like(this=column, expression=_literal(value))), normalized
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value or len(value) > 100:
            raise _error(f"Filter '{field}' with operator '{operator}' requires 1 to 100 values")
        if any(isinstance(item, (list, dict)) for item in value):
            raise _error(f"Filter '{field}' contains a non-scalar list value")
        expression: exp.Expression = exp.In(
            this=column,
            expressions=[_literal(item) for item in value],
        )
        if operator == "not_in":
            expression = exp.Not(this=expression)
        return expression, normalized
    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise _error(f"Filter '{field}' with operator 'between' requires exactly two values")
        if any(isinstance(item, (list, dict)) for item in value):
            raise _error(f"Filter '{field}' contains a non-scalar range value")
        return exp.Between(
            this=column,
            low=_literal(value[0]),
            high=_literal(value[1]),
        ), normalized
    if operator in {"is_null", "is_not_null"}:
        expression = exp.Is(this=column, expression=exp.Null())
        if operator == "is_not_null":
            expression = exp.Not(this=expression)
        normalized["value"] = None
        return expression, normalized
    raise _error(f"Unsupported filter operator '{operator}' for field '{field}'")


def _row_permission_condition(raw: str, table: str, dialect: str | None) -> exp.Expression:
    """Bind one legacy row-permission WHERE fragment to the metric table.

    The fragment comes from admin-configured rules rendered by the legacy permission
    module. Anything other than a plain predicate over this table fails closed.
    """
    denied = _error("Row permission rule could not be applied to the metric query", 403)
    try:
        condition = sqlglot.condition(raw, dialect=dialect)
    except sqlglot.errors.SqlglotError:
        raise denied from None
    if condition.find(exp.Query, exp.Subquery, exp.Command) is not None:
        raise denied
    for column in condition.find_all(exp.Column):
        if column.args.get("db") or column.args.get("catalog"):
            raise denied
        if column.table and column.table.casefold() != table.casefold():
            raise denied
        column.set("table", exp.to_identifier(table, quoted=True))
    return exp.paren(condition)


def _allowed_version_field(reference: str, allowed_fields: list[str]) -> bool:
    requested_table, requested_field = _field_key(reference)
    for allowed in allowed_fields:
        allowed_table, allowed_field = _field_key(allowed)
        if requested_field != allowed_field:
            continue
        if requested_table is None or allowed_table is None or requested_table == allowed_table:
            return True
    return False


def _allowed_runtime_field(reference: str, version: MetricVersion) -> bool:
    return _allowed_version_field(
        reference,
        [*version.dimensions, *([version.time_field] if version.time_field else [])],
    )


def _metric_projection(
    version: MetricVersion,
    datasource_type: str | None,
    alias: str,
    catalog: dict[str, set[str]],
) -> exp.Expression:
    parsed = validate_metric_expression(version.expression, datasource_type)
    metric_expression = parsed.expressions[0].copy()
    if isinstance(metric_expression, exp.Alias):
        raise _error("Metric expressions cannot define their own SQL alias")
    if metric_expression.find(exp.Window) is not None:
        raise _error("Metric expressions cannot contain window functions")
    def qualify_column(node: exp.Expression) -> exp.Expression:
        if not isinstance(node, exp.Column):
            return node
        reference = f"{node.table}.{node.name}" if node.table else node.name
        return _resolve_column(reference, catalog)

    metric_expression = metric_expression.transform(qualify_column)

    aggregation = version.aggregation.upper()
    if aggregation != "CUSTOM" and metric_expression.find(exp.AggFunc) is not None:
        raise _error(
            f"Metric aggregation '{aggregation}' cannot wrap an expression that already contains an aggregate"
        )
    if isinstance(metric_expression, exp.Star) and aggregation != "COUNT":
        raise _error("The '*' expression is only supported with COUNT aggregation")

    aggregate_types = {
        "SUM": exp.Sum,
        "COUNT": exp.Count,
        "AVG": exp.Avg,
        "MIN": exp.Min,
        "MAX": exp.Max,
    }
    if aggregation == "COUNT_DISTINCT":
        projection: exp.Expression = exp.Count(
            this=exp.Distinct(expressions=[metric_expression])
        )
    elif aggregation == "CUSTOM":
        if metric_expression.find(exp.AggFunc) is None:
            raise _error("CUSTOM metric expressions must contain an aggregate function")
        projection = metric_expression
    elif aggregation in aggregate_types:
        projection = aggregate_types[aggregation](this=metric_expression)
    else:
        raise _error(f"Unsupported metric aggregation '{version.aggregation}'")
    return exp.alias_(projection, alias, quoted=False)


def compile_metric_plan(
    metric: MetricDefinition,
    version: MetricVersion,
    datasource: CoreDatasource,
    request: MetricQueryPlanRequest,
    catalog: dict[str, set[str]],
    row_filters: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compile an immutable metric version and constrained query plan to one SELECT.

    ``row_filters`` are the caller's row-permission fragments; they are compiled into
    the statement so the SQL fingerprint covers the permission boundary.
    """

    if len(version.required_tables) != 1:
        raise _error(
            "metric-plan-v1 supports exactly one required table; multi-table metrics remain on the governed LLM path"
        )
    required_table = version.required_tables[0]
    actual_required_table = next(
        (name for name in catalog if name.casefold() == required_table.casefold()),
        None,
    )
    if actual_required_table is None:
        raise _error(f"Required table '{required_table}' is unavailable")
    dialect = SQLGLOT_DIALECTS.get(datasource.type.casefold()) if datasource.type else None

    dimension_columns: list[exp.Column] = []
    dimensions: list[str] = []
    for dimension in request.dimensions:
        if not _allowed_version_field(dimension, version.dimensions):
            raise _error(f"Dimension '{dimension}' is not declared by metric version {version.version}")
        dimension_columns.append(_resolve_column(dimension, catalog))
        dimensions.append(dimension)

    conditions: list[exp.Expression] = []
    applied_filters: list[dict[str, Any]] = []
    for payload in version.filters:
        condition, normalized = _compile_filter(payload, catalog)
        conditions.append(condition)
        normalized["source"] = "metric_version"
        applied_filters.append(normalized)
    for payload in request.filters:
        if not _allowed_runtime_field(payload.field, version):
            raise _error(
                f"Runtime filter field '{payload.field}' is not an allowed dimension or time field"
            )
        condition, normalized = _compile_filter(payload, catalog)
        conditions.append(condition)
        normalized["source"] = "request"
        applied_filters.append(normalized)
    for item in row_filters or []:
        if item.get("table", "").casefold() != actual_required_table.casefold():
            raise _error("Row permission rule targets a table outside the metric", 403)
        conditions.append(
            _row_permission_condition(item.get("filter") or "", actual_required_table, dialect)
        )

    serialized_time_range: dict[str, str] | None = None
    if request.time_range:
        if not version.time_field:
            raise _error(f"Metric version {version.version} does not declare a time field")
        time_column = _resolve_column(version.time_field, catalog)
        conditions.extend(
            [
                exp.GTE(this=time_column.copy(), expression=_literal(request.time_range.start)),
                exp.LT(this=time_column.copy(), expression=_literal(request.time_range.end)),
            ]
        )
        serialized_time_range = {
            "start": request.time_range.start.isoformat(),
            "end": request.time_range.end.isoformat(),
            "semantics": "start_inclusive_end_exclusive",
        }

    projections: list[exp.Expression] = [column.copy() for column in dimension_columns]
    projections.append(_metric_projection(version, datasource.type, metric.code, catalog))
    query = exp.Select(expressions=projections).from_(
        exp.table_(actual_required_table, quoted=True)
    )
    if conditions:
        combined = conditions[0]
        for condition in conditions[1:]:
            combined = exp.and_(combined, condition)
        query = query.where(combined)
    if dimension_columns:
        query = query.group_by(*[column.copy() for column in dimension_columns])
    if request.limit:
        query = query.limit(request.limit)

    sql = query.sql(dialect=dialect, pretty=True)
    return {
        "metric_id": int(metric.id),
        "metric_code": metric.code,
        "metric_name": metric.name,
        "metric_version_id": int(version.id),
        "metric_version": version.version,
        "datasource_id": metric.datasource_id,
        "datasource_type": datasource.type,
        "dimensions": dimensions,
        "applied_filters": applied_filters,
        "time_range": serialized_time_range,
        "required_tables": list(version.required_tables),
        "row_permission_applied": bool(row_filters),
        "sql": sql,
        "sql_fingerprint": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
        "compiler": COMPILER_VERSION,
    }


def _row_permission_filters(
    session: Session,
    current_user: Any,
    datasource: CoreDatasource,
    tables: list[str],
) -> list[dict[str, str]]:
    """Same row rules the legacy engine applies to generated SQL (admin is exempt)."""
    if not tables:
        return []
    # Lazy import: the permission module loads the optional xpack package.
    from apps.datasource.crud.permission import get_row_permission_filters

    return get_row_permission_filters(
        session=session, current_user=current_user, ds=datasource, tables=tables
    )


def preview_metric_query_plan(
    session: Session,
    metric_id: int,
    request: MetricQueryPlanRequest,
    oid: int | None,
    current_user: Any,
) -> dict[str, Any]:
    if current_user is None:
        # Column visibility and row permissions both depend on the caller.
        raise _error("A current user is required to apply datasource permissions", 403)
    workspace_id = int(oid or 1)
    metric = _get_definition(session, metric_id, workspace_id)
    datasource = session.get(CoreDatasource, metric.datasource_id)
    if not datasource or datasource.oid != workspace_id:
        raise _error("Datasource was not found in the current workspace", 404)

    version_id = request.version_id or metric.current_version_id
    if not version_id:
        raise _error("Metric has no published version", 409)
    version = session.exec(
        select(MetricVersion).where(
            and_(
                MetricVersion.id == version_id,
                MetricVersion.metric_id == metric_id,
                MetricVersion.status.in_(["published", "superseded"]),
            )
        )
    ).first()
    if not version:
        raise _error("Published metric version was not found", 404)

    visible_fields = _visible_fields_for_user(
        session,
        metric.datasource_id,
        current_user,
    )
    if not _metric_version_visible(version, visible_fields, datasource.type):
        raise _error("Metric version is outside the current user's datasource permissions", 403)

    catalog = _field_catalog(session, metric.datasource_id, version.required_tables)
    row_filters = _row_permission_filters(session, current_user, datasource, list(catalog))
    plan = compile_metric_plan(
        metric, version, datasource, request, catalog, row_filters=row_filters
    )
    # Keep the database driver/xpack import out of the pure compiler so it can be
    # tested and reused without initializing SQLBot's execution stack.
    from apps.db.db import check_sql_read

    safe, reason = check_sql_read(plan["sql"], datasource)
    if not safe:
        raise _error(f"Compiled metric SQL failed the read-only guard: {reason}")
    return plan

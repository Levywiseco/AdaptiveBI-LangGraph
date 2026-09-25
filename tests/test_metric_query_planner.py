from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlglot import exp, parse_one

from apps.datasource.models.datasource import CoreDatasource
from apps.metrics.crud.metric import validate_metric_expression
from apps.metrics.crud import metric as metric_crud
from apps.metrics.models.metric import MetricDefinition, MetricVersion
from apps.metrics.schemas.metric import MetricQueryPlanRequest
from apps.metrics.service.query_planner import compile_metric_plan


def _objects(
    *,
    aggregation: str = "SUM",
    expression: str = "amount - discount_amount",
    required_tables: list[str] | None = None,
) -> tuple[MetricDefinition, MetricVersion, CoreDatasource, dict[str, set[str]]]:
    table = "adaptive_demo_sales"
    metric = MetricDefinition(
        id=9,
        oid=1,
        code="net_sales",
        name="净销售额",
        datasource_id=3,
        owner_user_id=1,
    )
    version = MetricVersion(
        id=27,
        metric_id=9,
        version=2,
        expression=expression,
        aggregation=aggregation,
        time_field="paid_at",
        required_tables=required_tables or [table],
        dimensions=["region"],
        filters=[{"field": "status", "operator": "=", "value": "paid"}],
        created_by=1,
    )
    datasource = CoreDatasource(
        id=3,
        name="demo",
        type="pg",
        configuration="{}",
        create_by=1,
        oid=1,
    )
    catalog = {
        table: {
            "amount",
            "discount_amount",
            "paid_at",
            "region",
            "status",
        }
    }
    return metric, version, datasource, catalog


def test_compiles_versioned_metric_plan_with_governed_filters_and_time_range():
    metric, version, datasource, catalog = _objects()
    request = MetricQueryPlanRequest.model_validate(
        {
            "dimensions": ["region"],
            "filters": [{"field": "region", "operator": "in", "value": ["华东", "华南"]}],
            "time_range": {
                "start": "2026-08-01T00:00:00",
                "end": "2026-09-01T00:00:00",
            },
            "limit": 100,
        }
    )

    plan = compile_metric_plan(metric, version, datasource, request, catalog)
    statement = parse_one(plan["sql"], read="postgres")

    assert plan["metric_version_id"] == 27
    assert plan["compiler"] == "metric-plan-v1"
    assert len(plan["sql_fingerprint"]) == 64
    assert [item["source"] for item in plan["applied_filters"]] == [
        "metric_version",
        "request",
    ]
    assert {table.name for table in statement.find_all(exp.Table)} == {"adaptive_demo_sales"}
    assert {column.name for column in statement.args["group"].find_all(exp.Column)} == {"region"}
    assert statement.find(exp.Sum) is not None
    assert statement.args["limit"].expression.this == "100"
    assert '"paid_at" >= \'2026-08-01 00:00:00\'' in plan["sql"]
    assert '"paid_at" < \'2026-09-01 00:00:00\'' in plan["sql"]


def test_filter_literals_are_escaped_as_data():
    metric, version, datasource, catalog = _objects()
    request = MetricQueryPlanRequest.model_validate(
        {
            "filters": [
                {
                    "field": "region",
                    "operator": "=",
                    "value": "华东'; DROP TABLE adaptive_demo_sales; --",
                }
            ]
        }
    )

    plan = compile_metric_plan(metric, version, datasource, request, catalog)
    statements = [item for item in [parse_one(plan["sql"], read="postgres")] if item]

    assert len(statements) == 1
    assert statements[0].find(exp.Drop) is None
    assert "''; DROP TABLE" in plan["sql"]


def test_rejects_dimensions_not_declared_by_the_published_version():
    metric, version, datasource, catalog = _objects()
    catalog["adaptive_demo_sales"].add("customer_id")
    request = MetricQueryPlanRequest(dimensions=["customer_id"])

    with pytest.raises(HTTPException, match="not declared") as exc:
        compile_metric_plan(metric, version, datasource, request, catalog)

    assert exc.value.status_code == 422


def test_rejects_runtime_filters_outside_declared_dimensions_and_time_field():
    metric, version, datasource, catalog = _objects()
    request = MetricQueryPlanRequest.model_validate(
        {"filters": [{"field": "amount", "operator": ">", "value": 100}]}
    )

    with pytest.raises(HTTPException, match="not an allowed dimension or time field"):
        compile_metric_plan(metric, version, datasource, request, catalog)


def test_v1_compiler_fails_closed_for_multi_table_metrics():
    metric, version, datasource, catalog = _objects(
        required_tables=["adaptive_demo_sales", "customers"]
    )
    catalog["customers"] = {"id"}

    with pytest.raises(HTTPException, match="exactly one required table"):
        compile_metric_plan(metric, version, datasource, MetricQueryPlanRequest(), catalog)


def test_custom_aggregation_requires_an_aggregate_expression():
    metric, version, datasource, catalog = _objects(
        aggregation="CUSTOM",
        expression="amount - discount_amount",
    )

    with pytest.raises(HTTPException, match="must contain an aggregate"):
        compile_metric_plan(metric, version, datasource, MetricQueryPlanRequest(), catalog)


def test_time_range_is_start_inclusive_and_end_exclusive():
    with pytest.raises(ValueError, match="must be later"):
        MetricQueryPlanRequest.model_validate(
            {
                "time_range": {
                    "start": datetime(2026, 9, 1),
                    "end": datetime(2026, 9, 1),
                }
            }
        )


def test_metric_expression_rejects_embedded_query_clauses():
    with pytest.raises(HTTPException, match="must not contain query clauses"):
        validate_metric_expression("amount FROM another_table", "pg")


def test_preserves_and_quotes_catalog_identifier_case():
    metric, version, datasource, _catalog = _objects(expression="Amount")
    version.required_tables = ["salesorder"]
    version.dimensions = ["Region"]
    version.filters = []
    catalog = {"SalesOrder": {"Amount", "Region", "PaidAt"}}

    plan = compile_metric_plan(
        metric,
        version,
        datasource,
        MetricQueryPlanRequest(dimensions=["region"]),
        catalog,
    )

    assert 'FROM "SalesOrder"' in plan["sql"]
    assert '"SalesOrder"."Amount"' in plan["sql"]
    assert '"SalesOrder"."Region"' in plan["sql"]


def test_structured_metric_candidates_expose_plan_fields_but_not_formula(monkeypatch):
    metric, version, _datasource, _catalog = _objects()
    metric.aliases = ["净收入"]
    metric.description = "销售额扣除折扣"
    version.unit = "CNY"
    monkeypatch.setattr(
        metric_crud,
        "_rank_published_metrics",
        lambda *args, **kwargs: [(505, metric, version)],
    )

    candidates = metric_crud.get_metric_candidates(
        object(), "八月净销售额", 1, 3, current_user=object()
    )

    assert candidates == [{
        "metric_id": 9,
        "metric_code": "net_sales",
        "metric_name": "净销售额",
        "aliases": ["净收入"],
        "description": "销售额扣除折扣",
        "metric_version_id": 27,
        "metric_version": 2,
        "dimensions": ["region"],
        "time_field": "paid_at",
        "grain": None,
        "unit": "CNY",
        "required_tables": ["adaptive_demo_sales"],
        "score": 505,
    }]
    assert "expression" not in candidates[0] and "filters" not in candidates[0]


def test_row_permission_filters_are_compiled_into_the_statement_and_fingerprint():
    metric, version, datasource, catalog = _objects()
    request = MetricQueryPlanRequest(dimensions=["region"])
    unrestricted = compile_metric_plan(metric, version, datasource, request, catalog)

    restricted = compile_metric_plan(
        metric, version, datasource, request, catalog,
        row_filters=[{"table": "adaptive_demo_sales", "filter": "(\"region\" IN ('华东'))"}],
    )

    assert unrestricted["row_permission_applied"] is False
    assert restricted["row_permission_applied"] is True
    assert '"adaptive_demo_sales"."region" IN (\'华东\')' in restricted["sql"]
    assert restricted["sql_fingerprint"] != unrestricted["sql_fingerprint"]
    where = parse_one(restricted["sql"], read="postgres").args["where"]
    # The rule is combined with the governed filters instead of replacing them.
    assert "'paid'" in where.sql() and "'华东'" in where.sql()


@pytest.mark.parametrize("datasource_type,rule,expected", [
    ("mysql", "(`region` LIKE '%东%')", "`adaptive_demo_sales`.`region` LIKE '%东%'"),
    ("sqlServer", "([region] IN (N'华东'))", "[adaptive_demo_sales].[region] IN (N'华东')"),
])
def test_row_permission_filters_follow_the_datasource_dialect(datasource_type, rule, expected):
    metric, version, datasource, catalog = _objects()
    datasource.type = datasource_type

    plan = compile_metric_plan(
        metric, version, datasource, MetricQueryPlanRequest(), catalog,
        row_filters=[{"table": "adaptive_demo_sales", "filter": rule}],
    )

    assert expected in plan["sql"]


@pytest.mark.parametrize("rule", [
    "region IN (SELECT region FROM other_table)",
    "other_table.region = 'x'",
    "private.adaptive_demo_sales.region = 'x'",
    "region = ",
    "",
])
def test_unusable_row_permission_rules_fail_closed(rule):
    metric, version, datasource, catalog = _objects()

    with pytest.raises(HTTPException) as exc:
        compile_metric_plan(
            metric, version, datasource, MetricQueryPlanRequest(), catalog,
            row_filters=[{"table": "adaptive_demo_sales", "filter": rule}],
        )

    assert exc.value.status_code == 403


def test_row_permission_rule_for_another_table_fails_closed():
    metric, version, datasource, catalog = _objects()

    with pytest.raises(HTTPException) as exc:
        compile_metric_plan(
            metric, version, datasource, MetricQueryPlanRequest(), catalog,
            row_filters=[{"table": "customers", "filter": "(\"region\" = 'x')"}],
        )

    assert exc.value.status_code == 403


def test_preview_requires_a_current_user_for_permissions():
    from apps.metrics.service.query_planner import preview_metric_query_plan

    with pytest.raises(HTTPException) as exc:
        preview_metric_query_plan(None, 9, MetricQueryPlanRequest(), 1, None)

    assert exc.value.status_code == 403

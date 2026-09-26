import json

import pytest

from app.planning import (
    MetricCandidate,
    MetricPlanningError,
    parse_metric_plan,
)


@pytest.fixture
def candidate():
    return MetricCandidate(
        metric_id=9,
        metric_code="net_sales",
        metric_name="净销售额",
        aliases=["净收入"],
        description="销售额扣除退款",
        metric_version_id=27,
        metric_version=2,
        dimensions=["region"],
        time_field="paid_at",
        grain="day",
        unit="CNY",
        required_tables=["sales"],
        score=505,
    )


def payload(**changes):
    value = {
        "metric_id": 9,
        "metric_version_id": 27,
        "dimensions": ["REGION", "region"],
        "filters": [{"field": "region", "operator": "=", "value": "east"}],
        "time_range": {"start": "2026-08-01T00:00:00", "end": "2026-09-01T00:00:00"},
        "limit": 100,
    }
    value.update(changes)
    return json.dumps(value)


def test_plan_is_bound_to_authorized_metric_version_and_fields(candidate):
    plan = parse_metric_plan(payload(), [candidate])
    assert plan.metric_id == 9 and plan.metric_version_id == 27
    assert plan.dimensions == ["region"]
    assert plan.filters[0].field == "region"


@pytest.mark.parametrize("changes,code", [
    ({"metric_id": 10}, "metric_not_authorized"),
    ({"metric_version_id": 28}, "metric_not_authorized"),
    ({"dimensions": ["customer_id"]}, "metric_dimension_not_allowed"),
    ({"filters": [{"field": "amount", "operator": ">", "value": 0}]},
     "metric_filter_not_allowed"),
])
def test_plan_rejects_untrusted_metric_or_field(candidate, changes, code):
    with pytest.raises(MetricPlanningError) as error:
        parse_metric_plan(payload(**changes), [candidate])
    assert error.value.code == code


@pytest.mark.parametrize("raw", [
    "not-json",
    "```json\n{}\n```",
    json.dumps({"metric_id": 9, "metric_version_id": 27, "sql": "DROP TABLE sales"}),
])
def test_plan_rejects_malformed_or_extra_model_output(candidate, raw):
    with pytest.raises(MetricPlanningError) as error:
        parse_metric_plan(raw, [candidate])
    assert error.value.code == "metric_plan_invalid"


def test_time_range_requires_metric_time_field(candidate):
    without_time = candidate.model_copy(update={"time_field": None})
    with pytest.raises(MetricPlanningError) as error:
        parse_metric_plan(payload(), [without_time])
    assert error.value.code == "metric_time_range_not_allowed"


@pytest.mark.parametrize("removed", [
    "metric_id", "metric_version_id", "dimensions", "filters", "time_range", "limit",
])
def test_plan_requires_every_contract_key(candidate, removed):
    value = json.loads(payload())
    value.pop(removed)
    with pytest.raises(MetricPlanningError) as error:
        parse_metric_plan(json.dumps(value), [candidate])
    assert error.value.code == "metric_plan_invalid"

import json

import pytest

from app.contracts import ModelCallError
from app.metric_graph import build_metric_graph
from app.planning import MetricCandidate


def candidate():
    return MetricCandidate(
        metric_id=9,
        metric_code="net_sales",
        metric_name="Net sales",
        aliases=["sales after refunds"],
        description="Published net sales metric",
        metric_version_id=27,
        metric_version=3,
        dimensions=["region"],
        time_field="ordered_at",
        grain="day",
        unit="USD",
        required_tables=["orders"],
        score=10,
    )


def raw_plan(**changes):
    value = {
        "metric_id": 9,
        "metric_version_id": 27,
        "dimensions": ["region"],
        "filters": [{"field": "region", "operator": "=", "value": "east"}],
        "time_range": {
            "start": "2026-08-01T00:00:00",
            "end": "2026-09-01T00:00:00",
        },
        "limit": 100,
    }
    value.update(changes)
    return json.dumps(value)


class FakeGateway:
    def __init__(self, candidates=None, plan=None):
        self.available = [candidate()] if candidates is None else candidates
        self.raw = raw_plan() if plan is None else plan
        self.calls = []

    def authorize(self):
        self.calls.append("authorize")

    def candidates(self):
        self.calls.append("candidates")
        return self.available

    def plan(self, candidates):
        self.calls.append("model")
        return self.raw

    def compile(self, plan):
        self.calls.append("compile")
        return {
            "metric_id": 9,
            "metric_code": "net_sales",
            "metric_name": "Net sales",
            "metric_version_id": 27,
            "metric_version": 3,
            "dimensions": plan.dimensions,
            "time_range": plan.time_range.model_dump(mode="json"),
            "sql_fingerprint": "a" * 64,
            "compiler": "metric-plan-v1",
        }


def run(gateway):
    return build_metric_graph(gateway).invoke({"question": "August net sales", "datasource_id": 3})


def test_metric_graph_completes_with_governed_summary():
    gateway = FakeGateway()
    result = run(gateway)
    assert result["status"] == "completed"
    assert result["metric_id"] == 9 and result["metric_version_id"] == 27
    assert result["unit"] == "USD" and result["sql_fingerprint"] == "a" * 64
    assert gateway.calls == ["authorize", "candidates", "model", "compile"]


def test_empty_candidates_stop_before_model_and_compile():
    gateway = FakeGateway(candidates=[])
    result = run(gateway)
    assert result["status"] == "rejected" and result["error"] == "metric_not_found"
    assert gateway.calls == ["authorize", "candidates"]


@pytest.mark.parametrize("plan,error", [
    (raw_plan(metric_id=99), "metric_not_authorized"),
    (raw_plan(dimensions=["secret_column"]), "metric_dimension_not_allowed"),
    ("not-json", "metric_plan_invalid"),
])
def test_invalid_or_unauthorized_plan_stops_before_compile(plan, error):
    gateway = FakeGateway(plan=plan)
    result = run(gateway)
    assert result["status"] == "rejected" and result["error"] == error
    assert "compile" not in gateway.calls


def test_gateway_rejection_stops_before_candidate_read():
    class Rejected(FakeGateway):
        def authorize(self):
            self.calls.append("authorize")
            raise ModelCallError("gateway_rejected")

    gateway = Rejected()
    result = run(gateway)
    assert result["status"] == "rejected" and result["error"] == "gateway_rejected"
    assert gateway.calls == ["authorize"]

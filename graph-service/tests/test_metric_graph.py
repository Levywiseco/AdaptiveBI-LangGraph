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
    """``plan`` may be one reply or a list of successive replies (repairs)."""

    def __init__(self, candidates=None, plan=None, compile_errors=0):
        self.available = [candidate()] if candidates is None else candidates
        replies = raw_plan() if plan is None else plan
        self.replies = list(replies) if isinstance(replies, list) else [replies]
        self.compile_errors = compile_errors
        self.repairs_seen = []
        self.calls = []

    def authorize(self):
        self.calls.append("authorize")

    def candidates(self):
        self.calls.append("candidates")
        return self.available

    def plan(self, candidates, repairs):
        self.calls.append("model")
        self.repairs_seen.append([dict(item) for item in repairs])
        return self.replies[min(len(self.repairs_seen), len(self.replies)) - 1]

    def compile(self, plan):
        self.calls.append("compile")
        if self.compile_errors:
            self.compile_errors -= 1
            raise ModelCallError("metric_compile_failed")
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
    # Two bounded repairs, each told what was wrong with the previous reply.
    assert gateway.calls.count("model") == 3 and result["repairs"] == 2
    assert gateway.repairs_seen[-1] == [{"previous": plan, "error": error}] * 2


def test_repair_feeds_the_rejection_back_and_completes():
    bad = raw_plan(dimensions=["secret_column"])
    gateway = FakeGateway(plan=[bad, raw_plan()])
    result = run(gateway)
    assert result["status"] == "completed" and result["repairs"] == 1
    assert gateway.repairs_seen == [[], [{"previous": bad, "error": "metric_dimension_not_allowed"}]]
    assert gateway.calls == ["authorize", "candidates", "model", "model", "compile"]


def test_compile_rejection_is_repaired_once_then_compiles():
    gateway = FakeGateway(compile_errors=1)
    result = run(gateway)
    assert result["status"] == "completed" and result["repairs"] == 1
    assert gateway.repairs_seen[1][0]["error"] == "metric_compile_failed"
    assert gateway.calls == ["authorize", "candidates", "model", "compile", "model", "compile"]


def test_repairs_can_be_disabled(monkeypatch):
    monkeypatch.setenv("GRAPH_MAX_PLAN_REPAIRS", "0")
    gateway = FakeGateway(plan="not-json")
    result = run(gateway)
    assert result["status"] == "rejected" and gateway.calls.count("model") == 1


@pytest.mark.parametrize("reason", ["ambiguous", "unsupported"])
def test_clarification_stops_before_compile(reason):
    reply = json.dumps({"clarification": "您想看成交总额还是净销售额？", "reason": reason},
                       ensure_ascii=False)
    gateway = FakeGateway(plan=reply)
    result = run(gateway)
    assert result["status"] == "needs_clarification" and result["clarification_reason"] == reason
    assert result["clarification"] == "您想看成交总额还是净销售额？"
    assert "compile" not in gateway.calls


@pytest.mark.parametrize("reply", [
    {"clarification": "", "reason": "ambiguous"},
    {"clarification": "x" * 301, "reason": "ambiguous"},
    {"clarification": "which?", "reason": "other"},
    {"clarification": "which?", "reason": "ambiguous", "metric_id": 9},
])
def test_malformed_clarifications_are_repairable_rejections(reply):
    gateway = FakeGateway(plan=[json.dumps(reply), raw_plan()])
    result = run(gateway)
    assert result["status"] == "completed" and result["repairs"] == 1


def test_gateway_rejection_stops_before_candidate_read():
    class Rejected(FakeGateway):
        def authorize(self):
            self.calls.append("authorize")
            raise ModelCallError("gateway_rejected")

    gateway = Rejected()
    result = run(gateway)
    assert result["status"] == "rejected" and result["error"] == "gateway_rejected"
    assert gateway.calls == ["authorize"]

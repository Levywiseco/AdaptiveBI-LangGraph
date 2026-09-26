import hashlib
import json
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.adapters.business_gateway import BusinessGateway
from app.contracts import ModelCallError
from app.main import app
from app.metric_graph import build_metric_graph
from app.planning import MetricQueryPlan

from test_metric_graph import FakeGateway, candidate, raw_plan


def executed_payload(**changes):
    value = {
        "metric_id": 9,
        "sql_fingerprint": "a" * 64,
        "columns": ["region", "net_sales"],
        "rows": [{"region": "east", "net_sales": 770}],
        "row_count": 1,
        "truncated": False,
        "elapsed_ms": 12.5,
    }
    value.update(changes)
    return value


class ExecutingGateway(FakeGateway):
    def __init__(self, executed=None, error=None):
        super().__init__()
        self.executed = executed_payload() if executed is None else executed
        self.error = error

    def execute(self, plan):
        self.calls.append("execute")
        if self.error:
            raise ModelCallError(self.error)
        return self.executed


def run(gateway):
    return build_metric_graph(gateway, execute=True).invoke(
        {"question": "August net sales", "datasource_id": 3}
    )


def test_execution_graph_returns_bounded_result_contract():
    gateway = ExecutingGateway()
    result = run(gateway)
    assert result["status"] == "completed"
    assert result["columns"] == ["region", "net_sales"]
    assert result["rows"] == [{"region": "east", "net_sales": 770}]
    assert result["row_count"] == 1 and result["truncated"] is False
    assert gateway.calls == ["authorize", "candidates", "model", "compile", "execute"]


def test_execution_failure_fails_without_rows():
    gateway = ExecutingGateway(error="metric_execution_failed")
    result = run(gateway)
    assert result["status"] == "failed" and result["error"] == "metric_execution_failed"
    assert "rows" not in result and "columns" not in result


def test_execution_timeout_is_distinct_from_failure():
    gateway = ExecutingGateway(error="metric_execution_timeout")
    result = run(gateway)
    assert result["status"] == "failed" and result["error"] == "metric_execution_timeout"


def test_plan_only_graph_never_calls_execute():
    gateway = ExecutingGateway()
    result = build_metric_graph(gateway).invoke(
        {"question": "August net sales", "datasource_id": 3}
    )
    assert result["status"] == "completed"
    assert gateway.calls == ["authorize", "candidates", "model", "compile"]
    assert "rows" not in result


def gateway_with_stub_request(monkeypatch, payload=None, status=200):
    calls = []

    def post(self, url, **kwargs):
        calls.append(url)
        if status != 200:
            return httpx.Response(status, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"code": 0, "msg": None, "data": payload},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    gateway = BusinessGateway(
        gateway_url="http://localhost", service_token="secret-a", delegation="secret-b",
        request_body={"question": "private-question", "datasource_id": 3},
    )
    return gateway, calls


def plan():
    return MetricQueryPlan(
        metric_id=9, metric_version_id=27, dimensions=["region"], filters=[],
        time_range=None, limit=100,
    )


def test_gateway_execute_parses_result_and_sends_plan(monkeypatch):
    gateway, calls = gateway_with_stub_request(monkeypatch, executed_payload())
    result = gateway.execute(plan())
    assert result["rows"] == [{"region": "east", "net_sales": 770}]
    assert calls[-1].endswith("/internal/graph/metrics/execute")


@pytest.mark.parametrize("payload", [
    executed_payload(metric_id=10),
    executed_payload(rows=[{"region": "east", "net_sales": {"leak": "object"}}]),
    executed_payload(rows=[{"region": "east", "net_sales": 770}], row_count=5),
    executed_payload(truncated=False, rows=[{"region": f"r{i}", "net_sales": i} for i in range(10001)]),
    executed_payload(sql_fingerprint="not-a-fingerprint"),
])
def test_gateway_execute_rejects_contract_violations(monkeypatch, payload):
    gateway, _ = gateway_with_stub_request(monkeypatch, payload)
    with pytest.raises(ModelCallError) as error:
        gateway.execute(plan())
    assert error.value.code == "gateway_unavailable"


@pytest.mark.parametrize("status,code", [
    (504, "metric_execution_timeout"),
    (502, "metric_execution_failed"),
    (422, "metric_execution_failed"),
    (403, "gateway_rejected"),
])
def test_gateway_execute_maps_backend_status_codes(monkeypatch, status, code):
    gateway, _ = gateway_with_stub_request(monkeypatch, status=status)
    with pytest.raises(ModelCallError) as error:
        gateway.execute(plan())
    assert error.value.code == code


@pytest.fixture
def metric_query_experiment(monkeypatch):
    for key, value in {
        "GRAPH_EXPERIMENT_ENABLED": "true", "BACKEND_TO_GRAPH_TOKEN": "a" * 32,
        "GRAPH_TO_GATEWAY_TOKEN": "b" * 32, "GRAPH_DELEGATION_SECRET": "c" * 32,
        "GRAPH_METRIC_DATASOURCES": "3",
    }.items():
        monkeypatch.setenv(key, value)
    body = {"question": "August east net sales", "datasource_id": 3, "run_id": str(uuid4())}
    claims = {
        "iss": "adaptive-backend", "aud": ["adaptive-graph", "adaptive-gateway"],
        "sub": "7", "workspace": "2", "model_id": 10,
        "scope": ["metrics:read", "model:invoke", "metric:compile", "metric:execute"],
        "purpose": "metric-query", "iat": int(time.time()), "exp": int(time.time()) + 120,
        "deadline": time.time() + 60,
        "datasource_id": 3, "run_id": body["run_id"],
        "request_hash": hashlib.sha256(("3\n" + body["question"]).encode()).hexdigest(),
    }

    def headers(changes=None):
        return {
            "X-Graph-Service": "a" * 32,
            "X-Graph-Delegation": jwt.encode(
                {**claims, **(changes or {})}, "c" * 32, algorithm="HS256"
            ),
        }

    return body, headers


def request_metric_query(body, headers):
    return TestClient(app).request(
        "POST", "/internal/v1/metrics/query", json=body, headers=headers
    )


def stub_backend(monkeypatch, execute_payload=None, execute_status=None):
    calls = []

    def post(self, url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/authorize"):
            payload = {"authorized": True}
        elif url.endswith("/candidates"):
            payload = {"candidates": [json.loads(candidate().model_dump_json())]}
        elif url.endswith("/model"):
            payload = {
                "content": raw_plan(),
                "model_calls": 1,
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            }
        elif url.endswith("/compile"):
            payload = {
                "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
                "metric_version_id": 27, "metric_version": 3, "dimensions": ["region"],
                "time_range": None, "sql_fingerprint": "a" * 64,
                "compiler": "metric-plan-v1", "sql": "SELECT hidden", "formula": "hidden",
            }
        else:
            assert url.endswith("/execute")
            if execute_status is not None:
                return httpx.Response(execute_status, request=httpx.Request("POST", url))
            payload = dict(execute_payload or executed_payload(), sql="SELECT hidden")
        return httpx.Response(200, json={"code": 0, "msg": None, "data": payload},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    return calls


def test_metric_query_http_roundtrip_returns_rows_and_filters_secrets(
        metric_query_experiment, monkeypatch):
    body, headers = metric_query_experiment
    calls = stub_backend(monkeypatch)
    response = request_metric_query(body, headers())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed" and data["mode"] == "metric-query"
    assert data["columns"] == ["region", "net_sales"]
    assert data["rows"] == [{"region": "east", "net_sales": 770}]
    assert data["row_count"] == 1 and data["truncated"] is False
    assert data["usage"]["total_tokens"] == 30 and data["model_calls"] == 1
    assert not {"sql", "formula", "plan", "candidates", "question", "prompt"} & data.keys()
    assert calls == ["authorize", "candidates", "model", "compile", "execute"]


def test_metric_query_reports_truncation(metric_query_experiment, monkeypatch):
    body, headers = metric_query_experiment
    stub_backend(monkeypatch, execute_payload=executed_payload(
        rows=[{"region": "east", "net_sales": 770}, {"region": "west", "net_sales": 650}],
        row_count=2, truncated=True,
    ))
    data = request_metric_query(body, headers()).json()
    assert data["truncated"] is True and data["row_count"] == 2


@pytest.mark.parametrize("execute_status,expected", [
    (504, "metric_execution_timeout"),
    (502, "metric_execution_failed"),
])
def test_metric_query_surfaces_execution_errors(metric_query_experiment, monkeypatch,
                                                execute_status, expected):
    body, headers = metric_query_experiment
    stub_backend(monkeypatch, execute_status=execute_status)
    data = request_metric_query(body, headers()).json()
    assert data["status"] == "failed" and data["error"] == expected
    assert data["rows"] == [] and data["row_count"] == 0


@pytest.mark.parametrize("change", [
    {"purpose": "metric-plan"},
    {"scope": ["metrics:read", "model:invoke", "metric:compile"]},
    {"purpose": "other"}, {"datasource_id": 4}, {"run_id": "other"},
])
def test_plan_delegation_cannot_reach_query_endpoint(metric_query_experiment, monkeypatch, change):
    body, headers = metric_query_experiment
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("gateway called"))
    assert request_metric_query(body, headers(change)).status_code == 401


@pytest.mark.parametrize("change", [
    {"deadline": None}, {"deadline": "soon"}, {"deadline": int(time.time()) - 10},
    {"deadline": int(time.time()) + 500},
])
def test_metric_query_requires_deadline_inside_delegation_lifetime(
        metric_query_experiment, monkeypatch, change):
    body, headers = metric_query_experiment
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("gateway called"))
    assert request_metric_query(body, headers(change)).status_code == 401


def test_metric_query_past_deadline_stops_before_any_backend_call(metric_query_experiment, monkeypatch):
    body, headers = metric_query_experiment
    calls = stub_backend(monkeypatch)
    # Signed and inside the token lifetime, but the run budget is already spent.
    data = request_metric_query(body, headers({"deadline": time.time() + 0.2})).json()
    assert data["status"] == "failed" and data["error"] == "graph_deadline_exceeded"
    assert calls == []


def test_gateway_step_timeout_never_exceeds_remaining_budget(monkeypatch):
    seen = []
    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        seen.append(kwargs["timeout"])
        original(self, *args, **kwargs)

    gateway, _ = gateway_with_stub_request(monkeypatch, executed_payload())
    monkeypatch.setattr(httpx.Client, "__init__", init)
    gateway.execute(plan())
    assert seen[-1] == 35  # no deadline: the step cap applies
    gateway.deadline = time.time() + 10
    gateway.execute(plan())
    assert 9 < seen[-1] <= 10


def test_gateway_maps_timeouts_after_deadline_to_deadline_error(monkeypatch):
    gateway, _ = gateway_with_stub_request(monkeypatch, status=504)
    gateway.deadline = time.time() + 30
    with pytest.raises(ModelCallError) as error:
        gateway.execute(plan())
    assert error.value.code == "metric_execution_timeout"  # budget left: a real SQL timeout

    def slow(self, url, **kwargs):
        gateway.deadline = time.time()  # the budget runs out while waiting
        raise httpx.ReadTimeout("slow", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", slow)
    gateway.deadline = time.time() + 30
    with pytest.raises(ModelCallError) as error:
        gateway.execute(plan())
    assert error.value.code == "graph_deadline_exceeded"

import hashlib
import json
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from app.adapters.business_gateway import BusinessGateway
from app.adapters.model_gateway import GatewayChatModel
from app.contracts import ModelCallError
from app.main import app
from app.planning import MetricQueryPlan


@pytest.fixture
def experiment(monkeypatch):
    for key, value in {"GRAPH_EXPERIMENT_ENABLED": "true", "BACKEND_TO_GRAPH_TOKEN": "a" * 32,
                       "GRAPH_TO_GATEWAY_TOKEN": "b" * 32, "GRAPH_DELEGATION_SECRET": "c" * 32}.items():
        monkeypatch.setenv(key, value)
    body = {"question": "八月份扣除退款的金额是多少？", "datasource_id": "synthetic-sales", "run_id": str(uuid4())}
    claims = {"iss": "adaptive-backend", "aud": ["adaptive-graph", "adaptive-gateway"], "sub": "7",
              "workspace": "2", "model_id": 10, "scope": ["synthetic:query", "model:invoke"],
              "purpose": "synthetic-question", "iat": int(time.time()), "exp": int(time.time()) + 120,
              "datasource_id": "synthetic-sales", "run_id": body["run_id"],
              "request_hash": hashlib.sha256(("synthetic-sales\n" + body["question"]).encode()).hexdigest()}
    def headers(changes=None):
        return {"X-Graph-Service": "a" * 32,
                "X-Graph-Delegation": jwt.encode({**claims, **(changes or {})}, "c" * 32, algorithm="HS256")}
    return body, headers


def mock_gateway(monkeypatch, model_payload=None, error=None, denied=False):
    calls = []
    def post(self, url, **kwargs):
        calls.append(url)
        if denied:
            return httpx.Response(403, request=httpx.Request("POST", url))
        if url.endswith("/authorize"):
            payload = {"authorized": True}
        else:
            if error:
                raise error
            payload = model_payload or {"content": "SELECT SUM(gross-refund) AS net FROM sales WHERE month='2026-08'",
                                        "model_calls": 1, "usage": {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}}
        return httpx.Response(200, json={"code": 0, "msg": None, "data": payload}, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.Client, "post", post)
    return calls


def request_query(body, headers):
    # TestClient overrides .post too if the HTTP mock is installed; .request is the ASGI entrypoint.
    return TestClient(app).request("POST", "/internal/v1/query", json=body, headers=headers)


def test_arbitrary_question_offline_gateway_contract(experiment, monkeypatch):
    body, headers = experiment
    calls = mock_gateway(monkeypatch)
    response = request_query(body, headers())
    assert response.status_code == 200
    data = response.json()
    assert data["rows"] == [[1130]]
    assert data["usage"]["total_tokens"] == 70 and data["model_calls"] == 1
    assert sum(url.endswith("/model") for url in calls) == 1
    assert not {"schema", "sql", "question", "prompt", "reasoning_content", "api_key"} & data.keys()


@pytest.mark.parametrize("change", [{"sub": "8"}, {"workspace": "3"}, {"purpose": "other"},
                                      {"scope": []}, {"run_id": "other"}, {"aud": "other"},
                                      {"exp": 1}, {"datasource_id": "customer-db"}, {"request_hash": "other"}])
def test_forged_or_wrong_delegation_rejected(experiment, monkeypatch, change):
    body, headers = experiment
    calls = mock_gateway(monkeypatch, denied=True)
    # sub/workspace are signature-valid but must still fail current backend authorization.
    response = request_query(body, headers(change))
    assert response.status_code == 401 or response.json()["status"] == "rejected"
    assert not any(url.endswith("/model") for url in calls)


def test_invalid_signature_and_service_identity(experiment, monkeypatch):
    body, headers = experiment
    calls = mock_gateway(monkeypatch)
    for bad in ({**headers(), "X-Graph-Service": "bad"},
                {**headers(), "X-Graph-Delegation": "forged"}):
        assert request_query(body, bad).status_code == 401
    assert calls == []


@pytest.mark.parametrize("field", ["Principal", "user_id", "oid", "api_key", "model_url", "sql", "connection_string"])
def test_external_identity_and_configuration_fields_rejected(experiment, monkeypatch, field):
    body, headers = experiment
    calls = mock_gateway(monkeypatch)
    response = request_query({**body, field: "injected-secret"}, headers())
    assert response.status_code == 422
    assert "injected-secret" not in response.text
    assert calls == []


@pytest.mark.parametrize("content", ["", "```sql\nSELECT * FROM sales\n```", "Here is the query: SELECT * FROM sales", "SELECT * FROM sales\nThis is your answer."])
def test_bad_output_stops_without_query(experiment, monkeypatch, content):
    from app.synthetic import SyntheticTools
    body, headers = experiment
    mock_gateway(monkeypatch, {"content": content, "model_calls": 1})
    monkeypatch.setattr(SyntheticTools, "execute", lambda *args: pytest.fail("bad output executed"))
    result = request_query(body, headers()).json()
    assert result["status"] in ("failed", "rejected")
    assert result["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


def test_dangerous_model_sql_rejected(experiment, monkeypatch):
    body, headers = experiment
    mock_gateway(monkeypatch, {"content": "DELETE FROM sales", "model_calls": 1})
    result = request_query(body, headers()).json()
    assert result["status"] == "rejected" and result["error"] == "sql_validation_failed"


def test_timeout_unknown_usage_and_no_fake_fallback(experiment, monkeypatch):
    body, headers = experiment
    mock_gateway(monkeypatch, error=httpx.ReadTimeout("sensitive URL or key"))
    result = request_query(body, headers()).json()
    assert result["error"] == "model_timeout"
    assert result["model_calls"] is None and result["usage"]["total_tokens"] is None
    assert result["rows"] == []
    assert "sensitive" not in json.dumps(result)


def test_default_disabled_and_demo_sanitized(experiment, monkeypatch):
    body, headers = experiment
    monkeypatch.delenv("GRAPH_EXPERIMENT_ENABLED")
    assert request_query(body, headers()).status_code == 404
    result = TestClient(app).post("/demo/run", json={"case_id": "net"}).json()["result"]
    assert result["rows"] == [[1420]]
    assert not {"schema", "sql", "question"} & result.keys()


def test_adapter_serialization_excludes_credentials():
    model = GatewayChatModel(gateway_url="http://localhost", service_token="secret-a", delegation="secret-b",
                             request_body={"question": "private-question"})
    serialized = json.dumps(model.model_dump(), default=str)
    assert "secret-a" not in serialized and "secret-b" not in serialized and "private-question" not in serialized


@pytest.fixture
def metric_experiment(monkeypatch):
    for key, value in {
        "GRAPH_EXPERIMENT_ENABLED": "true",
        "BACKEND_TO_GRAPH_TOKEN": "a" * 32,
        "GRAPH_TO_GATEWAY_TOKEN": "b" * 32,
        "GRAPH_DELEGATION_SECRET": "c" * 32,
        "GRAPH_METRIC_DATASOURCES": "3",
    }.items():
        monkeypatch.setenv(key, value)
    body = {"question": "August east net sales", "datasource_id": 3, "run_id": str(uuid4())}
    claims = {
        "iss": "adaptive-backend", "aud": ["adaptive-graph", "adaptive-gateway"],
        "sub": "7", "workspace": "2", "model_id": 10,
        "scope": ["metrics:read", "model:invoke", "metric:compile"],
        "purpose": "metric-plan", "iat": int(time.time()), "exp": int(time.time()) + 120,
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


def request_metric_plan(body, headers):
    return TestClient(app).request(
        "POST", "/internal/v1/metrics/plan", json=body, headers=headers
    )


def test_metric_http_graph_roundtrip_is_safe(metric_experiment, monkeypatch):
    body, headers = metric_experiment
    calls = []

    def post(self, url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/authorize"):
            payload = {"authorized": True}
        elif url.endswith("/candidates"):
            payload = {"candidates": [{
                "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
                "aliases": [], "description": "Published metric", "metric_version_id": 27,
                "metric_version": 3, "dimensions": ["region"], "time_field": "ordered_at",
                "grain": "day", "unit": "USD", "required_tables": ["orders"], "score": 10,
            }]}
        elif url.endswith("/model"):
            assert kwargs["json"]["candidates"] == [{"metric_id": 9, "metric_version_id": 27}]
            payload = {
                "content": json.dumps({
                    "metric_id": 9, "metric_version_id": 27, "dimensions": ["region"],
                    "filters": [], "time_range": None, "limit": 100,
                }),
                "model_calls": 1,
                "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            }
        else:
            assert url.endswith("/compile")
            payload = {
                "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
                "metric_version_id": 27, "metric_version": 3, "dimensions": ["region"],
                "time_range": None, "sql_fingerprint": "a" * 64,
                "compiler": "metric-plan-v1", "sql": "SELECT hidden", "formula": "hidden",
            }
        return httpx.Response(
            200, json={"code": 0, "msg": None, "data": payload},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    response = request_metric_plan(body, headers())
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed" and data["metric_id"] == 9
    assert data["unit"] == "USD" and data["usage"]["total_tokens"] == 30
    assert data["sql_fingerprint"] == "a" * 64 and data["model_calls"] == 1
    assert not {"sql", "formula", "plan", "candidates", "question"} & data.keys()
    assert [url.rsplit("/", 1)[-1] for url, _ in calls] == [
        "authorize", "candidates", "model", "compile",
    ]


def test_metric_empty_candidates_never_invokes_model(metric_experiment, monkeypatch):
    body, headers = metric_experiment
    calls = []

    def post(self, url, **kwargs):
        calls.append(url)
        payload = {"authorized": True} if url.endswith("/authorize") else {"candidates": []}
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.Client, "post", post)
    data = request_metric_plan(body, headers()).json()
    assert data["status"] == "rejected" and data["error"] == "metric_not_found"
    assert not any(url.endswith(("/model", "/compile")) for url in calls)


@pytest.mark.parametrize("change", [
    {"purpose": "other"}, {"scope": []}, {"datasource_id": 4},
    {"request_hash": "other"}, {"run_id": "other"}, {"aud": "other"},
])
def test_metric_forged_delegation_rejected_before_gateway(metric_experiment, monkeypatch, change):
    body, headers = metric_experiment
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("gateway called"))
    assert request_metric_plan(body, headers(change)).status_code == 401


def test_business_gateway_serialization_excludes_credentials_and_question():
    gateway = BusinessGateway(
        gateway_url="http://localhost", service_token="secret-a", delegation="secret-b",
        request_body={"question": "private-question", "datasource_id": 3},
    )
    serialized = json.dumps(gateway.model_dump(), default=str)
    assert "secret-a" not in serialized and "secret-b" not in serialized
    assert "private-question" not in serialized


def test_business_gateway_rejects_mismatched_compiler_result(monkeypatch):
    gateway = BusinessGateway(
        gateway_url="http://localhost", service_token="secret-a", delegation="secret-b",
        request_body={"question": "private-question", "datasource_id": 3},
    )
    monkeypatch.setattr(BusinessGateway, "_request", lambda *args, **kwargs: {
        "metric_id": 10, "metric_code": "other", "metric_name": "Other",
        "metric_version_id": 27, "metric_version": 3, "dimensions": [],
        "time_range": None, "sql_fingerprint": "a" * 64, "compiler": "metric-plan-v1",
    })
    plan = MetricQueryPlan(
        metric_id=9, metric_version_id=27, dimensions=[], filters=[], time_range=None, limit=100,
    )
    with pytest.raises(ModelCallError) as error:
        gateway.compile(plan)
    assert error.value.code == "metric_compile_failed"

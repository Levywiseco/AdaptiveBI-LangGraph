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
from app.planning import MetricQueryPlan


def test_invalid_signature_and_service_identity(metric_experiment, monkeypatch):
    body, headers = metric_experiment
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("gateway called"))
    for bad in ({**headers(), "X-Graph-Service": "bad"},
                {**headers(), "X-Graph-Delegation": "forged"}):
        assert request_metric_plan(body, bad).status_code == 401


@pytest.mark.parametrize("field", ["Principal", "user_id", "oid", "api_key", "model_url", "sql", "connection_string"])
def test_external_identity_and_configuration_fields_rejected(metric_experiment, monkeypatch, field):
    body, headers = metric_experiment
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: pytest.fail("gateway called"))
    response = request_metric_plan({**body, field: "injected-secret"}, headers())
    assert response.status_code == 422
    assert "injected-secret" not in response.text


def test_default_disabled_and_demo_sanitized(metric_experiment, monkeypatch):
    body, headers = metric_experiment
    monkeypatch.delenv("GRAPH_EXPERIMENT_ENABLED")
    assert request_metric_plan(body, headers()).status_code == 404
    # The retired synthetic free-SQL gateway is gone; only the offline demo remains.
    assert TestClient(app).post("/internal/v1/query", json={}).status_code == 404
    result = TestClient(app).post("/demo/run", json={"case_id": "net"}).json()["result"]
    assert result["rows"] == [[1420]]
    assert not {"schema", "sql", "question"} & result.keys()


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

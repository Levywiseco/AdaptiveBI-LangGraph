import hashlib
import json
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from app.adapters.model_gateway import GatewayChatModel
from app.contracts import ModelCallError
from app.main import app


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

"""Legacy-environment tests: isolated metadata SQLite, no live DB or LLM."""
import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine
from starlette.requests import Request

from apps.graph_gateway import api, security, service
from apps.graph_gateway.contracts import InternalQuestion
from apps.system.models.system_model import AiModelDetail, UserWsModel, WorkspaceModel
from apps.system.models.user import UserModel
from common.core.config import settings


@pytest.fixture
def configured(monkeypatch):
    for key, value in {"GRAPH_EXPERIMENT_ENABLED": True, "BACKEND_TO_GRAPH_TOKEN": "a" * 32,
                       "GRAPH_TO_GATEWAY_TOKEN": "b" * 32, "GRAPH_DELEGATION_SECRET": "c" * 32,
                       "GRAPH_TEST_USERS": "7", "GRAPH_TEST_WORKSPACES": "2", "GRAPH_MODEL_ID": 10}.items():
        monkeypatch.setattr(settings, key, value)
    database = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from apps.system.models.system_model import AiModelWorkspaceMapping
    # Clone metadata so JSONB can be represented in this test's isolated SQLite only.
    metadata = MetaData()
    for model in (UserModel, WorkspaceModel, UserWsModel, AiModelDetail, AiModelWorkspaceMapping):
        table = model.__table__.to_metadata(metadata)
        if model is UserModel:
            table.c.system_variables.type = JSON()
    metadata.create_all(database)
    with Session(database) as session:
        session.add(UserModel(id=7, account="test", oid=2, name="test", email="test@example.invalid", status=1))
        session.add(WorkspaceModel(id=2, name="test"))
        session.add(UserWsModel(id=1, uid=7, oid=2, weight=0))
        session.add(AiModelDetail(id=10, supplier=1, name="test", model_type=0, base_model="test",
                                  api_domain="https://example.invalid", config="[]", default_model=True, status=1))
        session.commit()
    monkeypatch.setattr(service, "engine", database)
    yield database
    database.dispose()


def body_and_request(changes=None):
    body = InternalQuestion(question="七月净销售额", run_id=uuid4())
    token = security.issue_delegation(7, 2, body.run_id, body.question, body.datasource_id)
    if changes:
        claims = jwt.decode(token, settings.GRAPH_DELEGATION_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        token = jwt.encode({**claims, **changes}, settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [(b"x-graph-service", b"b" * 32),
                                                      (b"x-graph-delegation", token.encode())]})
    return body, request


def test_signed_binding(configured):
    body, request = body_and_request()
    assert security.verify_request(request, body)["sub"] == "7"
    with pytest.raises(HTTPException):
        security.verify_request(request, body.model_copy(update={"question": "modified"}))


@pytest.mark.parametrize("changes", [{"aud": "other"}, {"scope": []}, {"purpose": "other"},
                                       {"model_id": 999}, {"exp": 1}, {"run_id": "other"},
                                       {"exp": int(time.time()) + 5000}])
def test_invalid_delegation(configured, changes):
    body, request = body_and_request(changes)
    with pytest.raises(HTTPException) as error:
        security.verify_request(request, body)
    assert error.value.status_code == 401


def test_current_authorization_and_revocation(configured):
    service.authorize_current(7, 2)
    for uid, oid in [(999, 2), (7, 999)]:
        with pytest.raises(HTTPException):
            service.authorize_current(uid, oid)
    with Session(configured) as session:
        user = session.get(UserModel, 7)
        user.status = 0
        session.add(user)
        session.commit()
    with pytest.raises(HTTPException):
        service.authorize_current(7, 2)


@pytest.mark.parametrize("change", ["membership", "model_disabled", "model_unmapped", "workspace"])
def test_workspace_and_model_revocation(configured, change):
    with Session(configured) as session:
        if change == "membership":
            session.delete(session.get(UserWsModel, 1))
        elif change == "workspace":
            session.delete(session.get(WorkspaceModel, 2))
        else:
            model = session.get(AiModelDetail, 10)
            if change == "model_disabled":
                model.status = 0
            else:
                model.default_model = False
            session.add(model)
        session.commit()
    with pytest.raises(HTTPException):
        service.authorize_current(7, 2)


def test_model_factory_usage_and_no_session_during_call(configured, monkeypatch):
    from apps.ai_model.model_factory import LLMConfig
    captured = []
    active_sessions = []
    original_session = service.Session
    class TrackingSession(original_session):
        def __enter__(self):
            active_sessions.append(self)
            return super().__enter__()
        def __exit__(self, *args):
            active_sessions.remove(self)
            return super().__exit__(*args)
    monkeypatch.setattr(service, "Session", TrackingSession)
    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test", api_key="never-return",
                         api_base_url="https://example.invalid")
    class Model:
        async def ainvoke(self, messages):
            assert not active_sessions
            captured.append(messages)
            return SimpleNamespace(content="SELECT SUM(gross-refund) FROM sales", usage_metadata={
                "input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                additional_kwargs={"reasoning_content": "private reasoning"})
    def factory(config):
        assert config.additional_params["max_retries"] == 0
        assert config.additional_params["timeout"] == 25
        return SimpleNamespace(llm=Model())
    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", factory)
    result = asyncio.run(service.invoke_model({"sub": "7", "workspace": "2", "model_id": 10}, "new question"))
    assert result["usage"]["total_tokens"] == 18 and result["model_calls"] == 1
    assert captured[0][-1].content == "new question"
    assert "never-return" not in str(result) and "reasoning" not in str(result)


def test_unauthorized_model_never_reaches_factory(configured, monkeypatch):
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda *_: pytest.fail("unauthorized model invoked"))
    with pytest.raises(HTTPException):
        asyncio.run(service.invoke_model({"sub": "999", "workspace": "2", "model_id": 10}, "new question"))


def test_internal_routes_have_service_auth_even_with_user_middleware_bypass(configured):
    from apps.system.middleware.auth import TokenMiddleware
    application = FastAPI()
    application.include_router(api.router, prefix=settings.API_V1_STR)
    application.add_middleware(TokenMiddleware)
    client = TestClient(application)
    body, request = body_and_request()
    for endpoint in ("model", "authorize"):
        response = client.post(settings.API_V1_STR + "/internal/graph/" + endpoint,
                               json=body.model_dump(mode="json"))
        assert response.status_code == 401
    response = client.post(settings.API_V1_STR + "/internal/graph/authorize",
                           json=body.model_dump(mode="json"), headers=dict(request.headers))
    assert response.status_code == 200 and response.json() == {"authorized": True}


def test_public_query_rejects_missing_user_and_injected_fields(configured):
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    client = TestClient(application)
    assert client.post("/api/v1/analysis/query", json={"question": "anything"}).status_code == 401
    for field in ("user_id", "oid", "api_key", "sql", "model_url", "Principal"):
        response = client.post("/api/v1/analysis/query", json={"question": "anything", field: "injected-secret"})
        assert response.status_code == 422
        assert "injected-secret" not in response.text


def test_disabled_by_default(configured, monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_EXPERIMENT_ENABLED", False)
    with pytest.raises(HTTPException) as error:
        service.authorize_current(7, 2)
    assert error.value.status_code == 404


@pytest.mark.parametrize("bad_output", ["timeout", "empty", "nontext"])
def test_provider_errors_and_unknown_usage(configured, monkeypatch, bad_output):
    from apps.ai_model.model_factory import LLMConfig
    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test")
    class Model:
        async def ainvoke(self, messages):
            if bad_output == "timeout":
                raise asyncio.TimeoutError("private endpoint")
            return SimpleNamespace(content="" if bad_output == "empty" else [], usage_metadata=None)
    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    result = asyncio.run(service.invoke_model({"sub": "7", "workspace": "2", "model_id": 10}, "new question"))
    assert result["error"] == ("model_timeout" if bad_output == "timeout" else "model_output_invalid")
    assert result["model_calls"] == 1
    assert result["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert "private endpoint" not in str(result)


def test_logged_in_query_delegates_and_filters_response(configured, monkeypatch):
    import httpx
    from apps.system.middleware import auth
    from apps.system.schemas.system_schema import UserInfoDTO
    from common.core.response_middleware import ResponseMiddleware
    from datetime import timedelta
    from common.core.security import create_access_token

    async def user_info(*, session, user_id):
        user = session.get(UserModel, user_id)
        return UserInfoDTO.model_validate(user.model_dump()) if user else None
    monkeypatch.setattr(auth, "engine", configured)
    monkeypatch.setattr(auth, "get_user_info", user_info)
    captured = []
    async def post(self, url, **kwargs):
        captured.append(kwargs)
        body = kwargs["json"]
        claims = jwt.decode(kwargs["headers"]["X-Graph-Delegation"], settings.GRAPH_DELEGATION_SECRET,
                            algorithms=["HS256"], audience="adaptive-graph")
        assert claims["sub"] == "7" and claims["workspace"] == "2"
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "run_id": body["run_id"], "status": "completed", "rows": [[1420]], "columns": ["net"],
            "model_calls": 1, "schema": "hidden", "prompt": "hidden", "api_key": "hidden", "sql": "hidden"})
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    application.add_middleware(auth.TokenMiddleware)
    application.add_middleware(ResponseMiddleware)
    client = TestClient(application)
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    response = client.post("/api/v1/analysis/query", json={"question": "not a case id"},
                           headers={settings.TOKEN_KEY: "Bearer " + token})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rows"] == [[1420]]
    assert not {"schema", "sql", "prompt", "api_key"} & data.keys()
    unknown = create_access_token({"id": 999}, timedelta(minutes=1))
    assert client.post("/api/v1/analysis/query", json={"question": "anything"},
                       headers={settings.TOKEN_KEY: "Bearer " + unknown}).status_code == 401
    assert len(captured) == 1


def test_wire_schema_matches_graph_service():
    # Compare source-independent JSON schema contracts in the two dependency environments.
    import json
    from pathlib import Path
    import subprocess
    from apps.graph_gateway.contracts import QuestionRequest, SafeResponse
    root = Path(__file__).resolve().parents[1]
    python = root / "graph-service/.venv/Scripts/python.exe"
    if not python.exists():
        pytest.skip("graph-service environment required for cross-environment schema comparison")
    output = subprocess.check_output([str(python), "-c",
        "import json; from app.contracts import QuestionRequest, SafeResponse; "
        "print(json.dumps([QuestionRequest.model_json_schema(), SafeResponse.model_json_schema()]))"],
        cwd=root / "graph-service", text=True)
    request_schema, response_schema = json.loads(output)
    assert request_schema["properties"] == QuestionRequest.model_json_schema()["properties"]
    assert response_schema["properties"] == SafeResponse.model_json_schema()["properties"]

@pytest.fixture
def http_graph_stack(configured, monkeypatch):
    """Real loopback HTTP across two dependency environments; provider is a stub."""
    import os
    import secrets
    import socket
    import subprocess
    import threading
    import time
    from pathlib import Path
    import httpx
    import uvicorn
    from apps.system.middleware import auth
    from apps.system.schemas.system_schema import UserInfoDTO
    from common.core.response_middleware import ResponseMiddleware

    root = Path(__file__).resolve().parents[1]
    graph_python = root / "graph-service/.venv/Scripts/python.exe"
    if not graph_python.exists():
        graph_python = root / "graph-service/.venv/bin/python"
    if not graph_python.exists():
        pytest.skip("independent graph-service environment required")
    for key in ("BACKEND_TO_GRAPH_TOKEN", "GRAPH_TO_GATEWAY_TOKEN", "GRAPH_DELEGATION_SECRET"):
        monkeypatch.setattr(settings, key, secrets.token_urlsafe(48))
    async def user_info(*, session, user_id):
        user = session.get(UserModel, user_id)
        return UserInfoDTO.model_validate(user.model_dump()) if user else None
    monkeypatch.setattr(auth, "engine", configured)
    monkeypatch.setattr(auth, "get_user_info", user_info)
    application = FastAPI()
    application.include_router(api.router, prefix=settings.API_V1_STR)
    application.add_middleware(auth.TokenMiddleware)
    application.add_middleware(ResponseMiddleware)
    backend_socket = socket.socket()
    backend_socket.bind(("127.0.0.1", 0))
    backend_url = f"http://127.0.0.1:{backend_socket.getsockname()[1]}"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        graph_port = reservation.getsockname()[1]
    graph_url = f"http://127.0.0.1:{graph_port}"
    monkeypatch.setattr(settings, "GRAPH_SERVICE_URL", graph_url)
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "LANG"}}
    environment.update({"GRAPH_EXPERIMENT_ENABLED": "true",
                        "GRAPH_GATEWAY_URL": backend_url + settings.API_V1_STR,
                        **{key: getattr(settings, key) for key in
                           ("BACKEND_TO_GRAPH_TOKEN", "GRAPH_TO_GATEWAY_TOKEN", "GRAPH_DELEGATION_SECRET")}})
    server = uvicorn.Server(uvicorn.Config(application, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [backend_socket]}, daemon=True)
    process = None
    try:
        thread.start()
        process = subprocess.Popen([str(graph_python), "-m", "uvicorn", "app.main:app",
                                    "--host", "127.0.0.1", "--port", str(graph_port), "--log-level", "critical"],
                                   cwd=root / "graph-service", env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=1, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("isolated graph process exited during startup")
                try:
                    if server.started and client.get(graph_url + "/openapi.json").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("isolated HTTP stack did not start within 20 seconds")
            yield backend_url + settings.API_V1_STR, graph_url
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.should_exit = True
        thread.join(timeout=5)
        backend_socket.close()


@pytest.mark.parametrize("provider_output,expected_status,expected_rows", [
    ("SELECT SUM(gross-refund) AS net FROM sales WHERE month='2026-08' AND region='east'", "completed", [[770]]),
    ("DELETE FROM sales", "rejected", []),
])
def test_real_http_roundtrip_with_stub_provider(http_graph_stack, monkeypatch,
                                               provider_output, expected_status, expected_rows):
    import httpx
    from datetime import timedelta
    from apps.ai_model.model_factory import LLMConfig
    from common.core.security import create_access_token
    calls = []
    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test")
    class Model:
        async def ainvoke(self, messages):
            calls.append(messages[-1].content)
            return SimpleNamespace(content=provider_output, usage_metadata=None)
    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    backend_url, graph_url = http_graph_stack
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    question = "2026年8月东部扣除退款后的销售额是多少？"
    with httpx.Client(timeout=45, trust_env=False) as client:
        # Neither a direct graph request nor an anonymous public request can invoke the model.
        assert client.post(graph_url + "/internal/v1/query", json={
            "question": question, "run_id": str(uuid4())}).status_code == 401
        assert client.post(backend_url + "/analysis/query", json={"question": question}).status_code == 401
        assert calls == []
        response = client.post(backend_url + "/analysis/query", json={"question": question},
                               headers={settings.TOKEN_KEY: "Bearer " + token})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == expected_status, data
    assert data["rows"] == expected_rows
    assert data["model_calls"] == 1 and data["model_config_id"] == 10
    assert data["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert calls == [question]
    assert not {"schema", "sql", "prompt", "api_key", "messages"} & data.keys()

"""Legacy-environment tests: isolated metadata SQLite, no live DB or LLM."""
import asyncio
import datetime
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import sqlbot_xpack  # noqa: F401 — main.py loads xpack first; keep app import order stable
import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Integer, MetaData, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine
from starlette.requests import Request

from apps.chat.models.chat_model import Chat, ChatLog, ChatQuestion, ChatRecord
from apps.datasource.models.datasource import CoreDatasource
from apps.datasource.utils.utils import aes_encrypt
from apps.graph_gateway import api, security, service
from apps.graph_gateway.contracts import (
    InternalMetricQuestion,
    MetricCandidateRef,
    MetricQueryResponse,
)
from apps.system.models.system_model import AiModelDetail, UserWsModel, WorkspaceModel
from apps.system.models.user import UserModel
from apps.system.schemas.system_schema import UserInfoDTO
from common.core.config import settings


@pytest.fixture
def configured(monkeypatch):
    for key, value in {"GRAPH_EXPERIMENT_ENABLED": True, "BACKEND_TO_GRAPH_TOKEN": "a" * 32,
                       "GRAPH_TO_GATEWAY_TOKEN": "b" * 32, "GRAPH_DELEGATION_SECRET": "c" * 32,
                       "GRAPH_TEST_USERS": "7", "GRAPH_TEST_WORKSPACES": "2", "GRAPH_MODEL_ID": 10,
                       "GRAPH_METRIC_DATASOURCES": "3"}.items():
        monkeypatch.setattr(settings, key, value)
    database = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlbot_xpack.permissions.models.ds_permission import DsPermission
    from sqlbot_xpack.permissions.models.ds_rules import DsRules
    from apps.datasource.models.datasource import CoreField, CoreTable
    from apps.metrics.models.metric import MetricDefinition, MetricDimensionValue, MetricVersion
    from apps.system.models.system_model import AiModelWorkspaceMapping
    # Clone metadata so JSONB can be represented in this test's isolated SQLite only.
    metadata = MetaData()
    for model in (UserModel, WorkspaceModel, UserWsModel, AiModelDetail, AiModelWorkspaceMapping,
                  CoreDatasource, Chat, ChatRecord, ChatLog, MetricDefinition, MetricVersion,
                  MetricDimensionValue, CoreTable, CoreField, DsPermission, DsRules):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
        if model is UserModel:
            table.c.system_variables.type = JSON()
        if model is CoreDatasource:
            table.c.table_relation.type = JSON()
        if model is ChatLog:
            table.c.messages.type = JSON()
            table.c.token_usage.type = JSON()
        if model in (ChatRecord, MetricDimensionValue):
            # SQLite only autogenerates plain INTEGER primary keys; save_question
            # and dimension sampling rely on the database to assign ids.
            table.c.id.identity = None
            table.c.id.type = Integer()
    metadata.create_all(database)
    with Session(database) as session:
        session.add(UserModel(id=7, account="test", oid=2, name="test", email="test@example.invalid", status=1))
        session.add(WorkspaceModel(id=2, name="test"))
        session.add(UserWsModel(id=1, uid=7, oid=2, weight=0))
        session.add(AiModelDetail(id=10, supplier=1, name="test", model_type=0, base_model="test",
                                  api_domain="https://example.invalid", config="[]", default_model=True, status=1))
        session.add(CoreDatasource(id=3, name="test-metric-ds", type="pg", status="1",
                                   configuration=aes_encrypt(json.dumps({"host": "isolated"})).decode(),
                                   create_by=7, oid=2))
        session.add(Chat(id=1, oid=2, create_by=7, brief=None, chat_type="chat",
                         datasource=3, engine_type="pg", create_time=datetime.datetime.now()))
        session.commit()
    monkeypatch.setattr(service, "engine", database)
    yield database
    database.dispose()


def metric_body_and_request(changes=None):
    body = InternalMetricQuestion(question="八月东部净销售额", datasource_id=3, run_id=uuid4())
    token = security.issue_metric_delegation(7, 2, body.run_id, body.question, body.datasource_id)
    if changes:
        claims = jwt.decode(token, settings.GRAPH_DELEGATION_SECRET, algorithms=["HS256"],
                            options={"verify_aud": False})
        token = jwt.encode({**claims, **changes}, settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [(b"x-graph-service", b"b" * 32),
                                                      (b"x-graph-delegation", token.encode())]})
    return body, request


def metric_query_body_and_request(changes=None):
    body = InternalMetricQuestion(question="八月东部净销售额", datasource_id=3, run_id=uuid4())
    token = security.issue_metric_query_delegation(7, 2, body.run_id, body.question, body.datasource_id)
    if changes:
        claims = jwt.decode(token, settings.GRAPH_DELEGATION_SECRET, algorithms=["HS256"],
                            options={"verify_aud": False})
        token = jwt.encode({**claims, **changes}, settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [(b"x-graph-service", b"b" * 32),
                                                      (b"x-graph-delegation", token.encode())]})
    return body, request


def test_metric_signed_binding_and_datasource_allowlist(configured, monkeypatch):
    body, request = metric_body_and_request()
    claims = security.verify_metric_request(request, body)
    assert claims["sub"] == "7" and claims["datasource_id"] == 3
    with pytest.raises(HTTPException):
        security.verify_metric_request(request, body.model_copy(update={"question": "modified"}))
    monkeypatch.setattr(settings, "GRAPH_METRIC_DATASOURCES", "4")
    with pytest.raises(HTTPException) as error:
        security.issue_metric_delegation(7, 2, body.run_id, body.question, 3)
    assert error.value.status_code == 403


@pytest.mark.parametrize("changes", [
    {"aud": "other"}, {"scope": []}, {"purpose": "other"}, {"model_id": 999},
    {"exp": 1}, {"run_id": "other"}, {"datasource_id": 4}, {"request_hash": "other"},
    {"deadline": None}, {"deadline": "soon"}, {"deadline": 1},
    {"deadline": int(time.time()) + 5000},
])
def test_invalid_metric_delegation(configured, changes):
    body, request = metric_body_and_request(changes)
    with pytest.raises(HTTPException) as error:
        security.verify_metric_request(request, body)
    assert error.value.status_code in (401, 403)


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
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])
    monkeypatch.setattr(service, "planning_dimension_values", lambda *args: {})
    result = asyncio.run(service.invoke_metric_model(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}, "new question", 3,
        [MetricCandidateRef(metric_id=9, metric_version_id=27)]))
    assert result["usage"]["total_tokens"] == 18 and result["model_calls"] == 1
    assert captured[0][-1].content == "new question"
    assert "never-return" not in str(result) and "reasoning" not in str(result)


def metric_candidate():
    return {
        "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
        "aliases": ["sales after refunds"], "description": "Published metric",
        "metric_version_id": 27, "metric_version": 3, "dimensions": ["region"],
        "time_field": "ordered_at", "grain": "day", "unit": "USD",
        "required_tables": ["orders"], "score": 10,
    }


def test_metric_model_uses_revalidated_candidate_snapshot(configured, monkeypatch):
    from apps.ai_model.model_factory import LLMConfig
    captured = []

    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])

    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test")

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(
                content='{"metric_id":9,"metric_version_id":27,"dimensions":[],"filters":[],"time_range":null,"limit":100}',
                usage_metadata={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
            )

    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    result = asyncio.run(service.invoke_metric_model(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"},
        "August net sales", 3, [MetricCandidateRef(metric_id=9, metric_version_id=27)],
    ))
    assert result["model_calls"] == 1 and result["usage"]["total_tokens"] == 20
    prompt = captured[0].content
    assert "net_sales" in prompt and "metric_version_id" in prompt
    assert "expression" not in prompt and "api_key" not in prompt


def test_metric_model_rejects_unavailable_or_duplicate_refs(configured, monkeypatch):
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda *_: pytest.fail("invalid refs invoked model"))
    claims = {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}
    with pytest.raises(HTTPException):
        asyncio.run(service.invoke_metric_model(
            claims, "question", 3, [MetricCandidateRef(metric_id=99, metric_version_id=27)]
        ))
    duplicate = MetricCandidateRef(metric_id=9, metric_version_id=27)
    with pytest.raises(HTTPException):
        asyncio.run(service.invoke_metric_model(claims, "question", 3, [duplicate, duplicate]))


def test_metric_compile_returns_metadata_without_sql(configured, monkeypatch):
    captured = []

    def preview(session, metric_id, payload, oid, current_user):
        captured.append((metric_id, payload.version_id, oid, current_user.id))
        return {
            "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
            "metric_version_id": 27, "metric_version": 3, "datasource_id": 3,
            "dimensions": ["region"], "time_range": None,
            "sql": "SELECT secret_formula FROM orders", "sql_fingerprint": "a" * 64,
            "compiler": "metric-plan-v1",
        }

    monkeypatch.setattr(service, "preview_metric_query_plan", preview)
    result = service.compile_authorized_metric_plan(
        {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3,
        {"metric_id": 9, "metric_version_id": 27, "dimensions": ["region"],
         "filters": [], "time_range": None, "limit": 100},
    )
    assert captured == [(9, 27, 2, 7)]
    assert result["sql_fingerprint"] == "a" * 64
    assert "sql" not in result and "formula" not in result
    with pytest.raises(HTTPException) as error:
        service.compile_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3,
            {"metric_id": 9, "metric_version_id": 27},
        )
    assert error.value.status_code == 422


def install_isolated_sqlite_execution(monkeypatch, tmp_path, regions=250, stub_compiler=True):
    """Run the real exec_sql path against a temp SQLite file; only the pooled
    connection layer is redirected, read-only checks and row conversion stay real."""
    import sqlbot_xpack  # noqa: F401 — legacy import order, see service.execute_authorized_metric_plan
    import apps.db.db as db_module
    target = create_engine("sqlite:///" + str(tmp_path / "orders.db").replace("\\", "/"))
    with target.connect() as conn:
        conn.execute(text("CREATE TABLE orders (region TEXT, amount INTEGER)"))
        conn.execute(text("INSERT INTO orders (region, amount) VALUES (:region, :amount)"),
                     [{"region": f"region-{index}", "amount": index} for index in range(regions)])
        conn.commit()
    monkeypatch.setattr(db_module.pool_manager, "get_pool",
                        lambda ds, **kwargs: sessionmaker(bind=target))
    state = {"sql": "SELECT region, SUM(amount) AS total FROM orders GROUP BY region"}

    def preview(session, metric_id, payload, oid, current_user):
        return {
            "metric_id": metric_id, "metric_code": "net_sales", "metric_name": "Net sales",
            "metric_version_id": payload.version_id, "metric_version": 3, "datasource_id": 3,
            "dimensions": payload.dimensions, "applied_filters": [], "time_range": None,
            "required_tables": ["orders"], "sql": state["sql"],
            "sql_fingerprint": "a" * 64, "compiler": "metric-plan-v1",
        }

    if stub_compiler:
        monkeypatch.setattr(service, "preview_metric_query_plan", preview)
    return state


def execution_plan(**changes):
    value = {"metric_id": 9, "metric_version_id": 27, "dimensions": ["region"],
             "filters": [], "time_range": None, "limit": 300}
    value.update(changes)
    return value


def test_metric_query_delegation_binds_execute_purpose(configured):
    body, request = metric_query_body_and_request()
    claims = security.verify_metric_request(request, body)
    assert claims["purpose"] == "metric-query"
    assert claims["scope"] == security.METRIC_QUERY_SCOPES
    # The execute boundary only accepts the plan-and-execute purpose.
    plan_body, plan_request = metric_body_and_request()
    with pytest.raises(HTTPException):
        security.verify_metric_request(plan_request, plan_body, purposes=frozenset({"metric-query"}))
    # A query delegation is still valid for shared planning endpoints.
    assert security.verify_metric_request(request, body)["sub"] == "7"


def test_metric_execute_runs_readonly_grouped_query_with_row_cap(
        configured, monkeypatch, tmp_path):
    install_isolated_sqlite_execution(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "GRAPH_METRIC_MAX_ROWS", 5)
    result = asyncio.run(service.execute_authorized_metric_plan(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}, "question", 3,
        execution_plan(),
    ))
    assert result["columns"] == ["region", "total"]
    assert result["row_count"] == 5 and result["truncated"] is True
    assert len(result["rows"]) == 5 and result["rows"][0]["region"] == "region-0"
    assert result["sql_fingerprint"] == "a" * 64
    assert "sql" not in result and "formula" not in result


def test_metric_execute_returns_full_result_under_cap(configured, monkeypatch, tmp_path):
    state = install_isolated_sqlite_execution(monkeypatch, tmp_path)
    state["sql"] = ("SELECT region, SUM(amount) AS total FROM orders "
                    "WHERE region IN ('region-0', 'region-1') GROUP BY region")
    result = asyncio.run(service.execute_authorized_metric_plan(
        {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3, execution_plan(),
    ))
    assert result["row_count"] == 2 and result["truncated"] is False
    assert result["rows"][0] == {"region": "region-0", "total": 0}


def test_metric_execute_write_sql_is_blocked_by_readonly_guard(configured, monkeypatch, tmp_path):
    state = install_isolated_sqlite_execution(monkeypatch, tmp_path)
    state["sql"] = "DELETE FROM orders"
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.execute_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3, execution_plan(),
        ))
    assert error.value.status_code == 502 and error.value.detail == "metric_execution_failed"


def test_metric_execute_timeout_surfaces_dedicated_error(configured, monkeypatch, tmp_path):
    install_isolated_sqlite_execution(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "GRAPH_METRIC_EXECUTION_TIMEOUT", 0)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.execute_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3, execution_plan(),
        ))
    assert error.value.status_code == 504 and error.value.detail == "metric_execution_timeout"


@pytest.mark.parametrize("plan", [
    {"metric_id": 9, "metric_version_id": 27},
    {"metric_id": "9", "metric_version_id": 27, "dimensions": [], "filters": [],
     "time_range": None, "limit": 10, "extra": 1},
    {"metric_id": 9, "metric_version_id": 27, "dimensions": [], "filters": [],
     "time_range": None, "limit": 999999},
])
def test_metric_execute_rejects_invalid_plan_shapes(configured, plan):
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.execute_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3, plan,
        ))
    assert error.value.status_code == 422


def test_metric_execute_rejects_foreign_datasource_result(configured, monkeypatch, tmp_path):
    install_isolated_sqlite_execution(monkeypatch, tmp_path)
    original = service.preview_metric_query_plan

    def other_workspace_preview(session, metric_id, payload, oid, current_user):
        compiled = original(session, metric_id, payload, oid, current_user)
        compiled["datasource_id"] = 99
        return compiled

    monkeypatch.setattr(service, "preview_metric_query_plan", other_workspace_preview)
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.execute_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "question", 3, execution_plan(),
        ))
    assert error.value.status_code == 403


@pytest.mark.parametrize("model_name,api_base_url,expected_family,expected_extra_body", [
    ("kimi-k3", "https://api.moonshot.cn/v1", "openai-compatible", None),
    ("qwen-plus", "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "qwen-model-studio", {"enable_thinking": False}),
    ("qwen-plus", "https://example.invalid/v1", "openai-compatible", None),
])
def test_gateway_model_provider_policy(model_name, api_base_url, expected_family, expected_extra_body):
    from apps.ai_model.model_factory import LLMConfig
    from apps.graph_gateway.model_policy import gateway_model_config

    source = LLMConfig(model_id=10, model_type="openai", model_name=model_name,
                       api_key="never-return", api_base_url=api_base_url,
                       additional_params={"timeout": 999, "max_retries": 9,
                                          "extra_body": {"enable_thinking": True}})
    configured, policy = gateway_model_config(source)
    assert policy.family == expected_family
    assert configured.api_key == "never-return" and configured.api_base_url == api_base_url
    assert configured.additional_params["timeout"] == 25.0
    assert configured.additional_params["max_retries"] == 0
    assert configured.additional_params["streaming"] is False
    assert configured.additional_params.get("extra_body") == expected_extra_body


def test_unauthorized_model_never_reaches_factory(configured, monkeypatch):
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda *_: pytest.fail("unauthorized model invoked"))
    with pytest.raises(HTTPException):
        asyncio.run(service.invoke_metric_model(
            {"sub": "999", "workspace": "2", "model_id": 10, "run_id": "run"}, "new question", 3,
            [MetricCandidateRef(metric_id=9, metric_version_id=27)]))


def test_internal_routes_have_service_auth_even_with_user_middleware_bypass(configured, monkeypatch):
    from apps.system.middleware.auth import TokenMiddleware
    application = FastAPI()
    application.include_router(api.router, prefix=settings.API_V1_STR)
    application.add_middleware(TokenMiddleware)
    client = TestClient(application)
    metric_body, metric_request = metric_body_and_request()
    response = client.post(settings.API_V1_STR + "/internal/graph/metrics/authorize",
                           json=metric_body.model_dump(mode="json"), headers=dict(metric_request.headers))
    assert response.status_code == 200 and response.json() == {"authorized": True}
    # Retired synthetic routes are no longer exempt from the user middleware.
    for endpoint in ("model", "authorize"):
        assert client.post(settings.API_V1_STR + "/internal/graph/" + endpoint, json={}).status_code != 200
    for endpoint in ("authorize", "candidates"):
        response = client.post(settings.API_V1_STR + "/internal/graph/metrics/" + endpoint,
                               json=metric_body.model_dump(mode="json"))
        assert response.status_code == 401
    # The execute route also bypasses the user middleware and enforces service
    # identity plus the plan-and-execute delegation in its own handler.
    query_body, query_request = metric_query_body_and_request()
    execute_payload = {**query_body.model_dump(mode="json"), "plan": execution_plan()}
    assert client.post(settings.API_V1_STR + "/internal/graph/metrics/execute",
                       json=execute_payload).status_code == 401
    assert client.post(settings.API_V1_STR + "/internal/graph/metrics/execute",
                       json=execute_payload,
                       headers=dict(metric_request.headers)).status_code == 401
    # A valid service identity and delegation reaches current authorization. The
    # candidate store is stubbed because this suite never creates business data.
    monkeypatch.setattr(api, "authorized_metric_candidates", lambda *args: [])
    response = client.post(settings.API_V1_STR + "/internal/graph/metrics/candidates",
                           json=metric_body.model_dump(mode="json"),
                           headers=dict(metric_request.headers))
    assert response.status_code == 200 and response.json() == {"candidates": []}


def test_public_query_rejects_missing_user_and_injected_fields(configured):
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    client = TestClient(application)
    question = {"question": "anything", "datasource_id": 3}
    assert client.post("/api/v1/analysis/metrics/query", json=question).status_code == 401
    # The retired synthetic free-SQL endpoint no longer exists.
    assert client.post("/api/v1/analysis/query", json={"question": "anything"}).status_code == 404
    for field in ("user_id", "oid", "api_key", "sql", "model_url", "Principal"):
        response = client.post("/api/v1/analysis/metrics/query", json={**question, field: "injected-secret"})
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
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])
    monkeypatch.setattr(service, "planning_dimension_values", lambda *args: {})
    result = asyncio.run(service.invoke_metric_model(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}, "new question", 3,
        [MetricCandidateRef(metric_id=9, metric_version_id=27)]))
    assert result["error"] == ("model_timeout" if bad_output == "timeout" else "model_output_invalid")
    assert result["model_calls"] == 1
    assert result["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert "private endpoint" not in str(result)


def test_logged_in_metric_plan_delegates_and_filters_internal_fields(configured, monkeypatch):
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
        captured.append((url, kwargs))
        body = kwargs["json"]
        claims = jwt.decode(
            kwargs["headers"]["X-Graph-Delegation"], settings.GRAPH_DELEGATION_SECRET,
            algorithms=["HS256"], audience="adaptive-graph",
        )
        assert claims["purpose"] == "metric-plan" and claims["datasource_id"] == 3
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "mode": "metric-plan", "run_id": body["run_id"], "status": "completed",
            "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
            "metric_version_id": 27, "metric_version": 3, "dimensions": ["region"],
            "time_range": None, "unit": "USD", "sql_fingerprint": "a" * 64,
            "compiler": "metric-plan-v1", "model_calls": 1,
            "sql": "SELECT hidden", "formula": "gross-refund", "prompt": "hidden",
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    application.add_middleware(auth.TokenMiddleware)
    application.add_middleware(ResponseMiddleware)
    client = TestClient(application)
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    response = client.post(
        "/api/v1/analysis/metrics/plan",
        json={"question": "August net sales", "datasource_id": 3},
        headers={settings.TOKEN_KEY: "Bearer " + token},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["metric_id"] == 9 and data["sql_fingerprint"] == "a" * 64
    assert not {"sql", "formula", "prompt"} & data.keys()
    assert len(captured) == 1


def test_logged_in_metric_query_delegates_and_filters_internal_fields(configured, monkeypatch):
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
        captured.append(url)
        body = kwargs["json"]
        claims = jwt.decode(
            kwargs["headers"]["X-Graph-Delegation"], settings.GRAPH_DELEGATION_SECRET,
            algorithms=["HS256"], audience="adaptive-graph",
        )
        assert claims["purpose"] == "metric-query" and "metric:execute" in claims["scope"]
        assert url.endswith("/internal/v1/metrics/query")
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "mode": "metric-query", "run_id": body["run_id"], "status": "completed",
            "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
            "metric_version_id": 27, "metric_version": 3, "dimensions": ["region"],
            "time_range": None, "unit": "USD", "sql_fingerprint": "a" * 64,
            "compiler": "metric-plan-v1", "columns": ["region", "net_sales"],
            "rows": [{"region": "east", "net_sales": 770}], "row_count": 1,
            "truncated": False, "model_calls": 1,
            "sql": "SELECT hidden", "formula": "gross-refund", "prompt": "hidden",
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    application = FastAPI()
    application.include_router(api.router, prefix="/api/v1")
    application.add_middleware(auth.TokenMiddleware)
    application.add_middleware(ResponseMiddleware)
    client = TestClient(application)
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    response = client.post(
        "/api/v1/analysis/metrics/query",
        json={"question": "August net sales", "datasource_id": 3},
        headers={settings.TOKEN_KEY: "Bearer " + token},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "metric-query" and data["row_count"] == 1
    assert data["rows"] == [{"region": "east", "net_sales": 770}]
    assert not {"sql", "formula", "prompt"} & data.keys()
    assert captured and captured[-1].endswith("/metrics/query")


def test_wire_schema_matches_graph_service():
    # Compare source-independent JSON schema contracts in the two dependency environments.
    import json
    from pathlib import Path
    import subprocess
    from apps.graph_gateway.contracts import MetricPlanResponse, MetricQueryResponse, MetricQuestionRequest
    root = Path(__file__).resolve().parents[1]
    python = root / "graph-service/.venv/Scripts/python.exe"
    if not python.exists():
        python = root / "graph-service/.venv/bin/python"
    if not python.exists():
        pytest.skip("graph-service environment required for cross-environment schema comparison")
    output = subprocess.check_output([str(python), "-c",
        "import json; from app.contracts import MetricPlanResponse, MetricQueryResponse, "
        "MetricQuestionRequest; "
        "print(json.dumps([MetricQuestionRequest.model_json_schema(), "
        "MetricPlanResponse.model_json_schema(), MetricQueryResponse.model_json_schema()]))"],
        cwd=root / "graph-service", text=True)
    metric_request_schema, metric_response_schema, metric_query_response_schema = json.loads(output)
    assert metric_request_schema["properties"] == MetricQuestionRequest.model_json_schema()["properties"]
    assert metric_response_schema["properties"] == MetricPlanResponse.model_json_schema()["properties"]
    assert metric_query_response_schema["properties"] == MetricQueryResponse.model_json_schema()["properties"]

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
    candidate = metric_candidate()
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [candidate])
    monkeypatch.setattr(api, "authorized_metric_candidates", lambda *args: [candidate])

    def compile_metric(_claims, _question, datasource_id, plan):
        assert datasource_id == 3
        assert plan["metric_id"] == 9 and plan["metric_version_id"] == 27
        return {
            "metric_id": 9, "metric_code": "net_sales", "metric_name": "Net sales",
            "metric_version_id": 27, "metric_version": 3,
            "dimensions": plan["dimensions"], "time_range": plan["time_range"],
            "sql_fingerprint": "a" * 64, "compiler": "metric-plan-v1",
        }

    monkeypatch.setattr(api, "compile_authorized_metric_plan", compile_metric)
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
                        "GRAPH_METRIC_DATASOURCES": "3",
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


def test_real_metric_http_roundtrip_with_stub_provider(http_graph_stack, monkeypatch):
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
            return SimpleNamespace(
                content='{"metric_id":9,"metric_version_id":27,"dimensions":["region"],'
                        '"filters":[],"time_range":null,"limit":100}',
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            )

    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    backend_url, graph_url = http_graph_stack
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    question = "2026年8月东部净销售额"
    with httpx.Client(timeout=45, trust_env=False) as client:
        assert client.post(graph_url + "/internal/v1/metrics/plan", json={
            "question": question, "datasource_id": 3, "run_id": str(uuid4()),
        }).status_code == 401
        assert client.post(backend_url + "/analysis/metrics/plan", json={
            "question": question, "datasource_id": 3,
        }).status_code == 401
        response = client.post(
            backend_url + "/analysis/metrics/plan",
            json={"question": question, "datasource_id": 3},
            headers={settings.TOKEN_KEY: "Bearer " + token},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "completed" and data["metric_id"] == 9
    assert data["metric_version_id"] == 27 and data["unit"] == "USD"
    assert data["sql_fingerprint"] == "a" * 64 and data["compiler"] == "metric-plan-v1"
    assert data["usage"]["total_tokens"] == 30 and data["model_calls"] == 1
    assert calls == [question]
    assert not {"sql", "formula", "prompt", "plan", "candidates"} & data.keys()


def test_real_metric_query_http_roundtrip_with_stub_provider(http_graph_stack, monkeypatch, tmp_path):
    """Full plan-and-execute loop over real HTTP; SQL runs on an isolated SQLite file."""
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
            return SimpleNamespace(
                content='{"metric_id":9,"metric_version_id":27,"dimensions":["region"],'
                        '"filters":[],"time_range":null,"limit":300}',
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            )

    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    install_isolated_sqlite_execution(monkeypatch, tmp_path, regions=250)
    backend_url, graph_url = http_graph_stack
    token = create_access_token({"id": 7}, timedelta(minutes=1))
    question = "2026年8月各区域净销售额"
    with httpx.Client(timeout=45, trust_env=False) as client:
        # Anonymous and direct graph access cannot reach the execution path.
        assert client.post(graph_url + "/internal/v1/metrics/query", json={
            "question": question, "datasource_id": 3, "run_id": str(uuid4()),
        }).status_code == 401
        assert client.post(backend_url + "/analysis/metrics/query", json={
            "question": question, "datasource_id": 3,
        }).status_code == 401
        response = client.post(
            backend_url + "/analysis/metrics/query",
            json={"question": question, "datasource_id": 3},
            headers={settings.TOKEN_KEY: "Bearer " + token},
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data.get("error") is None, data
    assert data["status"] == "completed" and data["mode"] == "metric-query", data
    assert data["metric_id"] == 9 and data["metric_version_id"] == 27
    assert data["columns"] == ["region", "total"]
    # GRAPH_METRIC_MAX_ROWS defaults to 200, so the 250-region result is capped.
    assert data["row_count"] == 200 and data["truncated"] is True
    assert data["rows"][0] == {"region": "region-0", "total": 0}
    assert data["usage"]["total_tokens"] == 30 and data["model_calls"] == 1
    assert calls == [question]
    assert not {"sql", "formula", "prompt", "plan", "candidates"} & data.keys()


# --- Chat-engine grayscale routing (CHAT_ENGINE=legacy|langgraph) ---

def chat_user(session):
    user = session.get(UserModel, 7)
    dto = UserInfoDTO.model_validate(user.model_dump())
    dto.isAdmin = False
    return dto


def enable_graph_engine(monkeypatch, workspaces="2"):
    monkeypatch.setattr(settings, "CHAT_ENGINE", "langgraph")
    monkeypatch.setattr(settings, "CHAT_ENGINE_WORKSPACES", workspaces)


def test_chat_engine_routing_gates(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    with Session(configured) as session:
        user = chat_user(session)
        chat = session.get(Chat, 1)
    # Default stays on the legacy engine.
    assert chat_stream.graph_engine_enabled(user, chat) is False
    enable_graph_engine(monkeypatch, workspaces="")
    assert chat_stream.graph_engine_enabled(user, chat) is False
    enable_graph_engine(monkeypatch)
    assert chat_stream.graph_engine_enabled(user, chat) is True
    # Non-whitelisted workspace, unlisted datasource, missing datasource and a
    # malformed whitelist all fail closed to the legacy engine.
    assert chat_stream.graph_engine_enabled(user.model_copy(update={"oid": 9}), chat) is False
    monkeypatch.setattr(settings, "GRAPH_METRIC_DATASOURCES", "4")
    assert chat_stream.graph_engine_enabled(user, chat) is False
    monkeypatch.setattr(settings, "GRAPH_METRIC_DATASOURCES", "3")
    assert chat_stream.graph_engine_enabled(user, chat.model_copy(update={"datasource": None})) is False
    monkeypatch.setattr(settings, "CHAT_ENGINE_WORKSPACES", "2,not-a-number")
    assert chat_stream.graph_engine_enabled(user, chat) is False
    # An unconfigured gateway also stays on legacy.
    enable_graph_engine(monkeypatch)
    monkeypatch.setattr(settings, "GRAPH_TO_GATEWAY_TOKEN", "short")
    assert chat_stream.graph_engine_enabled(user, chat) is False


def collect_sse(response):
    async def read():
        chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk
                  async for chunk in response.body_iterator]
        return "".join(chunks)
    body = asyncio.run(read())
    events = []
    for line in body.split("\n\n"):
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):]))
    return body, events


def completed_query_result():
    return MetricQueryResponse(run_id=uuid4(), status="completed", metric_id=9,
                               metric_code="net_sales", metric_name="净销售额",
                               metric_version_id=27, metric_version=3,
                               dimensions=["region"], time_range=None, unit="CNY",
                               sql_fingerprint="a" * 64, compiler="metric-plan-v1",
                               columns=["region", "total"],
                               rows=[{"region": "east", "total": 770}],
                               row_count=1, truncated=False)


def test_graph_chat_stream_emits_legacy_events_and_persists_record(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)
    result = completed_query_result()

    async def fake_run(uid, oid, question, datasource_id, context=()):
        assert (uid, oid, datasource_id) == (7, 2, 3)
        return result

    monkeypatch.setattr(chat_stream, "run_metric_query", fake_run)
    with Session(configured) as session:
        user = chat_user(session)
        response = asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="八月东部净销售额")))
        assert response is not None
        body, events = collect_sse(response)
        assert [event["type"] for event in events] == [
            "id", "question", "datasource", "brief",
            "sql-result", "info", "sql", "sql-data", "chart", "finish",
        ]
        assert events[0]["id"] == 1 and events[1]["question"] == "八月东部净销售额"
        assert events[2]["id"] == 3 and events[2]["datasource_name"] == "test-metric-ds"
        assert "net_sales" in body and "SQL指纹" in body
        # No statement is shipped: the displayed SQL is a governed-query summary.
        assert "SELECT" not in body.upper() and "secret_formula" not in body
        chart = json.loads(events[8]["content"])
        assert chart["type"] == "table"
        assert chart["columns"] == [{"name": "region", "value": "region"},
                                    {"name": "total", "value": "total"}]
        record = session.get(ChatRecord, 1)
        assert record.finish is True and record.error is None
        assert record.sql.startswith("-- 受治理指标查询")
        data = json.loads(record.data)
        assert data["fields"] == ["region", "total"]
        assert data["data"] == [{"region": "east", "total": 770}]
        assert data["fields_info"] == [{"name": "region", "is_numeric": False},
                                       {"name": "total", "is_numeric": True}]
        assert session.get(Chat, 1).brief == "八月东部净销售额"


def test_graph_chat_stream_maps_rejections_and_does_not_fall_back(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)
    result = completed_query_result().model_copy(
        update={"status": "rejected", "error": "metric_not_found", "rows": [], "row_count": 0})

    async def fake_run(uid, oid, question, datasource_id, context=()):
        return result

    monkeypatch.setattr(chat_stream, "run_metric_query", fake_run)
    with Session(configured) as session:
        user = chat_user(session)
        response = asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="任意问题")))
        _, events = collect_sse(response)
        assert [event["type"] for event in events] == ["id", "question", "datasource", "error"]
        assert "未匹配到已发布的指标" in events[3]["content"]
        record = session.get(ChatRecord, 1)
        # save_error_message marks errored records finished, matching legacy behavior.
        assert record.finish is True and "未匹配到已发布的指标" in record.error


def test_graph_chat_stream_maps_gateway_outage(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)

    async def failing_run(uid, oid, question, datasource_id, context=()):
        raise HTTPException(502, "graph_unavailable")

    monkeypatch.setattr(chat_stream, "run_metric_query", failing_run)
    with Session(configured) as session:
        user = chat_user(session)
        response = asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="任意问题")))
        _, events = collect_sse(response)
        assert events[-1]["type"] == "error"
        assert "图服务暂不可用" in events[-1]["content"]


def test_chat_routing_returns_none_when_not_whitelisted(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    with Session(configured) as session:
        user = chat_user(session)
        # Engine disabled by default and non-whitelisted workspace both pass through.
        assert asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="q"))) is None
        enable_graph_engine(monkeypatch, workspaces="99")
        assert asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="q"))) is None
        # Non-interactive flows (MCP/embedded) never route to the graph engine.
        enable_graph_engine(monkeypatch)
        assert asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, ChatQuestion(chat_id=1, question="q"), in_chat=False)) is None


# --- Row permissions on the governed execution path ---

def install_metric_catalog(database):
    """Published metric + table catalog in the isolated metadata database."""
    from apps.datasource.models.datasource import CoreField, CoreTable
    from apps.metrics.models.metric import MetricDefinition, MetricVersion
    with Session(database) as session:
        session.add(MetricDefinition(id=9, oid=2, code="net_sales", name="Net sales",
                                     datasource_id=3, owner_user_id=7, status="published",
                                     current_version_id=27))
        session.add(MetricVersion(id=27, metric_id=9, version=3, expression="amount",
                                  aggregation="SUM", required_tables=["orders"],
                                  dimensions=["region"], status="published",
                                  validation_status="passed", created_by=7))
        session.add(CoreTable(id=31, ds_id=3, checked=True, table_name="orders",
                              table_comment="", custom_comment=""))
        for index, name in enumerate(("region", "amount")):
            session.add(CoreField(id=41 + index, ds_id=3, table_id=31, checked=True,
                                  field_name=name, field_type="text", field_comment="",
                                  custom_comment="", field_index=index))
        session.commit()


def test_metric_execute_compiles_row_permissions_into_the_executed_query(
        configured, monkeypatch, tmp_path):
    import apps.datasource.crud.permission as permission
    install_metric_catalog(configured)
    install_isolated_sqlite_execution(monkeypatch, tmp_path, regions=5, stub_compiler=False)
    claims = {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}
    plan = execution_plan(limit=None)

    monkeypatch.setattr(permission, "get_row_permission_filters",
                        lambda **kwargs: [])
    unrestricted = asyncio.run(service.execute_authorized_metric_plan(claims, "q", 3, plan))
    assert unrestricted["row_count"] == 5

    seen = []

    def rules(*, session, current_user, ds, tables):
        # The legacy renderer receives the real caller and the metric's catalog table.
        seen.append((current_user.id, ds.id, tables))
        return [{"table": "orders", "filter": "(\"region\" IN ('region-1', 'region-3'))"}]

    monkeypatch.setattr(permission, "get_row_permission_filters", rules)
    restricted = asyncio.run(service.execute_authorized_metric_plan(claims, "q", 3, plan))
    assert seen == [(7, 3, ["orders"])]
    assert restricted["rows"] == [{"region": "region-1", "net_sales": 1},
                                  {"region": "region-3", "net_sales": 3}]
    assert restricted["sql_fingerprint"] != unrestricted["sql_fingerprint"]


def test_metric_compile_refuses_unusable_row_permission_rules(configured, monkeypatch):
    import apps.datasource.crud.permission as permission
    install_metric_catalog(configured)
    monkeypatch.setattr(permission, "get_row_permission_filters", lambda **kwargs: [
        {"table": "orders", "filter": "region IN (SELECT region FROM secrets)"}])
    with pytest.raises(HTTPException) as error:
        service.compile_authorized_metric_plan(
            {"sub": "7", "workspace": "2", "model_id": 10}, "q", 3, execution_plan())
    assert error.value.status_code == 403


# --- One deadline for the whole run ---

def test_metric_delegation_carries_a_deadline_inside_its_lifetime(configured, monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_REQUEST_TIMEOUT", 60)
    body, request = metric_query_body_and_request()
    claims = security.verify_metric_request(request, body)
    assert claims["iat"] < claims["deadline"] <= claims["exp"]
    # The deadline is rounded to milliseconds, so allow that much above the budget.
    assert 50 < claims["deadline"] - time.time() <= 58.001
    # Oversized budgets are clamped inside the 120-second delegation lifetime.
    monkeypatch.setattr(settings, "GRAPH_REQUEST_TIMEOUT", 999)
    assert security.request_budget_seconds() == security.MAX_REQUEST_BUDGET_SECONDS


def test_step_timeout_is_bounded_by_the_run_deadline():
    assert service.step_timeout({}, 30) == 30
    assert 9 < service.step_timeout({"deadline": time.time() + 10}, 30) <= 10
    assert service.step_timeout({"deadline": time.time() + 100}, 30) == 30
    with pytest.raises(HTTPException) as error:
        service.step_timeout({"deadline": time.time() + 0.1}, 30)
    assert error.value.status_code == 504 and error.value.detail == "graph_deadline_exceeded"


def test_spent_budget_never_starts_a_query_or_a_model_call(configured, monkeypatch, tmp_path):
    install_isolated_sqlite_execution(monkeypatch, tmp_path)
    import apps.db.db as db_module
    monkeypatch.setattr(db_module, "exec_sql", lambda *args: pytest.fail("query started"))
    claims = {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run",
              "deadline": time.time() + 0.1}
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.execute_authorized_metric_plan(claims, "q", 3, execution_plan()))
    assert error.value.detail == "graph_deadline_exceeded"
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda *_: pytest.fail("model invoked"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(service.invoke_metric_model(
            claims, "q", 3, [MetricCandidateRef(metric_id=9, metric_version_id=27)]))
    assert error.value.detail == "graph_deadline_exceeded"


# --- Planning prompt ---

def test_planning_prompt_states_today_and_end_exclusive_ranges(monkeypatch):
    from apps.graph_gateway.prompts import metric_planning_messages
    monkeypatch.setattr(settings, "GRAPH_PLANNING_TIMEZONE", "Asia/Shanghai")
    system, human = metric_planning_messages(
        "上个月东部净销售额", [metric_candidate()], datetime.datetime(2026, 9, 25, 10, 30))
    prompt = system.content
    assert "Current date: 2026-09-25 (星期五), timezone Asia/Shanghai" in prompt
    assert "END-EXCLUSIVE" in prompt
    assert '"2026-08-01T00:00:00", end "2026-09-01T00:00:00"' in prompt
    assert '"metric_code":"net_sales"' in prompt and '"time_field":"ordered_at"' in prompt
    assert "expression" not in prompt and "api_key" not in prompt
    assert human.content == "上个月东部净销售额"


def test_planning_timezone_misconfiguration_fails_closed(monkeypatch):
    from apps.graph_gateway.prompts import planning_now
    monkeypatch.setattr(settings, "GRAPH_PLANNING_TIMEZONE", "Mars/Olympus")
    with pytest.raises(HTTPException) as error:
        planning_now()
    assert error.value.status_code == 503


def test_allowlists_tolerate_spaces_and_reject_malformed_entries(configured, monkeypatch):
    monkeypatch.setattr(settings, "GRAPH_TEST_USERS", " 7 , 8 ")
    monkeypatch.setattr(settings, "GRAPH_TEST_WORKSPACES", "2, 5")
    service.authorize_current(7, 2)
    monkeypatch.setattr(settings, "GRAPH_TEST_USERS", "7,abc")
    with pytest.raises(HTTPException) as error:
        service.authorize_current(7, 2)
    assert error.value.status_code == 503


# --- Chat routing follows experiment authorization ---

def test_chat_routing_requires_the_graph_experiment_authorization(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch, workspaces="2, 5")
    with Session(configured) as session:
        user = chat_user(session)
        chat = session.get(Chat, 1)
    assert chat_stream.graph_engine_enabled(user, chat) is True
    # A whitelisted workspace does not route users the graph run would reject.
    monkeypatch.setattr(settings, "GRAPH_TEST_USERS", "8")
    assert chat_stream.graph_engine_enabled(user, chat) is False
    monkeypatch.setattr(settings, "GRAPH_TEST_USERS", "7")
    # A chat from another workspace stays on the legacy engine.
    assert chat_stream.graph_engine_enabled(user, chat.model_copy(update={"oid": 5})) is False


def test_chat_routing_skips_assistants_and_non_streaming_callers(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)
    monkeypatch.setattr(chat_stream, "run_metric_query",
                        lambda *args: pytest.fail("graph engine used"))
    with Session(configured) as session:
        user = chat_user(session)
        question = ChatQuestion(chat_id=1, question="q")
        assert asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, question, current_assistant=SimpleNamespace(type=4))) is None
        assert asyncio.run(chat_stream.maybe_stream_graph_answer(
            session, user, question, stream=False)) is None


def test_graph_chat_names_the_conversation_after_its_first_question_only(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)

    async def fake_run(uid, oid, question, datasource_id, context=()):
        return completed_query_result()

    monkeypatch.setattr(chat_stream, "run_metric_query", fake_run)
    with Session(configured) as session:
        user = chat_user(session)
        for question in ("八月东部净销售额", "那西部呢"):
            response = asyncio.run(chat_stream.maybe_stream_graph_answer(
                session, user, ChatQuestion(chat_id=1, question=question)))
            _, events = collect_sse(response)
            assert ("brief" in [event["type"] for event in events]) is (question == "八月东部净销售额")
        assert session.get(Chat, 1).brief == "八月东部净销售额"


# --- Follow-ups, clarification and bounded repair ---

def test_metric_delegation_binds_conversation_context(configured):
    body = InternalMetricQuestion(question="那7月呢？", datasource_id=3, run_id=uuid4(),
                                  context=["8月各区域净销售额"])
    token = security.issue_metric_query_delegation(7, 2, body.run_id, body.question, 3, body.context)
    request = Request({"type": "http", "headers": [(b"x-graph-service", b"b" * 32),
                                                      (b"x-graph-delegation", token.encode())]})
    assert security.verify_metric_request(request, body)["sub"] == "7"
    for changed in ([], ["别的问题"], ["8月各区域净销售额", "追加"]):
        with pytest.raises(HTTPException):
            security.verify_metric_request(request, body.model_copy(update={"context": changed}))
    # Without context the hash is unchanged from earlier releases.
    assert security.fingerprint("q", 3) == security.fingerprint("q", 3, [])


@pytest.mark.parametrize("context", [["x"] * 4, [""], ["x" * 2001]])
def test_context_is_bounded(context):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InternalMetricQuestion(question="q", datasource_id=3, run_id=uuid4(), context=context)


def test_follow_up_recall_adds_metrics_from_earlier_questions(configured, monkeypatch):
    calls = []

    def candidates(session, text, oid, datasource_id, limit, current_user):
        calls.append(text)
        return [dict(metric_candidate(), metric_version_id=27)] if "净销售额" in text else []

    monkeypatch.setattr(service, "get_metric_candidates", candidates)
    claims = {"sub": "7", "workspace": "2"}
    result = service.authorized_metric_candidates(claims, "那7月呢？", 3, ["8月各区域净销售额"])
    assert [item["metric_version_id"] for item in result] == [27]
    assert calls == ["那7月呢？", "8月各区域净销售额"]
    assert service.authorized_metric_candidates(claims, "那7月呢？", 3) == []


def test_metric_model_sends_context_and_repairs_to_the_provider(configured, monkeypatch):
    from apps.ai_model.model_factory import LLMConfig
    captured = []
    monkeypatch.setattr(service, "authorized_metric_candidates", lambda *args: [metric_candidate()])

    async def config(model_id):
        return LLMConfig(model_id=10, model_type="openai", model_name="test")

    class Model:
        async def ainvoke(self, messages):
            captured.extend(messages)
            return SimpleNamespace(content="{}", usage_metadata=None)

    monkeypatch.setattr(service, "get_default_config", config)
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda _: SimpleNamespace(llm=Model()))
    asyncio.run(service.invoke_metric_model(
        {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}, "那7月呢？", 3,
        [MetricCandidateRef(metric_id=9, metric_version_id=27)], ["8月各区域净销售额"],
        [{"previous": '{"metric_id": 99}', "error": "metric_not_authorized"}]))
    system, question, previous, correction = captured
    assert "1. 8月各区域净销售额" in system.content and "Keep the earlier metric" in system.content
    assert '"clarification"' in system.content
    assert question.content == "那7月呢？" and previous.content == '{"metric_id": 99}'
    assert "metric_not_authorized" in correction.content and "copied together" in correction.content


def test_model_request_accepts_only_known_repair_codes():
    from pydantic import ValidationError

    from apps.graph_gateway.contracts import MetricModelRequest
    base = {"question": "q", "datasource_id": 3, "run_id": str(uuid4()),
            "candidates": [{"metric_id": 9, "metric_version_id": 27}]}
    MetricModelRequest.model_validate({**base, "repairs": [{"previous": "{}", "error": "metric_plan_invalid"}]})
    for repairs in ([{"previous": "{}", "error": "gateway_rejected"}],
                    [{"previous": "", "error": "metric_plan_invalid"}],
                    [{"previous": "{}", "error": "metric_plan_invalid"}] * 4):
        with pytest.raises(ValidationError):
            MetricModelRequest.model_validate({**base, "repairs": repairs})


def test_graph_chat_sends_earlier_questions_and_shows_clarifications(configured, monkeypatch):
    from apps.graph_gateway import chat_stream
    enable_graph_engine(monkeypatch)
    seen = []

    async def fake_run(uid, oid, question, datasource_id, context=()):
        seen.append(list(context))
        if question == "上个月卖了多少钱":
            return completed_query_result().model_copy(update={
                "status": "needs_clarification", "clarification": "您想看成交总额还是净销售额？",
                "clarification_reason": "ambiguous", "rows": [], "row_count": 0})
        return completed_query_result()

    monkeypatch.setattr(chat_stream, "run_metric_query", fake_run)
    with Session(configured) as session:
        user = chat_user(session)
        responses = []
        for question in ("上个月卖了多少钱", "净销售额", "那7月呢？"):
            responses.append(collect_sse(asyncio.run(chat_stream.maybe_stream_graph_answer(
                session, user, ChatQuestion(chat_id=1, question=question))))[1])
        clarification = responses[0][-1]
        assert clarification["type"] == "error"
        assert clarification["content"] == "需要确认：您想看成交总额还是净销售额？"
        assert "需要确认" in session.get(ChatRecord, 1).error
    assert seen == [[], ["上个月卖了多少钱"], ["上个月卖了多少钱", "净销售额"]]


def test_blocking_gateway_work_stays_off_the_event_loop(configured, monkeypatch, tmp_path):
    import inspect
    import threading
    # Database-bound internal handlers are plain functions, run in the threadpool.
    for handler in (api.metric_authorize, api.metric_candidates, api.metric_compile):
        assert not inspect.iscoroutinefunction(inspect.unwrap(handler))
    loop_threads = []

    def slow_candidates(*args):
        loop_threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
        return [metric_candidate()]

    monkeypatch.setattr(service, "authorized_metric_candidates", slow_candidates)
    monkeypatch.setattr(service, "planning_dimension_values", lambda *args: {})
    monkeypatch.setattr(service.LLMFactory, "create_llm", lambda *_: pytest.fail("not reached"))

    async def scenario():
        ticks = []

        async def heartbeat():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)

        async def call():
            async def config(model_id):
                raise HTTPException(503, "stop after the snapshot")
            monkeypatch.setattr(service, "get_default_config", config)
            with pytest.raises(HTTPException):
                await service.invoke_metric_model(
                    {"sub": "7", "workspace": "2", "model_id": 10, "run_id": "run"}, "q", 3,
                    [MetricCandidateRef(metric_id=9, metric_version_id=27)])

        await asyncio.gather(call(), heartbeat())
        return ticks

    ticks = asyncio.run(scenario())
    assert loop_threads == [False]
    # The heartbeat kept ticking while the 0.3 s database snapshot was running.
    assert len(ticks) == 5 and ticks[-1] - ticks[0] < 0.3

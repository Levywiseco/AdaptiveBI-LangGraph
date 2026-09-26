import hashlib
import hmac
import json
import os
from time import perf_counter

import jwt
from fastapi import APIRouter, HTTPException, Request

from app.adapters.business_gateway import BusinessGateway
from app.adapters.model_gateway import GatewayChatModel
from app.contracts import (
    InternalMetricQuestion,
    InternalQuestion,
    MetricPlanResponse,
    MetricQueryResponse,
    ModelCallError,
    Principal,
    SafeResponse,
)
from app.graph import build_graph
from app.metric_graph import build_metric_graph, recursion_limit
from app.synthetic import SyntheticTools

router = APIRouter()

METRIC_PURPOSE_SCOPES = {
    "metric-plan": ["metrics:read", "model:invoke", "metric:compile"],
    "metric-query": ["metrics:read", "model:invoke", "metric:compile", "metric:execute"],
}


def verify(request, body):
    if os.environ.get("GRAPH_EXPERIMENT_ENABLED", "false").lower() != "true":
        raise HTTPException(404, "experiment_disabled")
    incoming = os.environ.get("BACKEND_TO_GRAPH_TOKEN", "")
    outgoing = os.environ.get("GRAPH_TO_GATEWAY_TOKEN", "")
    secret = os.environ.get("GRAPH_DELEGATION_SECRET", "")
    if any(len(value) < 32 for value in (incoming, outgoing, secret)) or len({incoming, outgoing, secret}) != 3:
        raise HTTPException(503, "gateway_configuration_required")
    if not hmac.compare_digest(request.headers.get("X-Graph-Service", ""), incoming):
        raise HTTPException(401, "invalid_service_identity")
    try:
        claims = jwt.decode(request.headers.get("X-Graph-Delegation", ""), secret,
                            algorithms=["HS256"], audience="adaptive-graph", issuer="adaptive-backend",
                            options={"require": ["sub", "workspace", "run_id", "model_id", "scope", "purpose",
                                                 "request_hash", "datasource_id", "iat", "exp"]})
        expected_hash = hashlib.sha256((body.datasource_id + "\n" + body.question).encode()).hexdigest()
        valid = (claims["purpose"] == "synthetic-question"
                 and claims["scope"] == ["synthetic:query", "model:invoke"]
                 and claims["run_id"] == str(body.run_id)
                 and claims["datasource_id"] == body.datasource_id == "synthetic-sales"
                 and claims["request_hash"] == expected_hash
                 and 0 < claims["exp"] - claims["iat"] <= 120
                 and int(claims["sub"]) > 0 and int(claims["workspace"]) > 0
                 and type(claims["model_id"]) is int and claims["model_id"] > 0)
        if not valid:
            raise ValueError("invalid_claims")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "invalid_delegation") from None
    return claims


def verify_metric(request, body, purposes=frozenset(METRIC_PURPOSE_SCOPES)):
    if os.environ.get("GRAPH_EXPERIMENT_ENABLED", "false").lower() != "true":
        raise HTTPException(404, "experiment_disabled")
    incoming = os.environ.get("BACKEND_TO_GRAPH_TOKEN", "")
    outgoing = os.environ.get("GRAPH_TO_GATEWAY_TOKEN", "")
    secret = os.environ.get("GRAPH_DELEGATION_SECRET", "")
    if any(len(value) < 32 for value in (incoming, outgoing, secret)) or len({incoming, outgoing, secret}) != 3:
        raise HTTPException(503, "gateway_configuration_required")
    if not hmac.compare_digest(request.headers.get("X-Graph-Service", ""), incoming):
        raise HTTPException(401, "invalid_service_identity")
    try:
        allowed = {
            int(value.strip())
            for value in os.environ.get("GRAPH_METRIC_DATASOURCES", "").split(",")
            if value.strip()
        }
    except ValueError:
        raise HTTPException(503, "metric_datasource_configuration_invalid") from None
    if body.datasource_id not in allowed:
        raise HTTPException(403, "metric_datasource_not_allowed")
    try:
        claims = jwt.decode(
            request.headers.get("X-Graph-Delegation", ""),
            secret,
            algorithms=["HS256"],
            audience="adaptive-graph",
            issuer="adaptive-backend",
            options={"require": [
                "sub", "workspace", "run_id", "model_id", "scope", "purpose",
                "request_hash", "datasource_id", "deadline", "iat", "exp",
            ]},
        )
        payload = str(body.datasource_id) + "\n" + body.question
        if body.context:
            payload += "\n" + json.dumps(list(body.context), ensure_ascii=False)
        expected_hash = hashlib.sha256(payload.encode()).hexdigest()
        purpose = claims.get("purpose")
        valid = (
            purpose in METRIC_PURPOSE_SCOPES
            and purpose in purposes
            and claims["scope"] == METRIC_PURPOSE_SCOPES[purpose]
            and claims["run_id"] == str(body.run_id)
            and type(claims["datasource_id"]) is int
            and claims["datasource_id"] == body.datasource_id
            and claims["request_hash"] == expected_hash
            and 0 < claims["exp"] - claims["iat"] <= 120
            and type(claims["deadline"]) in (int, float)
            and claims["iat"] < claims["deadline"] <= claims["exp"]
            and int(claims["sub"]) > 0
            and int(claims["workspace"]) > 0
            and type(claims["model_id"]) is int
            and claims["model_id"] > 0
        )
        if not valid:
            raise ValueError("invalid_claims")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "invalid_delegation") from None
    return claims


class DelegatedSyntheticTools(SyntheticTools):
    def __init__(self, model, identity):
        self.model = model
        self.identity = identity

    def authorize(self, principal, datasource_id):
        if principal != self.identity or datasource_id not in principal.datasource_ids:
            raise PermissionError("datasource_access_denied")
        try:
            self.model.authorize()  # Recheck current backend permissions, including before SQL execution.
        except ModelCallError as exc:
            raise PermissionError("datasource_access_denied") from exc


@router.post("/internal/v1/query", response_model=SafeResponse)
def query(body: InternalQuestion, request: Request):
    claims = verify(request, body)
    model = GatewayChatModel(gateway_url=os.environ.get("GRAPH_GATEWAY_URL", "http://127.0.0.1:8000/api/v1"),
                             service_token=os.environ["GRAPH_TO_GATEWAY_TOKEN"],
                             delegation=request.headers["X-Graph-Delegation"], request_body=body.model_dump(mode="json"))
    identity = Principal(claims["sub"], claims["workspace"], frozenset({"synthetic-sales"}))
    graph = build_graph(model, DelegatedSyntheticTools(model, identity), identity)
    started = perf_counter()
    try:
        state = graph.invoke({"question": body.question, "datasource_id": body.datasource_id},
                             {"recursion_limit": 12})
    except Exception:
        state = {"status": "failed", "error": "graph_execution_failed"}
    return SafeResponse.model_validate({**state, "run_id": body.run_id, "usage": model.usage,
                                        "model_config_id": claims["model_id"],
                                        "model_calls": model.calls,
                                        "elapsed_ms": round((perf_counter() - started) * 1000, 2)})


@router.post("/internal/v1/metrics/plan", response_model=MetricPlanResponse)
def metric_plan(body: InternalMetricQuestion, request: Request):
    claims = verify_metric(request, body)
    gateway = BusinessGateway(
        gateway_url=os.environ.get("GRAPH_GATEWAY_URL", "http://127.0.0.1:8000/api/v1"),
        service_token=os.environ["GRAPH_TO_GATEWAY_TOKEN"],
        delegation=request.headers["X-Graph-Delegation"],
        request_body=body.model_dump(mode="json"),
        deadline=claims["deadline"],
    )
    graph = build_metric_graph(gateway)
    started = perf_counter()
    try:
        state = graph.invoke(
            {"question": body.question, "datasource_id": body.datasource_id},
            {"recursion_limit": recursion_limit()},
        )
    except Exception:
        state = {"status": "failed", "error": "graph_execution_failed"}
    public_fields = {
        key: state.get(key)
        for key in (
            "status", "error", "metric_id", "metric_code", "metric_name",
            "metric_version_id", "metric_version", "dimensions", "time_range",
            "unit", "sql_fingerprint", "compiler", "clarification",
            "clarification_reason", "repairs",
        )
        if key in state
    }
    return MetricPlanResponse.model_validate({
        **public_fields,
        "run_id": body.run_id,
        "usage": gateway.usage,
        "model_calls": gateway.calls,
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
    })


@router.post("/internal/v1/metrics/query", response_model=MetricQueryResponse)
def metric_query(body: InternalMetricQuestion, request: Request):
    # Only plan-and-execute delegations reach this endpoint.
    claims = verify_metric(request, body, purposes=frozenset({"metric-query"}))
    gateway = BusinessGateway(
        gateway_url=os.environ.get("GRAPH_GATEWAY_URL", "http://127.0.0.1:8000/api/v1"),
        service_token=os.environ["GRAPH_TO_GATEWAY_TOKEN"],
        delegation=request.headers["X-Graph-Delegation"],
        request_body=body.model_dump(mode="json"),
        deadline=claims["deadline"],
    )
    graph = build_metric_graph(gateway, execute=True)
    started = perf_counter()
    try:
        state = graph.invoke(
            {"question": body.question, "datasource_id": body.datasource_id},
            {"recursion_limit": recursion_limit() + 1},
        )
    except Exception:
        state = {"status": "failed", "error": "graph_execution_failed"}
    public_fields = {
        key: state.get(key)
        for key in (
            "status", "error", "metric_id", "metric_code", "metric_name",
            "metric_version_id", "metric_version", "dimensions", "time_range",
            "unit", "sql_fingerprint", "compiler", "columns", "rows",
            "row_count", "truncated", "clarification", "clarification_reason", "repairs",
        )
        if key in state
    }
    return MetricQueryResponse.model_validate({
        **public_fields,
        "run_id": body.run_id,
        "usage": gateway.usage,
        "model_calls": gateway.calls,
        "elapsed_ms": round((perf_counter() - started) * 1000, 2),
    })

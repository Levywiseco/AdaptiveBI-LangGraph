import hashlib
import hmac
import os
from time import perf_counter

import jwt
from fastapi import APIRouter, HTTPException, Request

from app.adapters.model_gateway import GatewayChatModel
from app.contracts import InternalQuestion, ModelCallError, Principal, SafeResponse
from app.graph import build_graph
from app.synthetic import SyntheticTools

router = APIRouter()


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

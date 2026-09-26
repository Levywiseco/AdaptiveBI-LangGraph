import hashlib
import hmac
import json
import time

import jwt
from fastapi import HTTPException, Request

from common.core.config import settings

ISSUER = "adaptive-backend"
METRIC_SCOPES = ["metrics:read", "model:invoke", "metric:compile"]
METRIC_QUERY_SCOPES = ["metrics:read", "model:invoke", "metric:compile", "metric:execute"]
METRIC_PURPOSE_SCOPES = {"metric-plan": METRIC_SCOPES, "metric-query": METRIC_QUERY_SCOPES}
# The graph service must answer before the backend's outer HTTP timeout fires.
DEADLINE_MARGIN_SECONDS = 2
MAX_REQUEST_BUDGET_SECONDS = 110  # stays inside the 120-second delegation lifetime


def enabled():
    if not settings.GRAPH_EXPERIMENT_ENABLED:
        raise HTTPException(404, "experiment_disabled")
    values = (settings.GRAPH_TO_GATEWAY_TOKEN, settings.BACKEND_TO_GRAPH_TOKEN,
              settings.GRAPH_DELEGATION_SECRET)
    if any(len(value) < 32 for value in values) or len(set(values)) != 3:
        raise HTTPException(503, "gateway_configuration_required")


def fingerprint(question, datasource_id, context=()):
    """Request hash signed into the delegation; conversation context is bound too."""
    payload = str(datasource_id) + "\n" + question
    if context:
        payload += "\n" + json.dumps(list(context), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def configured_ids(raw: str) -> set[int]:
    """Parse a comma-separated id allowlist; malformed entries raise ValueError."""
    return {int(value.strip()) for value in raw.split(",") if value.strip()}


def request_budget_seconds() -> float:
    return float(max(5, min(settings.GRAPH_REQUEST_TIMEOUT, MAX_REQUEST_BUDGET_SECONDS)))


def metric_datasource_enabled(datasource_id: int):
    enabled()
    try:
        allowed = configured_ids(settings.GRAPH_METRIC_DATASOURCES)
    except ValueError:
        raise HTTPException(503, "metric_datasource_configuration_invalid") from None
    if datasource_id not in allowed:
        raise HTTPException(403, "metric_datasource_not_allowed")


def issue_metric_delegation(uid, oid, run_id, question, datasource_id, context=()):
    return _issue_metric_delegation(uid, oid, run_id, question, datasource_id, context,
                                    purpose="metric-plan", scope=METRIC_SCOPES)


def issue_metric_query_delegation(uid, oid, run_id, question, datasource_id, context=()):
    return _issue_metric_delegation(uid, oid, run_id, question, datasource_id, context,
                                    purpose="metric-query", scope=METRIC_QUERY_SCOPES)


def _issue_metric_delegation(uid, oid, run_id, question, datasource_id, context, purpose, scope):
    metric_datasource_enabled(datasource_id)
    now = int(time.time())
    # One absolute deadline for the whole run; every hop derives its timeout from it.
    deadline = round(time.time() + request_budget_seconds() - DEADLINE_MARGIN_SECONDS, 3)
    return jwt.encode({"iss": ISSUER, "aud": ["adaptive-graph", "adaptive-gateway"],
                       "sub": str(uid), "workspace": str(oid), "run_id": str(run_id),
                       "model_id": settings.GRAPH_MODEL_ID, "datasource_id": datasource_id,
                       "scope": scope, "purpose": purpose,
                       "request_hash": fingerprint(question, datasource_id, context),
                       "deadline": deadline, "iat": now, "exp": now + 120},
                      settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")


def verify_metric_request(request: Request, body, purposes=frozenset(METRIC_PURPOSE_SCOPES)):
    """Verify a metric delegation; execution endpoints restrict the accepted purpose."""
    enabled()
    supplied = request.headers.get("X-Graph-Service", "")
    if not hmac.compare_digest(supplied, settings.GRAPH_TO_GATEWAY_TOKEN):
        raise HTTPException(401, "invalid_service_identity")
    metric_datasource_enabled(body.datasource_id)
    try:
        claims = jwt.decode(request.headers.get("X-Graph-Delegation", ""),
                            settings.GRAPH_DELEGATION_SECRET, algorithms=["HS256"],
                            audience="adaptive-gateway", issuer=ISSUER,
                            options={"require": ["sub", "workspace", "run_id", "model_id", "scope",
                                                 "purpose", "request_hash", "datasource_id",
                                                 "deadline", "iat", "exp"]})
        purpose = claims.get("purpose")
        valid = (purpose in METRIC_PURPOSE_SCOPES and purpose in purposes
                 and claims["scope"] == METRIC_PURPOSE_SCOPES[purpose]
                 and type(claims["datasource_id"]) is int
                 and claims["datasource_id"] == body.datasource_id
                 and claims["run_id"] == str(body.run_id)
                 and type(claims["model_id"]) is int
                 and claims["model_id"] == settings.GRAPH_MODEL_ID
                 and claims["request_hash"] == fingerprint(body.question, body.datasource_id,
                                                           body.context)
                 and 0 < claims["exp"] - claims["iat"] <= 120
                 and type(claims["deadline"]) in (int, float)
                 and claims["iat"] < claims["deadline"] <= claims["exp"]
                 and int(claims["sub"]) > 0 and int(claims["workspace"]) > 0)
        if not valid:
            raise ValueError("invalid_claims")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "invalid_delegation") from None
    return claims

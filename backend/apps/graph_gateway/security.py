import hashlib
import hmac
import time

import jwt
from fastapi import HTTPException, Request

from common.core.config import settings

ISSUER = "adaptive-backend"
SCOPES = ["synthetic:query", "model:invoke"]
METRIC_SCOPES = ["metrics:read", "model:invoke", "metric:compile"]


def enabled():
    if not settings.GRAPH_EXPERIMENT_ENABLED:
        raise HTTPException(404, "experiment_disabled")
    values = (settings.GRAPH_TO_GATEWAY_TOKEN, settings.BACKEND_TO_GRAPH_TOKEN,
              settings.GRAPH_DELEGATION_SECRET)
    if any(len(value) < 32 for value in values) or len(set(values)) != 3:
        raise HTTPException(503, "gateway_configuration_required")


def fingerprint(question, datasource_id):
    return hashlib.sha256((str(datasource_id) + "\n" + question).encode()).hexdigest()


def metric_datasource_enabled(datasource_id: int):
    enabled()
    try:
        allowed = {int(value.strip()) for value in settings.GRAPH_METRIC_DATASOURCES.split(",") if value.strip()}
    except ValueError:
        raise HTTPException(503, "metric_datasource_configuration_invalid") from None
    if datasource_id not in allowed:
        raise HTTPException(403, "metric_datasource_not_allowed")


def issue_delegation(uid, oid, run_id, question, datasource_id):
    enabled()
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": ["adaptive-graph", "adaptive-gateway"],
                       "sub": str(uid), "workspace": str(oid), "run_id": str(run_id),
                       "model_id": settings.GRAPH_MODEL_ID, "datasource_id": datasource_id,
                       "scope": SCOPES, "purpose": "synthetic-question",
                       "request_hash": fingerprint(question, datasource_id),
                       "iat": now, "exp": now + 120},
                      settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")


def issue_metric_delegation(uid, oid, run_id, question, datasource_id):
    metric_datasource_enabled(datasource_id)
    now = int(time.time())
    return jwt.encode({"iss": ISSUER, "aud": ["adaptive-graph", "adaptive-gateway"],
                       "sub": str(uid), "workspace": str(oid), "run_id": str(run_id),
                       "model_id": settings.GRAPH_MODEL_ID, "datasource_id": datasource_id,
                       "scope": METRIC_SCOPES, "purpose": "metric-plan",
                       "request_hash": fingerprint(question, datasource_id),
                       "iat": now, "exp": now + 120},
                      settings.GRAPH_DELEGATION_SECRET, algorithm="HS256")


def verify_request(request: Request, body):
    enabled()
    supplied = request.headers.get("X-Graph-Service", "")
    if not hmac.compare_digest(supplied, settings.GRAPH_TO_GATEWAY_TOKEN):
        raise HTTPException(401, "invalid_service_identity")
    try:
        claims = jwt.decode(request.headers.get("X-Graph-Delegation", ""),
                            settings.GRAPH_DELEGATION_SECRET, algorithms=["HS256"],
                            audience="adaptive-gateway", issuer=ISSUER,
                            options={"require": ["sub", "workspace", "run_id", "model_id", "scope",
                                                 "purpose", "request_hash", "datasource_id", "iat", "exp"]})
        valid = (claims["purpose"] == "synthetic-question" and claims["scope"] == SCOPES
                 and claims["datasource_id"] == body.datasource_id == "synthetic-sales"
                 and claims["run_id"] == str(body.run_id)
                 and claims["model_id"] == settings.GRAPH_MODEL_ID
                 and claims["request_hash"] == fingerprint(body.question, body.datasource_id)
                 and 0 < claims["exp"] - claims["iat"] <= 120
                 and int(claims["sub"]) > 0 and int(claims["workspace"]) > 0)
        if not valid:
            raise ValueError("invalid_claims")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "invalid_delegation") from None
    return claims


def verify_metric_request(request: Request, body):
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
                                                 "purpose", "request_hash", "datasource_id", "iat", "exp"]})
        valid = (claims["purpose"] == "metric-plan" and claims["scope"] == METRIC_SCOPES
                 and type(claims["datasource_id"]) is int
                 and claims["datasource_id"] == body.datasource_id
                 and claims["run_id"] == str(body.run_id)
                 and type(claims["model_id"]) is int
                 and claims["model_id"] == settings.GRAPH_MODEL_ID
                 and claims["request_hash"] == fingerprint(body.question, body.datasource_id)
                 and 0 < claims["exp"] - claims["iat"] <= 120
                 and int(claims["sub"]) > 0 and int(claims["workspace"]) > 0)
        if not valid:
            raise ValueError("invalid_claims")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "invalid_delegation") from None
    return claims

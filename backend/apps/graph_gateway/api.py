from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from apps.graph_gateway.contracts import InternalQuestion, QuestionRequest, SafeResponse
from apps.graph_gateway.security import enabled, issue_delegation, verify_request
from apps.graph_gateway.service import authorize_current, invoke_model
from common.core.config import settings

class SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                raise HTTPException(422, "invalid_request") from None
        return handler


router = APIRouter(tags=["graph-experiment"], route_class=SafeValidationRoute)


@router.post("/analysis/query", response_model=SafeResponse)
async def query(body: QuestionRequest, request: Request):
    enabled()
    user = getattr(request.state, "current_user", None)
    # Login session only, never assistant or API-key identities for this experiment.
    if not user or not request.headers.get(settings.TOKEN_KEY):
        raise HTTPException(401, "login_required")
    if any(request.headers.get(key) for key in (settings.ASSISTANT_TOKEN_KEY, "X-SQLBOT-ASK-TOKEN", "X-SQLBOT-API-KEY")):
        raise HTTPException(403, "login_required")
    authorize_current(user.id, user.oid)
    run_id = uuid4()
    token = issue_delegation(user.id, user.oid, run_id, body.question, body.datasource_id)
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False, trust_env=False) as client:
            response = await client.post(settings.GRAPH_SERVICE_URL.rstrip("/") + "/internal/v1/query",
                                         json={**body.model_dump(), "run_id": str(run_id)},
                                         headers={"X-Graph-Service": settings.BACKEND_TO_GRAPH_TOKEN,
                                                  "X-Graph-Delegation": token})
        response.raise_for_status()
        result = SafeResponse.model_validate(response.json())
        if result.run_id != run_id:
            raise ValueError("run_mismatch")
        authorize_current(user.id, user.oid)
        return result
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "graph_timeout") from None
    except Exception:
        raise HTTPException(502, "graph_unavailable") from None


@router.post("/internal/graph/authorize")
async def authorize(body: InternalQuestion, request: Request):
    claims = verify_request(request, body)
    authorize_current(int(claims["sub"]), int(claims["workspace"]))
    return {"authorized": True}


@router.post("/internal/graph/model")
async def model(body: InternalQuestion, request: Request):
    claims = verify_request(request, body)
    return await invoke_model(claims, body.question)

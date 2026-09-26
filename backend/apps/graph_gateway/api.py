from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from apps.graph_gateway.contracts import (
    InternalMetricQuestion,
    InternalQuestion,
    MetricCompileRequest,
    MetricExecuteRequest,
    MetricModelRequest,
    MetricPlanResponse,
    MetricQueryResponse,
    MetricQuestionRequest,
    QuestionRequest,
    SafeResponse,
)
from apps.graph_gateway.security import (
    enabled,
    issue_delegation,
    issue_metric_delegation,
    issue_metric_query_delegation,
    metric_datasource_enabled,
    request_budget_seconds,
    verify_metric_request,
    verify_request,
)
from apps.graph_gateway.service import (
    authorize_current,
    authorized_metric_candidates,
    compile_authorized_metric_plan,
    execute_authorized_metric_plan,
    invoke_metric_model,
    invoke_model,
    run_metric_query,
)
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


def login_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user or not request.headers.get(settings.TOKEN_KEY):
        raise HTTPException(401, "login_required")
    if any(request.headers.get(key) for key in
           (settings.ASSISTANT_TOKEN_KEY, "X-SQLBOT-ASK-TOKEN", "X-SQLBOT-API-KEY")):
        raise HTTPException(403, "login_required")
    return user


@router.post("/analysis/query", response_model=SafeResponse)
async def query(body: QuestionRequest, request: Request):
    enabled()
    user = login_user(request)
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


@router.post("/analysis/metrics/plan", response_model=MetricPlanResponse)
async def metric_plan(body: MetricQuestionRequest, request: Request):
    user = login_user(request)
    metric_datasource_enabled(body.datasource_id)
    authorize_current(user.id, user.oid)
    run_id = uuid4()
    token = issue_metric_delegation(user.id, user.oid, run_id, body.question, body.datasource_id,
                                    body.context)
    try:
        async with httpx.AsyncClient(timeout=request_budget_seconds(), follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.post(
                settings.GRAPH_SERVICE_URL.rstrip("/") + "/internal/v1/metrics/plan",
                json={**body.model_dump(), "run_id": str(run_id)},
                headers={"X-Graph-Service": settings.BACKEND_TO_GRAPH_TOKEN,
                         "X-Graph-Delegation": token},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid_response")
        # The public boundary is an explicit allowlist even if the graph service
        # accidentally adds internal plan, prompt, formula or SQL fields.
        result = MetricPlanResponse.model_validate({
            key: value for key, value in payload.items()
            if key in MetricPlanResponse.model_fields
        })
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


@router.post("/analysis/metrics/query", response_model=MetricQueryResponse)
async def metric_query(body: MetricQuestionRequest, request: Request):
    user = login_user(request)
    return await run_metric_query(user.id, user.oid, body.question, body.datasource_id, body.context)


@router.post("/internal/graph/authorize")
async def authorize(body: InternalQuestion, request: Request):
    claims = verify_request(request, body)
    authorize_current(int(claims["sub"]), int(claims["workspace"]))
    return {"authorized": True}


@router.post("/internal/graph/model")
async def model(body: InternalQuestion, request: Request):
    claims = verify_request(request, body)
    return await invoke_model(claims, body.question)


@router.post("/internal/graph/metrics/authorize")
async def metric_authorize(body: InternalMetricQuestion, request: Request):
    claims = verify_metric_request(request, body)
    authorize_current(int(claims["sub"]), int(claims["workspace"]))
    return {"authorized": True}


@router.post("/internal/graph/metrics/candidates")
async def metric_candidates(body: InternalMetricQuestion, request: Request):
    claims = verify_metric_request(request, body)
    return {"candidates": authorized_metric_candidates(
        claims, body.question, body.datasource_id, body.context
    )}


@router.post("/internal/graph/metrics/model")
async def metric_model(body: MetricModelRequest, request: Request):
    claims = verify_metric_request(request, body)
    return await invoke_metric_model(
        claims, body.question, body.datasource_id, body.candidates, body.context,
        [repair.model_dump() for repair in body.repairs],
    )


@router.post("/internal/graph/metrics/compile")
async def metric_compile(body: MetricCompileRequest, request: Request):
    claims = verify_metric_request(request, body)
    return compile_authorized_metric_plan(
        claims, body.question, body.datasource_id, body.plan
    )


@router.post("/internal/graph/metrics/execute")
async def metric_execute(body: MetricExecuteRequest, request: Request):
    # Only the plan-and-execute delegation may trigger customer-database queries.
    claims = verify_metric_request(request, body, purposes=frozenset({"metric-query"}))
    return await execute_authorized_metric_plan(
        claims, body.question, body.datasource_id, body.plan
    )

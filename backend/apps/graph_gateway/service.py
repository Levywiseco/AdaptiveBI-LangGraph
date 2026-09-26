import asyncio
import json
import logging
import time
from time import perf_counter
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from apps.ai_model.model_factory import LLMFactory, get_default_config
from apps.datasource.models.datasource import CoreDatasource
from apps.system.crud.aimodel_manage import get_ai_model_list_by_workspace
from apps.system.models.system_model import AiModelDetail, UserWsModel, WorkspaceModel
from apps.system.models.user import UserModel
from common.core.config import settings
from common.core.db import engine
from apps.graph_gateway.contracts import MetricQueryResponse
from apps.graph_gateway.security import (
    configured_ids,
    enabled,
    issue_metric_query_delegation,
    metric_datasource_enabled,
    request_budget_seconds,
)
from apps.graph_gateway.model_policy import gateway_model_config
from apps.graph_gateway.prompts import metric_planning_messages, planning_now
from apps.metrics.crud.metric import get_metric_candidates
from apps.metrics.schemas.metric import MetricQueryPlanRequest
from apps.metrics.service.query_planner import preview_metric_query_plan
from apps.system.schemas.system_schema import UserInfoDTO

MODEL_SLOTS = asyncio.Semaphore(4)
EXECUTION_SLOTS = asyncio.Semaphore(4)
PLAN_KEYS = {"metric_id", "metric_version_id", "dimensions", "filters", "time_range", "limit"}
MODEL_STEP_SECONDS = 30
MIN_STEP_SECONDS = 0.5
SCHEMA = (
    "sales(id INTEGER, month TEXT, region TEXT, gross INTEGER, refund INTEGER); net = gross - refund. "
    "month stores YYYY-MM text, for example '2026-08' (August 2026), not full dates. "
    "region stores 'east' (East / 东部) or 'west' (West / 西部)."
)


def authorize_current(uid: int, oid: int):
    enabled()
    try:
        users = configured_ids(settings.GRAPH_TEST_USERS)
        workspaces = configured_ids(settings.GRAPH_TEST_WORKSPACES)
    except ValueError:
        # A malformed allowlist fails closed instead of matching partial entries.
        raise HTTPException(503, "gateway_configuration_required") from None
    if uid not in users or oid not in workspaces:
        raise HTTPException(403, "experiment_not_allowed")
    # Never trust the cached login DTO for revocation-sensitive authorization.
    with Session(engine) as session:
        user = session.get(UserModel, uid)
        if not user or user.status != 1 or user.oid != oid:
            raise HTTPException(403, "identity_not_allowed")
        if not session.get(WorkspaceModel, oid):
            raise HTTPException(403, "workspace_not_allowed")
        membership = session.exec(select(UserWsModel).where(UserWsModel.uid == uid, UserWsModel.oid == oid)).first()
        if not membership and not (user.id == 1 and user.account == "admin"):
            raise HTTPException(403, "workspace_not_allowed")
        available = get_ai_model_list_by_workspace(session, oid)
        if settings.GRAPH_MODEL_ID not in {model.id for model in available}:
            raise HTTPException(403, "model_not_allowed")
        model = session.get(AiModelDetail, settings.GRAPH_MODEL_ID)
        if not model or model.status != 1 or model.protocol != 1:
            raise HTTPException(403, "model_not_allowed")


def step_timeout(claims, cap: float) -> float:
    """Timeout for one step: its own cap, never beyond the run deadline."""
    deadline = claims.get("deadline")
    if deadline is None:
        return cap
    remaining = deadline - time.time()
    if remaining <= MIN_STEP_SECONDS:
        raise HTTPException(504, "graph_deadline_exceeded")
    return min(cap, remaining)


def usage_from(message):
    raw = getattr(message, "usage_metadata", None) or {}
    if not raw:
        legacy = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
        raw = {"input_tokens": legacy.get("prompt_tokens"), "output_tokens": legacy.get("completion_tokens"),
               "total_tokens": legacy.get("total_tokens")}
    return {key: raw[key] if type(raw.get(key)) is int and raw[key] >= 0 else None
            for key in ("input_tokens", "output_tokens", "total_tokens")}


def _current_user(session: Session, uid: int, oid: int) -> UserInfoDTO:
    user = session.get(UserModel, uid)
    if not user or user.status != 1 or user.oid != oid:
        raise HTTPException(403, "identity_not_allowed")
    result = UserInfoDTO.model_validate(user.model_dump())
    result.isAdmin = result.id == 1 and result.account == "admin"
    if not result.isAdmin:
        membership = session.exec(
            select(UserWsModel).where(UserWsModel.uid == uid, UserWsModel.oid == oid)
        ).first()
        result.weight = membership.weight if membership else -1
    return result


def authorized_metric_candidates(claims, question: str, datasource_id: int):
    uid, oid = int(claims["sub"]), int(claims["workspace"])
    authorize_current(uid, oid)
    with Session(engine) as session:
        current_user = _current_user(session, uid, oid)
        return get_metric_candidates(
            session, question, oid, datasource_id, limit=10, current_user=current_user
        )


async def invoke_metric_model(claims, question: str, datasource_id: int, candidate_refs):
    candidates = authorized_metric_candidates(claims, question, datasource_id)
    requested = {(item.metric_id, item.metric_version_id) for item in candidate_refs}
    if len(requested) != len(candidate_refs):
        raise HTTPException(422, "metric_candidates_invalid")
    selected = [item for item in candidates
                if (item["metric_id"], item["metric_version_id"]) in requested]
    if not selected or len(selected) != len(requested):
        raise HTTPException(403, "metric_not_authorized")
    uid, oid = int(claims["sub"]), int(claims["workspace"])
    messages = metric_planning_messages(question, selected, planning_now())
    step_timeout(claims, MODEL_STEP_SECONDS)  # refuse to start a call that cannot finish
    try:
        await asyncio.wait_for(MODEL_SLOTS.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "gateway_busy") from None
    calls = 0
    started = perf_counter()
    provider_family = None
    usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    try:
        config = await get_default_config(settings.GRAPH_MODEL_ID)
        if config.model_id != claims["model_id"]:
            raise HTTPException(403, "model_not_allowed")
        config, policy = gateway_model_config(config)
        provider_family = policy.family
        model = LLMFactory.create_llm(config).llm
        authorize_current(uid, oid)
        timeout = step_timeout(claims, MODEL_STEP_SECONDS)
        calls = 1
        message = await asyncio.wait_for(model.ainvoke(messages), timeout=timeout)
        usage = usage_from(message)
        authorize_current(uid, oid)
        content = message.content
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            return {"error": "model_output_invalid", "usage": usage, "model_calls": calls}
        return {"content": content, "usage": usage, "model_calls": calls}
    except HTTPException:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        return {"error": "model_timeout", "usage": usage, "model_calls": calls}
    except Exception as exc:
        error = "model_timeout" if "timeout" in type(exc).__name__.lower() else "model_call_failed"
        return {"error": error, "usage": usage, "model_calls": calls}
    finally:
        logging.getLogger("adaptive.graph_gateway").info("metric_model_usage %s", json.dumps({
            "run_id": claims.get("run_id"), "model_config_id": claims["model_id"],
            "provider_family": provider_family, "model_calls": calls, "usage": usage,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }))
        MODEL_SLOTS.release()


def _validated_plan_request(plan: dict) -> tuple[int, int, MetricQueryPlanRequest]:
    """Re-validate the graph-service plan shape; the plan is never trusted beyond these keys."""
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS:
        raise HTTPException(422, "metric_plan_invalid")
    metric_id = plan.get("metric_id")
    version_id = plan.get("metric_version_id")
    if (type(metric_id) is not int or metric_id <= 0
            or type(version_id) is not int or version_id <= 0):
        raise HTTPException(422, "metric_plan_invalid")
    try:
        payload = MetricQueryPlanRequest.model_validate({
            "version_id": version_id,
            "dimensions": plan.get("dimensions") or [],
            "filters": plan.get("filters") or [],
            "time_range": plan.get("time_range"),
            "limit": plan.get("limit"),
        })
    except Exception:
        raise HTTPException(422, "metric_plan_invalid") from None
    return metric_id, version_id, payload


def compile_authorized_metric_plan(claims, question: str, datasource_id: int, plan: dict):
    metric_id, version_id, payload = _validated_plan_request(plan)
    uid, oid = int(claims["sub"]), int(claims["workspace"])
    authorize_current(uid, oid)
    with Session(engine) as session:
        current_user = _current_user(session, uid, oid)
        compiled = preview_metric_query_plan(session, metric_id, payload, oid, current_user)
    authorize_current(uid, oid)
    if compiled["datasource_id"] != datasource_id or compiled["metric_version_id"] != version_id:
        raise HTTPException(403, "metric_not_authorized")
    return {key: compiled.get(key) for key in (
        "metric_id", "metric_code", "metric_name", "metric_version_id", "metric_version",
        "dimensions", "time_range", "sql_fingerprint", "compiler",
    )}


async def execute_authorized_metric_plan(claims, question: str, datasource_id: int, plan: dict):
    """Compile the published version again, then run one read-only bounded query.

    SQL never leaves this process: the graph service only supplies the validated plan.
    """
    metric_id, version_id, payload = _validated_plan_request(plan)
    uid, oid = int(claims["sub"]), int(claims["workspace"])
    authorize_current(uid, oid)
    with Session(engine) as session:
        current_user = _current_user(session, uid, oid)
        compiled = preview_metric_query_plan(session, metric_id, payload, oid, current_user)
        datasource = session.get(CoreDatasource, compiled["datasource_id"])
    # Re-check permissions after compilation; a revoked session stops before execution.
    authorize_current(uid, oid)
    if (compiled["datasource_id"] != datasource_id or compiled["metric_version_id"] != version_id
            or not datasource or datasource.oid != oid):
        raise HTTPException(403, "metric_not_authorized")
    # Never start a query the caller can no longer wait for.
    timeout = step_timeout(claims, settings.GRAPH_METRIC_EXECUTION_TIMEOUT)
    try:
        await asyncio.wait_for(EXECUTION_SLOTS.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "gateway_busy") from None
    started = perf_counter()
    try:
        try:
            # Imported lazily: apps.db.db pulls driver modules and xpack crypto.
            # main.py loads sqlbot_xpack first in production; keep that order here
            # so the legacy circular import inside apps.system.crud.assistant resolves.
            import sqlbot_xpack  # noqa: F401
            from apps.db.db import exec_sql
            # exec_sql rejects non-read statements; the worker thread cannot be cancelled
            # on timeout, so the overrun is logged instead of silently dropped.
            raw = await asyncio.wait_for(
                asyncio.to_thread(exec_sql, datasource, compiled["sql"]),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logging.getLogger("adaptive.graph_gateway").warning(
                "metric_execution_timeout %s", json.dumps({
                    "run_id": claims.get("run_id"), "metric_id": metric_id,
                    "timeout_s": round(timeout, 3),
                    "background_state": "unknown",
                }))
            raise HTTPException(504, "metric_execution_timeout") from None
        except Exception:
            raise HTTPException(502, "metric_execution_failed") from None
    finally:
        EXECUTION_SLOTS.release()
    rows = raw.get("data") or []
    max_rows = settings.GRAPH_METRIC_MAX_ROWS
    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    elapsed_ms = round((perf_counter() - started) * 1000, 2)
    logging.getLogger("adaptive.graph_gateway").info("metric_execution_usage %s", json.dumps({
        "run_id": claims.get("run_id"), "metric_id": metric_id,
        "row_count": len(rows), "truncated": truncated, "elapsed_ms": elapsed_ms,
    }))
    return {
        "metric_id": compiled["metric_id"],
        "sql_fingerprint": compiled["sql_fingerprint"],
        "columns": raw.get("fields") or [],
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": elapsed_ms,
    }


async def invoke_model(claims, question):
    from langchain_core.messages import HumanMessage, SystemMessage
    uid, oid = int(claims["sub"]), int(claims["workspace"])
    authorize_current(uid, oid)
    try:
        await asyncio.wait_for(MODEL_SLOTS.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        raise HTTPException(503, "gateway_busy") from None
    started = perf_counter()
    calls = 0
    provider_family = None
    usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    try:
        config = await get_default_config(settings.GRAPH_MODEL_ID)
        if config.model_id != claims["model_id"]:
            raise HTTPException(403, "model_not_allowed")
        # Apply a bounded provider policy instead of forwarding arbitrary saved
        # request options into this security-sensitive execution path.
        config, policy = gateway_model_config(config)
        provider_family = policy.family
        model = LLMFactory.create_llm(config).llm
        authorize_current(uid, oid)
        calls = 1
        message = await asyncio.wait_for(model.ainvoke([
            SystemMessage(content="Return exactly one SQLite SELECT statement, no markdown, comments or explanation. "
                                  "Only this synthetic schema is available: " + SCHEMA),
            HumanMessage(content=question),
        ]), timeout=30)
        usage = usage_from(message)
        authorize_current(uid, oid)
        content = message.content
        if not isinstance(content, str) or not content.strip() or len(content) > 16000:
            return {"error": "model_output_invalid", "usage": usage, "model_calls": calls}
        return {"content": content, "usage": usage, "model_calls": calls,
                "elapsed_ms": round((perf_counter() - started) * 1000, 2)}
    except HTTPException:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        return {"error": "model_timeout", "usage": usage, "model_calls": calls}
    except Exception as exc:
        # SDK timeouts can use provider-specific exception classes.
        error = "model_timeout" if "timeout" in type(exc).__name__.lower() else "model_call_failed"
        return {"error": error, "usage": usage, "model_calls": calls}
    finally:
        logging.getLogger("adaptive.graph_gateway").info("graph_model_usage %s", json.dumps({
            "run_id": claims.get("run_id"), "model_config_id": claims["model_id"],
            "provider_family": provider_family,
            "model_calls": calls, "usage": usage,
            "elapsed_ms": round((perf_counter() - started) * 1000, 2),
        }))
        MODEL_SLOTS.release()


async def run_metric_query(uid: int, oid: int, question: str, datasource_id: int) -> MetricQueryResponse:
    """Plan and execute one governed metric query through the graph service.

    Shared by the analysis endpoint and the chat-engine router; the response is
    an explicit allowlist so leaked internal fields cannot pass through.
    """
    metric_datasource_enabled(datasource_id)
    authorize_current(uid, oid)
    run_id = uuid4()
    token = issue_metric_query_delegation(uid, oid, run_id, question, datasource_id)
    try:
        async with httpx.AsyncClient(timeout=request_budget_seconds(), follow_redirects=False,
                                     trust_env=False) as client:
            response = await client.post(
                settings.GRAPH_SERVICE_URL.rstrip("/") + "/internal/v1/metrics/query",
                json={"question": question, "datasource_id": datasource_id, "run_id": str(run_id)},
                headers={"X-Graph-Service": settings.BACKEND_TO_GRAPH_TOKEN,
                         "X-Graph-Delegation": token},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid_response")
        result = MetricQueryResponse.model_validate({
            key: value for key, value in payload.items()
            if key in MetricQueryResponse.model_fields
        })
        if result.run_id != run_id:
            raise ValueError("run_mismatch")
        authorize_current(uid, oid)
        return result
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "graph_timeout") from None
    except Exception:
        raise HTTPException(502, "graph_unavailable") from None

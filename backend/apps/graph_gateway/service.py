import asyncio
import json
import logging
from time import perf_counter

from fastapi import HTTPException
from sqlmodel import Session, select

from apps.ai_model.model_factory import LLMFactory, get_default_config
from apps.system.crud.aimodel_manage import get_ai_model_list_by_workspace
from apps.system.models.system_model import AiModelDetail, UserWsModel, WorkspaceModel
from apps.system.models.user import UserModel
from common.core.config import settings
from common.core.db import engine
from apps.graph_gateway.security import enabled
from apps.graph_gateway.model_policy import gateway_model_config

MODEL_SLOTS = asyncio.Semaphore(4)
SCHEMA = (
    "sales(id INTEGER, month TEXT, region TEXT, gross INTEGER, refund INTEGER); net = gross - refund. "
    "month stores YYYY-MM text, for example '2026-08' (August 2026), not full dates. "
    "region stores 'east' (East / 东部) or 'west' (West / 西部)."
)


def authorize_current(uid: int, oid: int):
    enabled()
    if (str(uid) not in settings.GRAPH_TEST_USERS.split(",")
            or str(oid) not in settings.GRAPH_TEST_WORKSPACES.split(",")):
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


def usage_from(message):
    raw = getattr(message, "usage_metadata", None) or {}
    if not raw:
        legacy = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
        raw = {"input_tokens": legacy.get("prompt_tokens"), "output_tokens": legacy.get("completion_tokens"),
               "total_tokens": legacy.get("total_tokens")}
    return {key: raw[key] if type(raw.get(key)) is int and raw[key] >= 0 else None
            for key in ("input_tokens", "output_tokens", "total_tokens")}


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

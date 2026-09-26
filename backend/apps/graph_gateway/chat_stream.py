"""Chat-engine grayscale routing.

When CHAT_ENGINE=langgraph and the workspace is whitelisted, an interactive
chat question is answered by the governed metric graph and mapped onto the
legacy SSE event contract so the existing frontend renders it unchanged.

Phase-1 boundary: the graph runs to completion first, then the result is
mapped to the legacy event sequence (id, question, datasource, sql-result,
info, brief, sql, sql-data, chart, finish). Token-level streaming, chart
recommendation and result explanation come with the later streaming work.
SQL never enters this layer: the displayed "SQL" is a governed-query summary
with the fingerprint, per the metric contract.
"""
import logging
from typing import Any, Optional

import orjson
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlmodel import Session, select

from apps.chat.curd.chat import (
    finish_record,
    rename_chat,
    save_chart,
    save_error_message,
    save_question,
    save_sql,
    save_sql_exec_data,
)
from apps.chat.models.chat_model import Chat, ChatQuestion, ChatRecord, RenameChat
from apps.datasource.models.datasource import CoreDatasource
from apps.graph_gateway.contracts import MetricQueryResponse
from apps.graph_gateway.security import (
    configured_ids,
    enabled,
    metric_datasource_enabled,
)
from apps.graph_gateway.service import authorize_current, run_metric_query
from common.core.config import settings

_ENGINE_ERROR_MESSAGES = {
    "metric_not_found": "未匹配到已发布的指标，请先在指标库发布相关指标",
    "metric_plan_invalid": "模型生成的指标计划无效，请重试或换个问法",
    "metric_dimension_not_allowed": "指标计划引用了未声明维度，请重试或换个问法",
    "metric_filter_not_allowed": "指标计划引用了不允许的过滤字段，请重试或换个问法",
    "metric_time_range_not_allowed": "该指标不支持所请求的时间范围",
    "metric_not_authorized": "当前账号无权访问该指标",
    "metric_compile_failed": "指标编译失败，指标版本可能已变更，请重试",
    "metric_execution_failed": "指标查询执行失败，请稍后重试",
    "metric_execution_timeout": "指标查询执行超时，请稍后重试",
    "model_timeout": "模型调用超时，请稍后重试",
    "model_call_failed": "模型调用失败，请稍后重试",
    "model_output_invalid": "模型输出无效，请重试",
    "gateway_unavailable": "图服务暂不可用，请稍后重试",
    "gateway_rejected": "请求被拒绝，请重新登录后重试",
    "graph_timeout": "图服务响应超时，请稍后重试",
    "graph_unavailable": "图服务暂不可用，请稍后重试",
    "graph_execution_failed": "图执行失败，请稍后重试",
    "graph_deadline_exceeded": "问答总耗时超出上限，请稍后重试或缩小查询范围",
}
_FALLBACK_ERROR_MESSAGE = "指标查询失败，请稍后重试"


def _engine_error_message(code: Optional[str]) -> str:
    return _ENGINE_ERROR_MESSAGES.get(code or "", _FALLBACK_ERROR_MESSAGE)


def _whitelisted_workspaces() -> Optional[set[int]]:
    try:
        return configured_ids(settings.CHAT_ENGINE_WORKSPACES)
    except ValueError:
        # A malformed whitelist fails closed: every chat stays on the legacy engine.
        return None


def graph_engine_enabled(current_user, chat: Optional[Chat]) -> bool:
    """Every gate must pass before one chat question moves to the graph engine.

    Routing uses the same authorization the graph run enforces, so a routed
    question never fails merely because the user is outside the experiment.
    """
    if settings.CHAT_ENGINE != "langgraph" or chat is None or not chat.datasource:
        return False
    workspaces = _whitelisted_workspaces()
    if workspaces is None or current_user.oid not in workspaces:
        return False
    if chat.oid is not None and chat.oid != current_user.oid:
        return False
    try:
        enabled()
        metric_datasource_enabled(chat.datasource)
        authorize_current(current_user.id, current_user.oid)
    except HTTPException:
        return False
    return True


def _is_first_question(session: Session, chat_id: int, record_id: int) -> bool:
    """True when no earlier question exists; the datasource placeholder record is ignored."""
    earlier = session.exec(
        select(ChatRecord.id).where(
            ChatRecord.chat_id == chat_id,
            ChatRecord.id != record_id,
            or_(ChatRecord.first_chat.is_(None), ChatRecord.first_chat.is_(False)),
        ).limit(1)
    ).first()
    return earlier is None


CONTEXT_QUESTIONS = 3
_CLARIFICATION_PREFIX = {"ambiguous": "需要确认：", "unsupported": "暂时无法回答："}


def _conversation_context(session: Session, chat_id: int, record_id: int) -> list[str]:
    """Up to three earlier questions of this chat, oldest first, for follow-ups."""
    rows = session.exec(
        select(ChatRecord.question).where(
            ChatRecord.chat_id == chat_id,
            ChatRecord.id < record_id,
            or_(ChatRecord.first_chat.is_(None), ChatRecord.first_chat.is_(False)),
            ChatRecord.analysis_record_id.is_(None),
            ChatRecord.predict_record_id.is_(None),
        ).order_by(ChatRecord.id.desc()).limit(CONTEXT_QUESTIONS)
    ).all()
    return [question.strip()[:2000] for question in reversed(rows) if question and question.strip()]


def _sse(payload: dict[str, Any]) -> str:
    return "data:" + orjson.dumps(payload).decode() + "\n\n"


def _numeric_columns(columns: list[str], rows: list[dict]) -> list[dict]:
    sample = next((row for row in rows if row), {})
    return [{"name": column,
             "is_numeric": isinstance(sample.get(column), (int, float)) and not isinstance(sample.get(column), bool)}
            for column in columns]


def _governed_summary(result: MetricQueryResponse) -> str:
    lines = [
        "-- 受治理指标查询（图服务执行，未走自由 SQL 生成）",
        f"-- 编译器: {result.compiler}  SQL指纹: {(result.sql_fingerprint or '')[:16]}",
        f"-- 指标: {result.metric_code or result.metric_id} {result.metric_name or ''}"
        f"  版本 v{result.metric_version}",
    ]
    if result.dimensions:
        lines.append("-- 维度: " + ", ".join(result.dimensions))
    if result.time_range:
        lines.append(f"-- 时间范围: {result.time_range.get('start')} ~ {result.time_range.get('end')}")
    suffix = "（超出行数上限，已截断）" if result.truncated else ""
    lines.append(f"-- 返回 {result.row_count} 行{suffix}")
    if result.unit:
        lines.append(f"-- 单位: {result.unit}")
    return "\n".join(lines)


async def maybe_stream_graph_answer(session: Session, current_user, request_question: ChatQuestion,
                                    in_chat: bool = True, current_assistant: Any = None,
                                    stream: bool = True):
    """Return a legacy-format SSE response, or None to stay on the legacy engine.

    Only the interactive, streaming web chat is routed: MCP (in_chat=False),
    embedded assistants and non-streaming callers stay on the legacy engine.
    All persistence happens eagerly while the request session is alive; the
    generator only replays pre-built events.
    """
    if not in_chat or current_assistant is not None or not stream:
        return None
    chat = session.get(Chat, request_question.chat_id) if request_question.chat_id else None
    if not graph_engine_enabled(current_user, chat):
        return None

    record = save_question(session=session, current_user=current_user, question=request_question)
    datasource = session.get(CoreDatasource, chat.datasource)
    logging.getLogger("adaptive.graph_gateway").info(
        "chat_engine_routed %s", orjson.dumps({
            "engine": "langgraph", "record_id": record.id, "user_id": current_user.id,
            "workspace": current_user.oid, "datasource_id": chat.datasource,
        }).decode())

    events = [
        _sse({"type": "id", "id": record.id}),
    ]
    if record.regenerate_record_id:
        events.append(_sse({"type": "regenerate_record_id",
                            "regenerate_record_id": record.regenerate_record_id}))
    events.append(_sse({"type": "question", "question": record.question}))
    events.append(_sse({"id": datasource.id, "datasource_name": datasource.name,
                        "engine_type": datasource.type_name or datasource.type,
                        "type": "datasource"}))

    try:
        context = _conversation_context(session, chat.id, record.id)
        result = await run_metric_query(current_user.id, current_user.oid,
                                        request_question.question, chat.datasource, context)
        if result.status == "needs_clarification" and result.clarification:
            # Shown as the answer text (the legacy UI renders plain record errors
            # as normal text); the user's reply arrives as the next follow-up.
            graph_error = _CLARIFICATION_PREFIX.get(result.clarification_reason, "") + result.clarification
        else:
            graph_error = None if result.status == "completed" else _engine_error_message(result.error)
    except HTTPException as exc:
        result = None
        graph_error = _engine_error_message(exc.detail if isinstance(exc.detail, str) else None)

    if graph_error is not None:
        save_error_message(session=session, record_id=record.id, message=graph_error)
        events.append(_sse({"content": graph_error, "type": "error"}))
        return StreamingResponse(iter(events), media_type="text/event-stream")

    summary = _governed_summary(result)
    data = orjson.dumps({
        "fields": result.columns,
        "data": result.rows,
        "fields_info": _numeric_columns(result.columns, result.rows),
        "datasource": chat.datasource,
    }).decode()
    chart = orjson.dumps({
        "type": "table",
        "columns": [{"name": column, "value": column.lower()} for column in result.columns],
    }).decode()

    save_sql(session=session, record_id=record.id, sql=summary)
    save_sql_exec_data(session=session, record_id=record.id, data=data)
    save_chart(session=session, record_id=record.id, chart=chart)
    brief_source = (request_question.question or "").strip()
    # Name the chat after its first question only, and leave a model-generated
    # brief alone, so later questions never overwrite the conversation title.
    if brief_source and not chat.brief_generate and _is_first_question(session, chat.id, record.id):
        brief = rename_chat(session=session,
                            rename_object=RenameChat(id=chat.id, brief=brief_source[:20],
                                                     brief_generate=False))
        events.append(_sse({"type": "brief", "brief": brief}))
    finish_record(session=session, record_id=record.id)

    events.extend([
        _sse({"content": summary, "reasoning_content": None, "type": "sql-result"}),
        _sse({"type": "info", "msg": "sql generated"}),
        _sse({"content": summary, "type": "sql"}),
        _sse({"content": "execute-success", "type": "sql-data"}),
        _sse({"content": chart, "type": "chart"}),
        _sse({"type": "finish"}),
    ])
    return StreamingResponse(iter(events), media_type="text/event-stream")

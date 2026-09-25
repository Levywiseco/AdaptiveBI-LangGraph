"""Metric planning prompt.

This is the only copy of the planning prompt: the graph service sends candidate
references, and the gateway rebuilds the prompt from the revalidated snapshot.
The rules below must stay aligned with the metric-plan-v1 compiler, which
filters time ranges as ``start <= time_field < end``.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from common.core.config import settings

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_RULES = """You translate one business question into a governed metric query plan.
Return exactly one JSON object and nothing else: no markdown, no code fences, no SQL, no explanation.
Required keys: metric_id, metric_version_id, dimensions, filters, time_range, limit.

Rules:
1. metric_id and metric_version_id must be copied together from one candidate below.
2. dimensions: [] or a subset of that candidate's "dimensions" (group-by fields).
3. filters: [] or objects {{"field": ..., "operator": ..., "value": ...}}. field must be one of the
   candidate's dimensions or its time_field. operator is one of =, !=, >, >=, <, <=, in, not_in,
   between, like, not_like, is_null, is_not_null. in/not_in take a JSON array; between takes [low, high].
4. time_range: null when the question has no time constraint. Otherwise {{"start": ..., "end": ...}}
   applied to the candidate's time_field; a candidate whose time_field is null cannot take a time_range.
   - Use ISO 8601 local datetimes without a timezone offset, e.g. "2026-08-01T00:00:00".
   - The range is start-inclusive and END-EXCLUSIVE: end is the first instant AFTER the period.
     "2026年8月" -> start "2026-08-01T00:00:00", end "2026-09-01T00:00:00".
     "2026年" -> start "2026-01-01T00:00:00", end "2027-01-01T00:00:00".
     The single day 2026-08-15 -> start "2026-08-15T00:00:00", end "2026-08-16T00:00:00".
   - Resolve relative periods (今天, 昨天, 本周, 上周, 本月, 上个月, 本季度, 今年, 去年, 近N天, 最近N个月)
     against the current date below. Weeks start on Monday. 近N天 includes today:
     start is N-1 days before today at 00:00, end is tomorrow at 00:00.
   - A month or quarter named without a year means the most recent one that has already started.
5. limit: null unless the question asks for the top/bottom N rows.

Current date: {today} ({weekday}), timezone {timezone}.
Example output: {example}
Authorized candidates: {candidates}"""

_EXAMPLE = json.dumps({
    "metric_id": 1, "metric_version_id": 2, "dimensions": [], "filters": [],
    "time_range": {"start": "2026-08-01T00:00:00", "end": "2026-09-01T00:00:00"}, "limit": None,
}, separators=(",", ":"))


def planning_now() -> datetime:
    """Current local time in the configured planning timezone (naive, like the plan values)."""
    try:
        zone = ZoneInfo(settings.GRAPH_PLANNING_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(503, "gateway_configuration_required") from None
    return datetime.now(zone).replace(tzinfo=None)


def metric_planning_messages(question: str, candidates: list[dict], now: datetime):
    from langchain_core.messages import HumanMessage, SystemMessage

    system = _RULES.format(
        today=now.date().isoformat(),
        weekday=_WEEKDAYS[now.weekday()],
        timezone=settings.GRAPH_PLANNING_TIMEZONE,
        example=_EXAMPLE,
        candidates=json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
    )
    return [SystemMessage(content=system), HumanMessage(content=question)]

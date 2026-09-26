import os
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.contracts import ModelCallError
from app.planning import (
    REPAIRABLE_ERRORS,
    MetricCandidate,
    MetricClarification,
    MetricPlanningError,
    MetricQueryPlan,
    parse_model_reply,
)

STOP_STATUSES = ("failed", "rejected", "needs_clarification")


class MetricGateway(Protocol):
    def authorize(self) -> None: ...
    def candidates(self) -> list[MetricCandidate]: ...
    def plan(self, candidates: list[MetricCandidate], repairs: list[dict]) -> str: ...
    def compile(self, plan: MetricQueryPlan) -> dict: ...
    def execute(self, plan: MetricQueryPlan) -> dict: ...


class MetricState(TypedDict, total=False):
    question: str
    datasource_id: int
    candidates: list[MetricCandidate]
    raw_plan: str
    plan: MetricQueryPlan
    compiled: dict
    executed: dict
    # Rejected replies fed back to the model: [{"previous": raw, "error": code}]
    repair_log: list[dict]
    repair_pending: bool
    repairs: int
    clarification: str | None
    clarification_reason: str | None
    status: str
    error: str
    metric_id: int | None
    metric_code: str | None
    metric_name: str | None
    metric_version_id: int | None
    metric_version: int | None
    dimensions: list[str]
    time_range: dict[str, str] | None
    unit: str | None
    sql_fingerprint: str | None
    compiler: str | None
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool


def max_repairs() -> int:
    """Bounded retries after a rejected plan; the run deadline bounds them too."""
    try:
        return max(0, min(int(os.environ.get("GRAPH_MAX_PLAN_REPAIRS", "2")), 3))
    except ValueError:
        return 2


def _gateway_failure(exc: ModelCallError) -> dict:
    rejected = exc.code in {"gateway_rejected", "metric_compile_failed"}
    return {"status": "rejected" if rejected else "failed", "error": exc.code}


def build_metric_graph(gateway: MetricGateway, execute: bool = False):
    """Plan graph (optionally executing) with bounded repair and a clarification exit.

    authorize -> retrieve -> model_plan -> validate_plan -> compile [-> execute] -> answer
    A repairable rejection at validate_plan or compile loops back to model_plan
    with the rejected reply and its error code, at most ``max_repairs()`` times.
    """
    limit = max_repairs()

    def repair_or_reject(state: MetricState, code: str) -> dict:
        log = state.get("repair_log") or []
        if code in REPAIRABLE_ERRORS and len(log) < limit and state.get("raw_plan"):
            return {"repair_log": [*log, {"previous": state["raw_plan"], "error": code}],
                    "repair_pending": True, "repairs": len(log) + 1}
        return {"status": "rejected", "error": code}

    def authorize(_state: MetricState):
        try:
            gateway.authorize()
            return {"status": "running", "repairs": 0}
        except ModelCallError as exc:
            return _gateway_failure(exc)

    def retrieve(_state: MetricState):
        try:
            candidates = gateway.candidates()
            if not candidates:
                return {"status": "rejected", "error": "metric_not_found"}
            return {"candidates": candidates}
        except ModelCallError as exc:
            return _gateway_failure(exc)

    def model_plan(state: MetricState):
        try:
            return {"raw_plan": gateway.plan(state["candidates"], state.get("repair_log") or []),
                    "repair_pending": False}
        except ModelCallError as exc:
            return {**_gateway_failure(exc), "repair_pending": False}

    def validate_plan(state: MetricState):
        try:
            reply = parse_model_reply(state["raw_plan"], state["candidates"])
        except MetricPlanningError as exc:
            return repair_or_reject(state, exc.code)
        except Exception:
            return repair_or_reject(state, "metric_plan_invalid")
        if isinstance(reply, MetricClarification):
            return {"status": "needs_clarification", "clarification": reply.clarification.strip(),
                    "clarification_reason": reply.reason}
        return {"plan": reply}

    def compile_plan(state: MetricState):
        try:
            return {"compiled": gateway.compile(state["plan"])}
        except ModelCallError as exc:
            if exc.code == "metric_compile_failed":
                return repair_or_reject(state, exc.code)
            return _gateway_failure(exc)
        except Exception:
            return {"status": "failed", "error": "metric_compile_failed"}

    def execute_plan(state: MetricState):
        # SQL never enters this service: the backend recompiles the published
        # version and returns only the bounded result contract.
        try:
            return {"executed": gateway.execute(state["plan"])}
        except ModelCallError as exc:
            return _gateway_failure(exc)
        except Exception:
            return {"status": "failed", "error": "metric_execution_failed"}

    def answer(state: MetricState):
        compiled = state["compiled"]
        plan = state["plan"]
        candidate = next(
            item for item in state["candidates"]
            if item.metric_id == plan.metric_id and item.metric_version_id == plan.metric_version_id
        )
        result = {
            "status": "completed",
            "metric_id": compiled.get("metric_id"),
            "metric_code": compiled.get("metric_code"),
            "metric_name": compiled.get("metric_name"),
            "metric_version_id": compiled.get("metric_version_id"),
            "metric_version": compiled.get("metric_version"),
            "dimensions": compiled.get("dimensions") or [],
            "time_range": compiled.get("time_range"),
            "unit": candidate.unit,
            "sql_fingerprint": compiled.get("sql_fingerprint"),
            "compiler": compiled.get("compiler"),
        }
        executed = state.get("executed")
        if executed is not None:
            result.update({
                "columns": executed.get("columns") or [],
                "rows": executed.get("rows") or [],
                "row_count": executed.get("row_count", 0),
                "truncated": executed.get("truncated", False),
            })
        return result

    def route(state: MetricState) -> str:
        if state.get("status") in STOP_STATUSES:
            return "stop"
        if state.get("repair_pending"):
            return "repair"
        return "next"

    steps = [
        ("authorize", authorize),
        ("retrieve", retrieve),
        ("model_plan", model_plan),
        ("validate_plan", validate_plan),
        ("compile", compile_plan),
    ]
    if execute:
        steps.append(("execute", execute_plan))
    steps.append(("answer", answer))
    builder = StateGraph(MetricState)
    for name, node in steps:
        builder.add_node(name, node)
    builder.add_edge(START, "authorize")
    for index, (name, _node) in enumerate(steps[:-1]):
        next_name = steps[index + 1][0]
        builder.add_conditional_edges(
            name,
            route,
            {"stop": END, "next": next_name, "repair": "model_plan"},
        )
    builder.add_edge("answer", END)
    return builder.compile()


def recursion_limit() -> int:
    """Node visits for the longest path: every repair re-runs plan, validate and compile."""
    return 8 + 3 * (max_repairs() + 1)

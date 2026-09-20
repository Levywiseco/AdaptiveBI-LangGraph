from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from app.contracts import ModelCallError
from app.planning import MetricCandidate, MetricPlanningError, MetricQueryPlan, parse_metric_plan


class MetricGateway(Protocol):
    def authorize(self) -> None: ...
    def candidates(self) -> list[MetricCandidate]: ...
    def plan(self, candidates: list[MetricCandidate]) -> str: ...
    def compile(self, plan: MetricQueryPlan) -> dict: ...


class MetricState(TypedDict, total=False):
    question: str
    datasource_id: int
    candidates: list[MetricCandidate]
    raw_plan: str
    plan: MetricQueryPlan
    compiled: dict
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


def _gateway_failure(exc: ModelCallError) -> dict:
    rejected = exc.code in {"gateway_rejected", "metric_compile_failed"}
    return {"status": "rejected" if rejected else "failed", "error": exc.code}


def build_metric_graph(gateway: MetricGateway):
    def authorize(_state: MetricState):
        try:
            gateway.authorize()
            return {"status": "running"}
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
            return {"raw_plan": gateway.plan(state["candidates"])}
        except ModelCallError as exc:
            return _gateway_failure(exc)

    def validate_plan(state: MetricState):
        try:
            return {"plan": parse_metric_plan(state["raw_plan"], state["candidates"])}
        except MetricPlanningError as exc:
            return {"status": "rejected", "error": exc.code}
        except Exception:
            return {"status": "rejected", "error": "metric_plan_invalid"}

    def compile_plan(state: MetricState):
        try:
            return {"compiled": gateway.compile(state["plan"])}
        except ModelCallError as exc:
            return _gateway_failure(exc)
        except Exception:
            return {"status": "failed", "error": "metric_compile_failed"}

    def answer(state: MetricState):
        compiled = state["compiled"]
        plan = state["plan"]
        candidate = next(
            item for item in state["candidates"]
            if item.metric_id == plan.metric_id and item.metric_version_id == plan.metric_version_id
        )
        return {
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

    steps = [
        ("authorize", authorize),
        ("retrieve", retrieve),
        ("model_plan", model_plan),
        ("validate_plan", validate_plan),
        ("compile", compile_plan),
        ("answer", answer),
    ]
    builder = StateGraph(MetricState)
    for name, node in steps:
        builder.add_node(name, node)
    builder.add_edge(START, "authorize")
    for index, (name, _node) in enumerate(steps[:-1]):
        next_name = steps[index + 1][0]
        builder.add_conditional_edges(
            name,
            lambda state: "stop" if state.get("status") in ("failed", "rejected") else "next",
            {"stop": END, "next": next_name},
        )
    builder.add_edge("answer", END)
    return builder.compile()

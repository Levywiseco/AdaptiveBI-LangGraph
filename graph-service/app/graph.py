from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.contracts import Principal, QueryState, QueryTools


def build_graph(model: BaseChatModel, tools: QueryTools, principal: Principal):
    """Model/tools/identity are dependencies, not checkpoint-serializable state."""

    def authorize(state: QueryState):
        try:
            tools.authorize(principal, state["datasource_id"])
            return {"status": "running"}
        except PermissionError:
            return {"status": "rejected", "error": "datasource_access_denied"}

    def retrieve(state: QueryState):
        try:
            return {"schema": tools.schema(principal, state["datasource_id"])}
        except PermissionError:
            return {"status": "rejected", "error": "datasource_access_denied"}

    def generate(state: QueryState):
        try:
            response = model.invoke([
                SystemMessage(content="Return a single SQLite SELECT statement only. Schema: " + state["schema"]),
                HumanMessage(content=state["question"]),
            ])
            if not isinstance(response.content, str):
                return {"status": "failed", "error": "unsupported_model_output"}
            return {"sql": response.content.strip()}
        except Exception:
            return {"status": "failed", "error": "model_call_failed"}

    def validate(state: QueryState):
        try:
            tools.validate(state["sql"])
            return {"status": "running"}
        except ValueError:
            return {"status": "rejected", "error": "sql_validation_failed"}

    def execute(state: QueryState):
        try:
            result = tools.execute(principal, state["datasource_id"], state["sql"])
            return result.model_dump()
        except PermissionError:
            return {"status": "rejected", "error": "datasource_access_denied"}
        except Exception:
            return {"status": "failed", "error": "query_execution_failed"}

    def answer(state: QueryState):
        return {"status": "completed", "answer": f"合成数据查询完成，返回 {len(state['rows'])} 行。"}

    builder = StateGraph(QueryState)
    steps = [("authorize", authorize), ("retrieve", retrieve), ("generate", generate),
             ("validate", validate), ("execute", execute), ("answer", answer)]
    for name, node in steps:
        builder.add_node(name, node)
    builder.add_edge(START, "authorize")
    for index, (name, _) in enumerate(steps[:-1]):
        next_name = steps[index + 1][0]
        builder.add_conditional_edges(
            name,
            lambda state: "stop" if state.get("status") in ("failed", "rejected") else "next",
            {"stop": END, "next": next_name},
        )
    builder.add_edge("answer", END)
    return builder.compile()

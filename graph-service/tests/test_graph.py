import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.contracts import Principal
from app.graph import build_graph
from app.main import app
from app.synthetic import SyntheticTools

CASES = json.loads((Path(__file__).parents[1] / "fixtures/cases.json").read_text(encoding="utf-8"))
IDENTITY = Principal("fixture-user", "demo", frozenset({"synthetic-sales"}))


def run_sql(sql, principal=IDENTITY, tools=None):
    graph = build_graph(FakeListChatModel(responses=[sql]), tools or SyntheticTools(), principal)
    return graph.invoke({"question": "synthetic test", "datasource_id": "synthetic-sales"})


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_synthetic_result_contract(case):
    result = run_sql(case["sql"])
    assert result["status"] == "completed"
    assert result["columns"] == case["columns"]
    assert result["rows"] == case["rows"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales", "DROP TABLE sales", "UPDATE sales SET gross=0",
    "SELECT * FROM sales; DELETE FROM sales", "SELECT * FROM sqlite_master",
    "SELECT * FROM private_sales", "SELECT missing FROM sales", "PRAGMA database_list",
    "ATTACH DATABASE '/tmp/example' AS other", "SELECT * FROM other.sales",
    "SELECT * FROM sales JOIN sales s ON 1=1",
])
def test_rejects_unsafe_or_unsupported_sql_before_execution(sql):
    class NeverExecute(SyntheticTools):
        def execute(self, *args):
            pytest.fail("Rejected SQL reached execution")

    assert run_sql(sql, tools=NeverExecute())["status"] == "rejected"


def test_sqlite_function_allowlist_defense():
    result = run_sql("SELECT load_extension('anything') FROM sales")
    assert result["status"] == "failed"
    assert result["error"] == "query_execution_failed"


@pytest.mark.parametrize("principal", [
    Principal("user", "other-workspace", frozenset({"synthetic-sales"})),
    Principal("user", "demo", frozenset()),
])
def test_identity_rejected_before_model_call(principal):
    class NeverModel(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            pytest.fail("Unauthorized request invoked a model")

    graph = build_graph(NeverModel(responses=["unused"]), SyntheticTools(), principal)
    result = graph.invoke({"question": "count", "datasource_id": "synthetic-sales"})
    assert result["status"] == "rejected"


def test_permission_is_rechecked_at_execution():
    class RevokedTools(SyntheticTools):
        def execute(self, *args):
            raise PermissionError("revoked")

    result = run_sql("SELECT COUNT(*) FROM sales", tools=RevokedTools())
    assert result["status"] == "rejected"
    assert "rows" not in result


def test_model_failure_has_safe_error():
    class BrokenModel(FakeListChatModel):
        def invoke(self, *args, **kwargs):
            raise RuntimeError("sensitive-provider-details")

    graph = build_graph(BrokenModel(responses=["unused"]), SyntheticTools(), IDENTITY)
    result = graph.invoke({"question": "count", "datasource_id": "synthetic-sales"})
    assert result["status"] == "failed"
    assert result["error"] == "model_call_failed"


def test_parallel_runs_do_not_mix_results():
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda case: run_sql(case["sql"]), CASES))
    assert [result["rows"] for result in results] == [case["rows"] for case in CASES]


def test_api_only_accepts_fixed_demo_cases():
    client = TestClient(app)
    assert client.get("/health").json()["real_model"] is False
    assert client.post("/demo/run", json={"case_id": "count"}).json()["result"]["rows"] == [[5]]
    assert client.post("/demo/run", json={"case_id": "unknown"}).status_code == 404
    assert client.post("/demo/run", json={"case_id": "count", "user_id": "admin"}).status_code == 422
    assert client.post("/demo/run", json={"case_id": "count", "sql": "DELETE FROM sales"}).status_code == 422


def test_stream_has_ordered_sanitized_events_and_one_terminal():
    response = TestClient(app).post("/demo/stream", json={"case_id": "net"})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["node"] for event in events] == ["authorize", "retrieve", "generate", "validate", "execute", "answer"]
    assert [event["sequence"] for event in events] == list(range(1, 7))
    assert len({event["run_id"] for event in events}) == 1
    assert [event["type"] for event in events].count("completed") == 1
    assert events[-1]["type"] == "completed"
    assert all("schema" not in event["payload"] and "sql" not in event["payload"] for event in events)

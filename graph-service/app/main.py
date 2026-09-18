"""Loopback-only demo API: fixed cases + fake model + disposable synthetic SQLite."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from app.contracts import DemoRequest, Principal, RunEvent, SafeResult
from app.experiment import router as experiment_router
from app.graph import build_graph
from app.synthetic import SyntheticTools

CASES = json.loads((Path(__file__).parent.parent / "fixtures" / "cases.json").read_text(encoding="utf-8"))
app = FastAPI(title="AdaptiveBI Graph Foundation — synthetic demo", version="0.1.0")
app.include_router(experiment_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Validation errors must not echo rejected secrets or identity fields.
    return JSONResponse(status_code=422, content={"detail": "invalid_request"})


@app.get("/health")
def health():
    return {"status": "ok", "mode": "synthetic", "real_model": False, "persistent": False}


@app.get("/demo/cases")
def cases():
    return [{"id": case["id"], "question": case["question"]} for case in CASES]


def prepare(case_id: str):
    case = next((case for case in CASES if case["id"] == case_id), None)
    if case is None:
        raise HTTPException(404, "Unknown synthetic case")
    principal = Principal("demo-user", "demo", frozenset({"synthetic-sales"}))
    graph = build_graph(FakeListChatModel(responses=[case["sql"]]), SyntheticTools(), principal)
    return graph, {"question": case["question"], "datasource_id": "synthetic-sales"}


@app.post("/demo/run")
def run(request: DemoRequest):
    graph, state = prepare(request.case_id)
    return {"mode": "synthetic", "run_id": str(uuid4()),
            "result": SafeResult.model_validate(graph.invoke(state, {"recursion_limit": 12})).model_dump()}


@app.post("/demo/stream")
def stream(request: DemoRequest):
    graph, state = prepare(request.case_id)
    run_id = str(uuid4())

    def events():
        sequence = 0
        try:
            for update in graph.stream(state, {"recursion_limit": 12}, stream_mode="updates"):
                for node, patch in update.items():
                    patch = patch or {}
                    sequence += 1
                    status = patch.get("status")
                    event_type = status if status in ("completed", "rejected", "failed") else "progress"
                    # Do not stream raw graph state (schemas, prompts or future credentials).
                    payload = {key: value for key, value in patch.items()
                               if key in {"status", "error", "answer", "columns", "rows", "truncated"}}
                    event = RunEvent(run_id=run_id, sequence=sequence, type=event_type, node=node, payload=payload)
                    yield f"id: {run_id}:{sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
        except Exception:
            event = RunEvent(run_id=run_id, sequence=sequence + 1, type="failed", node="runtime",
                             payload={"error": "graph_execution_failed"})
            yield f"event: failed\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

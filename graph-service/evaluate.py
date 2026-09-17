"""Deterministic harness assessment. Does NOT measure real LLM answer quality."""
import json
from time import perf_counter

from app.main import CASES, prepare


def main():
    results = []
    for case in CASES:
        graph, state = prepare(case["id"])
        started = perf_counter()
        result = graph.invoke(state)
        passed = (result.get("status") == "completed" and result.get("rows") == case["rows"]
                  and result.get("columns") == case["columns"])
        results.append({"case_id": case["id"], "passed": passed,
                        "elapsed_ms": round((perf_counter() - started) * 1000, 2)})
    print(json.dumps({"mode": "synthetic-scripted-model", "real_model_calls": 0,
                      "passed": sum(item["passed"] for item in results), "total": len(results),
                      "results": results}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(item["passed"] for item in results) else 1)


if __name__ == "__main__":
    main()

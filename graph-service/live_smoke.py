"""Explicit opt-in live check against an independently configured test backend."""
import json
import os
from time import perf_counter

import httpx


def main():
    required = ("ADAPTIVE_TEST_API_URL", "ADAPTIVE_TEST_LOGIN_TOKEN")
    if (os.environ.get("ADAPTIVE_LIVE_TEST") != "1"
            or os.environ.get("ADAPTIVE_TEST_ENV_CONFIRMED") != "1"
            or not all(os.environ.get(key) for key in required)):
        print(json.dumps({"status": "not_executed", "reason": "independent_test_environment_and_login_required"}))
        return 2
    question = "2026年8月，东部地区扣除退款后的销售额是多少？请只返回金额。"
    started = perf_counter()
    try:
        with httpx.Client(timeout=50, follow_redirects=False, trust_env=False) as client:
            response = client.post(os.environ["ADAPTIVE_TEST_API_URL"].rstrip("/") + "/analysis/query",
                                   headers={"X-SQLBOT-TOKEN": "Bearer " + os.environ["ADAPTIVE_TEST_LOGIN_TOKEN"]},
                                   json={"question": question, "datasource_id": "synthetic-sales"})
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        passed = (data.get("mode") == "synthetic-live" and data.get("status") == "completed"
                  and data.get("rows") == [[770]] and data.get("model_calls") == 1)
        report = {key: data.get(key) for key in ("run_id", "status", "model_config_id", "model_calls", "usage", "error")}
        print(json.dumps({"suite": "live-smoke", "passed": passed, "expected_rows": [[770]],
                          "actual_rows": data.get("rows"), "elapsed_ms": round((perf_counter()-started)*1000, 2),
                          **report}, ensure_ascii=False))
        return 0 if passed else 1
    except Exception:
        # Never print response bodies, tokens, URLs or provider exceptions.
        print(json.dumps({"status": "failed", "reason": "live_request_failed", "usage": None,
                          "model_calls": None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The synthetic metric suite stays consistent and the scripted pipeline stays green."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluations"))
sys.path.insert(0, str(ROOT / "evaluations" / "metric_suite"))

import build  # noqa: E402
import catalog  # noqa: E402
import metric_eval  # noqa: E402

# Paraphrases with no lexical overlap; only embedding recall (off here) finds them.
EMBEDDING_ONLY = {"syn-discount-paraphrase", "syn-units-paraphrase", "syn-refund-paraphrase"}


def test_generated_files_match_the_catalog(tmp_path):
    regenerated = tmp_path / "orders.csv"
    catalog.write_orders(catalog.generate_orders(), regenerated)
    assert regenerated.read_text(encoding="utf-8") == catalog.DATA_FILE.read_text(encoding="utf-8")
    assert build.render_cases() == catalog.CASES_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scripted():
    return metric_eval.evaluate("scripted", hybrid=True)


def test_compiled_sql_matches_the_independent_answer_key(scripted):
    _suite, summary, results = scripted
    failures = {item["id"] for item in results if not item["passed"]}
    # Every miss is a recall miss that needs embeddings, never a wrong answer.
    assert failures <= EMBEDDING_ONLY, failures
    assert all(item["recalled"] is False for item in results if item["id"] in failures)
    assert summary["recall"]["rate"] >= 0.9


def test_hybrid_recall_is_not_worse_than_the_legacy_exact_match(scripted):
    _suite, legacy, _results = metric_eval.evaluate("scripted", hybrid=False)
    assert scripted[1]["recall"]["rate"] > legacy["recall"]["rate"]


def test_report_lists_recall_misses(scripted):
    suite_info, summary, results = scripted
    report = metric_eval.render_report(suite_info, "scripted", True, summary, results)
    assert "Gold metric among candidates" in report
    assert all(case_id in report for case_id in {item["id"] for item in results if not item["passed"]})


@pytest.mark.parametrize("values,message", [
    ({"EVAL_MODEL_API_KEY": "你的key"}, "EVAL_MODEL_API_KEY contains non-ASCII"),
    ({"EVAL_MODEL_NAME": "你的模型名"}, "EVAL_MODEL_NAME contains non-ASCII"),
    ({"EVAL_MODEL_BASE_URL": "api.moonshot.cn/v1"}, "must start with http"),
    ({"EVAL_MODEL_NAME": "  "}, "live mode needs EVAL_MODEL_NAME"),
])
def test_live_mode_rejects_placeholder_configuration(monkeypatch, values, message):
    base = {"EVAL_MODEL_BASE_URL": "https://api.example.invalid/v1",
            "EVAL_MODEL_API_KEY": "sk-test", "EVAL_MODEL_NAME": "model"}
    for key, value in {**base, **values}.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as stop:
        metric_eval.live_planner()
    assert message in str(stop.value) and "你的" not in str(stop.value)


def test_live_mode_stops_after_repeated_model_errors(monkeypatch):
    calls = []

    class ProviderError(Exception):
        status_code = 401

    def failing(_messages, _case):
        calls.append(1)
        raise ProviderError("invalid api key")

    monkeypatch.setattr(metric_eval, "live_planner", lambda: failing)
    with pytest.raises(SystemExit) as stop:
        metric_eval.evaluate("live")
    assert "3 consecutive model errors (ProviderError (HTTP 401))" in str(stop.value)
    assert len(calls) == 3


def test_runner_drives_the_real_graph_through_a_repair():
    env = metric_eval.build_environment()
    document = metric_eval.yaml.safe_load(catalog.CASES_FILE.read_text(encoding="utf-8"))
    case = next(item for item in document["cases"] if item["id"] == "dim-region")
    gold = metric_eval.scripted_planner(env)
    replies = []

    def flaky(messages, current):
        # First reply names an undeclared dimension; the repair prompt explains why.
        replies.append(messages)
        if len(replies) == 1:
            content, usage = gold(messages, current)
            return content.replace('"region"', '"customer_id"'), usage
        return gold(messages, current)

    result = metric_eval.run_case(env, case, flaky, metric_eval.datetime(2026, 9, 25, 10), True, 0.01)
    assert result["passed"] and result["repairs"] == 1 and result["model_calls"] == 2
    assert "metric_dimension_not_allowed" in replies[1][-1].content

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

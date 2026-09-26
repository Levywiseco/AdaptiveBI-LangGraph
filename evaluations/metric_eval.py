#!/usr/bin/env python3
"""Evaluate governed metric planning on the synthetic sales suite.

Every case runs the production path on isolated in-memory databases:

    candidate recall -> known dimension values -> planning prompt -> model
    -> strict plan parsing (graph service) -> plan re-validation + metric compiler
    -> SQL on a SQLite copy of sales_orders.csv -> row comparison with the answer key

Modes
  scripted  The "model" returns each case's gold plan. No network. Measures candidate
            recall and checks the compiler against the independent answer key.
  live      Calls an OpenAI-compatible model configured by EVAL_MODEL_BASE_URL,
            EVAL_MODEL_API_KEY and EVAL_MODEL_NAME. Measures real planning accuracy.

Run from backend/ with the legacy environment:

    uv run --no-sync python ../evaluations/metric_eval.py --mode scripted \
        --output ../evaluations/reports/metric-scripted.json \
        --report ../evaluations/reports/metric-scripted.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
# The legacy settings create directories on import and may load a local embedding
# model; keep both inside a scratch directory unless the caller overrides them.
_SCRATCH = Path(tempfile.gettempdir()) / "adaptive-metric-eval"
for _key, _sub in {"BASE_DIR": "", "UPLOAD_DIR": "data/file", "EXCEL_PATH": "data/excel",
                   "MCP_IMAGE_PATH": "images", "LOG_DIR": "logs", "LOCAL_MODEL_PATH": "models"}.items():
    os.environ.setdefault(_key, str(_SCRATCH / _sub))
os.environ.setdefault("EMBEDDING_ENABLED", "false")
os.environ.setdefault("TABLE_EMBEDDING_ENABLED", "false")
for _path in (ROOT / "backend", ROOT / "graph-service", ROOT / "evaluations", ROOT / "evaluations" / "metric_suite"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import catalog as suite  # noqa: E402
import sqlbot_xpack  # noqa: E402,F401 — legacy import order: xpack before apps.db
import yaml  # noqa: E402
from run import unordered_rows_equal  # noqa: E402
from sqlalchemy import JSON, Integer, MetaData, create_engine, text  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session  # noqa: E402

ADMIN = SimpleNamespace(id=1, account="admin", oid=1, isAdmin=True)
DATASOURCE_ID = 1
MAX_CONSECUTIVE_ERRORS = 3


@dataclass
class Environment:
    meta: Any
    data: Any
    metric_ids: dict[str, tuple[int, int]]  # code -> (metric_id, version_id)


def build_environment() -> Environment:
    """Metadata and data in two isolated in-memory SQLite databases."""
    from sqlbot_xpack.permissions.models.ds_permission import DsPermission
    from sqlbot_xpack.permissions.models.ds_rules import DsRules

    from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable
    from apps.datasource.utils.utils import aes_encrypt
    from apps.metrics.models.metric import (
        MetricDefinition,
        MetricDimensionValue,
        MetricVersion,
    )
    from apps.metrics.service.dimension_values import (
        sample_version_dimension_values,
        set_manual_dimension_values,
    )

    meta = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (CoreDatasource, CoreTable, CoreField, MetricDefinition, MetricVersion,
                  MetricDimensionValue, DsPermission, DsRules):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
        if model is MetricDimensionValue:
            table.c.id.identity = None
            table.c.id.type = Integer()
    metadata.create_all(meta)

    data = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    orders = suite.load_orders()
    with data.begin() as connection:
        connection.execute(text(
            "CREATE TABLE sales_orders (order_id INTEGER, paid_at TEXT, region TEXT, channel TEXT, "
            "category TEXT, customer_id INTEGER, quantity INTEGER, amount REAL, discount_amount REAL, "
            "refund_amount REAL, status TEXT)"))
        connection.execute(
            text("INSERT INTO sales_orders VALUES (:order_id, :paid_at, :region, :channel, :category, "
                 ":customer_id, :quantity, :amount, :discount_amount, :refund_amount, :status)"),
            [{**row, "paid_at": row["paid_at"].strftime("%Y-%m-%d %H:%M:%S")} for row in orders])

    def sample(_datasource, sql: str) -> list[Any]:
        with data.connect() as connection:
            return [row[0] for row in connection.execute(text(sql))]

    metric_ids = {}
    with Session(meta) as session:
        session.add(CoreDatasource(
            id=DATASOURCE_ID, name="Synthetic sales (evaluation)", type="pg", type_name="PostgreSQL",
            status="Success", create_by=1, oid=1,
            configuration=aes_encrypt(json.dumps({"host": "isolated"})).decode()))
        session.add(CoreTable(id=1, ds_id=DATASOURCE_ID, checked=True, table_name=suite.TABLE,
                              table_comment="虚构销售订单", custom_comment=""))
        for index, name in enumerate(suite.COLUMNS):
            session.add(CoreField(id=index + 1, ds_id=DATASOURCE_ID, table_id=1, checked=True,
                                  field_name=name, field_type=suite.FIELD_TYPES[name],
                                  field_comment="", custom_comment="", field_index=index))
        for index, metric in enumerate(suite.METRICS, start=1):
            session.add(MetricDefinition(
                id=index, oid=1, code=metric.code, name=metric.name, aliases=metric.aliases,
                description=metric.description, datasource_id=DATASOURCE_ID, owner_user_id=1,
                status="published", current_version_id=index))
            session.add(MetricVersion(
                id=index, metric_id=index, version=1, expression=metric.expression,
                aggregation=metric.aggregation, time_field="paid_at", required_tables=[suite.TABLE],
                dimensions=suite.DIMENSIONS, filters=metric.filters, unit=metric.unit,
                status="published", validation_status="approved", created_by=1))
            metric_ids[metric.code] = (index, index)
        session.commit()
        for metric_id, version_id in metric_ids.values():
            sample_version_dimension_values(session, version_id, 1, execute=sample)
            for dimension, values in suite.MANUAL_DIMENSION_VALUES.items():
                set_manual_dimension_values(session, metric_id, version_id, dimension, values, 1, 1)
        session.commit()
    return Environment(meta=meta, data=data, metric_ids=metric_ids)


# --- Planners -------------------------------------------------------------------

Planner = Callable[[list, dict], tuple[str, dict]]


def scripted_planner(env: Environment) -> Planner:
    """Answers every call with the gold plan, or a clarification when the case has none."""
    def plan(_messages, case):
        gold = case["gold"]
        if gold is None:
            return json.dumps({"clarification": "指标库里没有能回答这个问题的指标，换个问法？",
                               "reason": "unsupported"}, ensure_ascii=False), {}
        metric_id, version_id = env.metric_ids[gold["metric"]]
        return json.dumps({"metric_id": metric_id, "metric_version_id": version_id,
                           "dimensions": gold["dimensions"], "filters": gold["filters"],
                           "time_range": gold["time_range"], "limit": gold["limit"]},
                          ensure_ascii=False), {}
    return plan


def live_planner() -> Planner:
    from apps.ai_model.model_factory import LLMConfig, LLMFactory
    from apps.graph_gateway.model_policy import gateway_model_config
    from apps.graph_gateway.service import usage_from

    keys = ("EVAL_MODEL_BASE_URL", "EVAL_MODEL_API_KEY", "EVAL_MODEL_NAME")
    missing = [key for key in keys if not os.environ.get(key, "").strip()]
    if missing:
        raise SystemExit("live mode needs " + ", ".join(missing))
    # Values end up in HTTP headers and URLs; non-ASCII text is almost always an
    # unreplaced placeholder. The message names the variable, never its value.
    invalid = [key for key in keys if not os.environ[key].strip().isascii()]
    if invalid:
        raise SystemExit(", ".join(invalid) + " contains non-ASCII characters; replace the placeholder value")
    if not os.environ["EVAL_MODEL_BASE_URL"].startswith(("http://", "https://")):
        raise SystemExit("EVAL_MODEL_BASE_URL must start with http:// or https://")
    config, _policy = gateway_model_config(LLMConfig(
        model_type="openai", model_name=os.environ["EVAL_MODEL_NAME"].strip(),
        api_key=os.environ["EVAL_MODEL_API_KEY"].strip(),
        api_base_url=os.environ["EVAL_MODEL_BASE_URL"].strip()))
    model = LLMFactory.create_llm(config).llm

    def plan(messages, _case):
        message = model.invoke(messages)
        content = message.content if isinstance(message.content, str) else ""
        return content, usage_from(message)
    return plan


# --- Running cases -----------------------------------------------------------

class EvalGateway:
    """In-process stand-in for the backend gateway, calling the same backend code.

    The graph service's real LangGraph flow (repairs, clarification) drives it.
    """

    def __init__(self, env: Environment, case: dict, planner: Planner, now: datetime, hybrid: bool):
        self.env, self.case, self.planner, self.now, self.hybrid = env, case, planner, now, hybrid
        self.candidate_dicts: list[dict] = []
        self.compiled: Optional[dict] = None
        self.calls = 0
        self.usage: dict[str, Optional[int]] = {}

    def authorize(self):
        return None

    def candidates(self):
        from app.planning import MetricCandidate

        from apps.graph_gateway.service import candidates_with_context
        from apps.metrics.crud.metric import get_metric_candidates

        def recall(session, text, oid, datasource_id, limit, current_user):
            return get_metric_candidates(session, text, oid, datasource_id, limit=limit,
                                         current_user=current_user, hybrid=self.hybrid)

        with Session(self.env.meta) as session:
            self.candidate_dicts = candidates_with_context(
                session, self.case["question"], 1, DATASOURCE_ID, ADMIN,
                self.case.get("context") or [], recall=recall)
        return [MetricCandidate.model_validate(item) for item in self.candidate_dicts]

    def plan(self, _candidates, repairs):
        from apps.graph_gateway.prompts import metric_planning_messages
        from apps.metrics.service.dimension_values import planning_dimension_values

        with Session(self.env.meta) as session:
            known = planning_dimension_values(session, self.candidate_dicts, ADMIN, DATASOURCE_ID)
        prompt_candidates = [
            {**item, "dimension_values": known[item["metric_version_id"]]}
            if item["metric_version_id"] in known else item
            for item in self.candidate_dicts
        ]
        messages = metric_planning_messages(self.case["question"], prompt_candidates, self.now,
                                            context=self.case.get("context") or [], repairs=repairs)
        content, usage = self.planner(messages, self.case)
        self.calls += 1
        for key, value in usage.items():
            if value is not None:
                self.usage[key] = self.usage.get(key, 0) + value
        return content

    def compile(self, plan):
        from app.contracts import ModelCallError
        from fastapi import HTTPException

        from apps.graph_gateway.service import _validated_plan_request
        from apps.metrics.service.query_planner import preview_metric_query_plan

        try:
            metric_id, _version_id, payload = _validated_plan_request(plan.model_dump(mode="json"))
            with Session(self.env.meta) as session:
                self.compiled = preview_metric_query_plan(session, metric_id, payload, 1, ADMIN)
        except HTTPException as exc:
            raise ModelCallError("metric_compile_failed" if exc.status_code == 422
                                 else "gateway_rejected") from None
        return {key: self.compiled.get(key) for key in (
            "metric_id", "metric_code", "metric_name", "metric_version_id", "metric_version",
            "dimensions", "time_range", "sql_fingerprint", "compiler")}

    def execute(self, _plan):
        with self.env.data.connect() as connection:
            cursor = connection.execute(text(self.compiled["sql"]))
            rows = [dict(row._mapping) for row in cursor]
        return {"metric_id": self.compiled["metric_id"], "sql_fingerprint": self.compiled["sql_fingerprint"],
                "columns": list(rows[0]) if rows else [], "rows": rows, "row_count": len(rows),
                "truncated": False, "elapsed_ms": 0.0}


def run_case(env: Environment, case: dict, planner: Planner, now: datetime,
             hybrid: bool, tolerance: float) -> dict[str, Any]:
    from app.metric_graph import build_metric_graph, recursion_limit

    gold_metric = (case.get("gold") or {}).get("metric")
    expected = case["expected"]
    result: dict[str, Any] = {"id": case["id"], "category": case["category"], "question": case["question"],
                              "context": case.get("context") or [],
                              "expected_outcome": expected["outcome"], "gold_metric": gold_metric,
                              "candidates": [], "recalled": None, "planned_metric": None,
                              "outcome": None, "error": None, "clarification": None, "repairs": 0,
                              "model_calls": 0, "passed": False, "usage": {}}
    gateway = EvalGateway(env, case, planner, now, hybrid)
    started = perf_counter()
    try:
        state = build_metric_graph(gateway, execute=True).invoke(
            {"question": case["question"], "datasource_id": DATASOURCE_ID},
            {"recursion_limit": recursion_limit() + 1})
    except Exception as exc:  # provider or network failure in live mode
        status = getattr(exc, "status_code", None)
        state = {"status": "error", "error": type(exc).__name__ + (f" (HTTP {status})" if status else "")}
    result["latency_ms"] = round((perf_counter() - started) * 1000, 1)
    result["candidates"] = [item["metric_code"] for item in gateway.candidate_dicts]
    if gold_metric:
        result["recalled"] = gold_metric in result["candidates"]
    result.update(repairs=state.get("repairs", 0), model_calls=gateway.calls, usage=gateway.usage,
                  error=state.get("error"), clarification=state.get("clarification"))
    status = state.get("status")
    if status == "completed":
        result.update(outcome="success", rows=state.get("rows", []), planned_metric=state.get("metric_code"))
    elif status == "needs_clarification":
        result["outcome"] = "clarification"
    elif status == "rejected" and state.get("error") == "metric_not_found":
        result["outcome"] = "refusal"
    elif status == "rejected":
        result["outcome"] = "rejected"
    else:
        result["outcome"] = "error"
    if expected["outcome"] == "refusal":
        # Not guessing is correct: no candidate, a clarification or a rejected plan.
        result["passed"] = result["outcome"] in ("refusal", "clarification", "rejected")
    else:
        result["passed"] = (result["outcome"] == "success"
                            and unordered_rows_equal(expected["rows"], result["rows"], tolerance))
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(items, key):
        values = [item[key] for item in items if item[key] is not None]
        return {"count": len(values), "rate": round(sum(values) / len(values), 4) if values else None}

    categories = sorted({item["category"] for item in results})
    usage = [item["usage"].get("total_tokens") for item in results if item["usage"].get("total_tokens")]
    latencies = sorted(item["latency_ms"] for item in results)
    return {
        "cases": len(results),
        "passed": sum(item["passed"] for item in results),
        "pass_rate": round(sum(item["passed"] for item in results) / len(results), 4),
        "recall": rate(results, "recalled"),
        "by_category": {
            category: {"cases": len(group := [item for item in results if item["category"] == category]),
                       "passed": sum(item["passed"] for item in group)}
            for category in categories
        },
        "total_tokens": sum(usage) if usage else None,
        "model_calls": sum(item["model_calls"] for item in results),
        "repairs": sum(item["repairs"] for item in results),
        "repaired_and_passed": sum(1 for item in results if item["repairs"] and item["passed"]),
        "clarifications": sum(1 for item in results if item["outcome"] == "clarification"),
        "p50_latency_ms": latencies[len(latencies) // 2],
        "p95_latency_ms": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
    }


def render_report(suite_info: dict, mode: str, hybrid: bool, summary: dict,
                  results: list[dict[str, Any]]) -> str:
    recall = summary["recall"]
    lines = [
        f"# Metric planning evaluation: {suite_info['name']}",
        "",
        f"- Mode: `{mode}`; candidate recall: `{'hybrid' if hybrid else 'legacy exact match'}`; "
        f"today: {suite_info['now']} ({suite_info['timezone']})",
        f"- Passed: **{summary['passed']}/{summary['cases']}** ({summary['pass_rate']:.0%})",
        f"- Gold metric among candidates: {recall['rate']:.0%} of {recall['count']} answerable cases",
        f"- Model calls: {summary['model_calls']} (repairs {summary['repairs']}, "
        f"passed after repair {summary['repaired_and_passed']}); clarifications {summary['clarifications']}",
        f"- Tokens: {summary['total_tokens'] if summary['total_tokens'] is not None else 'n/a'}; "
        f"latency p50 {summary['p50_latency_ms']} ms, p95 {summary['p95_latency_ms']} ms",
        "",
        "| Category | Passed |",
        "| --- | --- |",
        *[f"| {name} | {item['passed']}/{item['cases']} |" for name, item in summary["by_category"].items()],
    ]
    failures = [item for item in results if not item["passed"]]
    if failures:
        lines += ["", "## Failures", "", "| Case | Question | Outcome | Detail |", "| --- | --- | --- | --- |"]
        for item in failures:
            if item["recalled"] is False:
                detail = f"gold `{item['gold_metric']}` not recalled; candidates {item['candidates'] or 'none'}"
            elif item["outcome"] == "success" and item["planned_metric"] != item["gold_metric"]:
                detail = f"planned `{item['planned_metric']}` instead of `{item['gold_metric']}`"
            elif item["outcome"] == "success":
                detail = "rows differ from the answer key"
            elif item["outcome"] == "clarification":
                detail = f"asked instead of answering: {item['clarification']}"
            else:
                detail = item["error"] or ""
            lines.append(f"| {item['id']} | {item['question']} | {item['outcome']} | {detail} |")
    return "\n".join(lines) + "\n"


def evaluate(mode: str = "scripted", hybrid: bool = True, cases_path: Path = suite.CASES_FILE,
             only: Optional[set[str]] = None) -> tuple[dict, dict, list[dict]]:
    document = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    suite_info = document["suite"]
    cases = [case for case in document["cases"] if not only or case["id"] in only]
    env = build_environment()
    planner = scripted_planner(env) if mode == "scripted" else live_planner()
    now = datetime.fromisoformat(suite_info["now"])
    results = []
    consecutive_errors = 0
    for case in cases:
        results.append(run_case(env, case, planner, now, hybrid, suite_info["float_tolerance"]))
        consecutive_errors = consecutive_errors + 1 if results[-1]["outcome"] == "error" else 0
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            # A wrong key, model name or endpoint fails every case the same way.
            raise SystemExit(f"stopped after {consecutive_errors} consecutive model errors "
                             f"({results[-1]['error']}); check the model configuration")
    return suite_info, summarize(results), results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--legacy-recall", action="store_true",
                        help="use the legacy exact-match candidate recall (baseline)")
    parser.add_argument("--cases", type=Path, default=suite.CASES_FILE)
    parser.add_argument("--only", nargs="*", help="case ids to run")
    parser.add_argument("--output", type=Path, help="write per-case JSON results")
    parser.add_argument("--report", type=Path, help="write a Markdown report")
    args = parser.parse_args(argv)
    hybrid = not args.legacy_recall
    suite_info, summary, results = evaluate(args.mode, hybrid, args.cases, set(args.only or []))
    if args.output:
        args.output.write_text(json.dumps({"suite": suite_info, "mode": args.mode, "hybrid": hybrid,
                                           "summary": summary, "results": results},
                                          ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = render_report(suite_info, args.mode, hybrid, summary, results)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

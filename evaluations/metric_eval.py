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
    def plan(_messages, case):
        gold = case["gold"] or {}
        metric_id, version_id = env.metric_ids.get(gold.get("metric"), (1, 1))
        return json.dumps({"metric_id": metric_id, "metric_version_id": version_id,
                           "dimensions": gold.get("dimensions", []), "filters": gold.get("filters", []),
                           "time_range": gold.get("time_range"), "limit": gold.get("limit")},
                          ensure_ascii=False), {}
    return plan


def live_planner() -> Planner:
    from apps.ai_model.model_factory import LLMConfig, LLMFactory
    from apps.graph_gateway.model_policy import gateway_model_config
    from apps.graph_gateway.service import usage_from

    missing = [key for key in ("EVAL_MODEL_BASE_URL", "EVAL_MODEL_API_KEY", "EVAL_MODEL_NAME")
               if not os.environ.get(key)]
    if missing:
        raise SystemExit("live mode needs " + ", ".join(missing))
    config, _policy = gateway_model_config(LLMConfig(
        model_type="openai", model_name=os.environ["EVAL_MODEL_NAME"],
        api_key=os.environ["EVAL_MODEL_API_KEY"], api_base_url=os.environ["EVAL_MODEL_BASE_URL"]))
    model = LLMFactory.create_llm(config).llm

    def plan(messages, _case):
        message = model.invoke(messages)
        content = message.content if isinstance(message.content, str) else ""
        return content, usage_from(message)
    return plan


# --- Running cases -----------------------------------------------------------

def run_case(env: Environment, case: dict, planner: Planner, now: datetime,
             hybrid: bool, tolerance: float) -> dict[str, Any]:
    from app.planning import MetricCandidate, MetricPlanningError, parse_metric_plan
    from fastapi import HTTPException

    from apps.graph_gateway.prompts import metric_planning_messages
    from apps.graph_gateway.service import _validated_plan_request
    from apps.metrics.crud.metric import get_metric_candidates
    from apps.metrics.service.dimension_values import planning_dimension_values
    from apps.metrics.service.query_planner import preview_metric_query_plan

    gold_metric = (case.get("gold") or {}).get("metric")
    expected = case["expected"]
    result: dict[str, Any] = {"id": case["id"], "category": case["category"], "question": case["question"],
                              "expected_outcome": expected["outcome"], "gold_metric": gold_metric,
                              "candidates": [], "recalled": None, "planned_metric": None,
                              "outcome": None, "error": None, "passed": False, "usage": {}}
    started = perf_counter()
    with Session(env.meta) as session:
        candidates = get_metric_candidates(session, case["question"], 1, DATASOURCE_ID, limit=10,
                                           current_user=ADMIN, hybrid=hybrid)
        result["candidates"] = [item["metric_code"] for item in candidates]
        if gold_metric:
            result["recalled"] = gold_metric in result["candidates"]
        if not candidates:
            result.update(outcome="refusal", error="metric_not_found")
        else:
            known = planning_dimension_values(session, candidates, ADMIN, DATASOURCE_ID)
            prompt_candidates = [
                {**item, "dimension_values": known[item["metric_version_id"]]}
                if item["metric_version_id"] in known else item
                for item in candidates
            ]
            messages = metric_planning_messages(case["question"], prompt_candidates, now)
            try:
                content, result["usage"] = planner(messages, case)
                plan = parse_metric_plan(content, [MetricCandidate.model_validate(item) for item in candidates])
                result["planned_metric"] = next(item["metric_code"] for item in candidates
                                                if item["metric_id"] == plan.metric_id)
                metric_id, _version_id, payload = _validated_plan_request(plan.model_dump(mode="json"))
                compiled = preview_metric_query_plan(session, metric_id, payload, 1, ADMIN)
                with env.data.connect() as connection:
                    cursor = connection.execute(text(compiled["sql"]))
                    rows = [dict(row._mapping) for row in cursor]
                result.update(outcome="success", rows=rows)
            except MetricPlanningError as exc:
                result.update(outcome="rejected", error=exc.code)
            except HTTPException as exc:
                result.update(outcome="rejected", error=str(exc.detail))
            except Exception as exc:  # provider or network failure in live mode
                result.update(outcome="error", error=type(exc).__name__)
    result["latency_ms"] = round((perf_counter() - started) * 1000, 1)
    if expected["outcome"] == "refusal":
        result["passed"] = result["outcome"] in ("refusal", "rejected")
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
    results = [run_case(env, case, planner, now, hybrid, suite_info["float_tolerance"]) for case in cases]
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

"""Synthetic sales metric suite: data, published metrics and questions.

Everything here is fictional. The suite is the single source for ``cases.yaml``
and ``sales_orders.csv`` (regenerate with ``build.py``).

Each case has a *gold plan*: the metric plan a careful analyst would write. The
expected rows are computed from that plan by ``oracle_rows`` in plain Python,
independently of the SQL compiler, so a scripted run checks the compiler and a
live-model run checks the planner against the same answer key.
"""
from __future__ import annotations

import csv
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

SUITE_DIR = Path(__file__).resolve().parent
DATA_FILE = SUITE_DIR / "sales_orders.csv"
CASES_FILE = SUITE_DIR / "cases.yaml"

SUITE_NAME = "metric-sales-v1"
# Fixed "today" for relative periods: Friday 2026-09-25, Asia/Shanghai.
NOW = datetime(2026, 9, 25, 10, 0, 0)
TABLE = "sales_orders"
DIMENSIONS = ["region", "channel", "category"]

REGIONS = [("华东", 4), ("华南", 3), ("华北", 3), ("西南", 2)]
# Channels are stored as codes; business users call them 线上 / 门店 / 分销.
CHANNELS = [("online", 5), ("store", 3), ("distributor", 2)]
CHANNEL_LABELS = {"online": "线上", "store": "门店", "distributor": "分销"}
CATEGORIES = [("家电", 899.0), ("服饰", 239.0), ("食品", 59.0)]

COLUMNS = ["order_id", "paid_at", "region", "channel", "category", "customer_id",
           "quantity", "amount", "discount_amount", "refund_amount", "status"]
FIELD_TYPES = {"order_id": "bigint", "paid_at": "timestamp", "region": "varchar",
               "channel": "varchar", "category": "varchar", "customer_id": "bigint",
               "quantity": "int", "amount": "numeric", "discount_amount": "numeric",
               "refund_amount": "numeric", "status": "varchar"}


def _weighted(rng: random.Random, options: list[tuple[Any, int]]) -> Any:
    total = sum(weight for _value, weight in options)
    pick = rng.randrange(total)
    for value, weight in options:
        if pick < weight:
            return value
        pick -= weight
    raise AssertionError("unreachable")


def generate_orders() -> list[dict[str, Any]]:
    """Deterministic orders from 2025-07-01 to 2026-09-24 (the day before NOW)."""
    rng = random.Random(20260925)
    orders = []
    day = date(2025, 7, 1)
    while day < NOW.date():
        for _ in range(rng.randrange(1, 4)):
            category, base_price = CATEGORIES[rng.randrange(len(CATEGORIES))]
            quantity = rng.randrange(1, 6)
            amount = round(quantity * base_price * (0.8 + rng.randrange(0, 41) / 100), 2)
            discount = round(amount * rng.randrange(5, 16) / 100, 2) if rng.randrange(3) == 0 else 0.0
            status = _weighted(rng, [("paid", 8), ("cancelled", 1), ("refunded", 1)])
            refund = round(amount - discount, 2) if status == "refunded" else 0.0
            orders.append({
                "order_id": len(orders) + 1,
                "paid_at": datetime.combine(day, datetime.min.time())
                + timedelta(hours=rng.randrange(8, 22), minutes=rng.randrange(60)),
                "region": _weighted(rng, REGIONS),
                "channel": _weighted(rng, CHANNELS),
                "category": category,
                "customer_id": rng.randrange(1, 121),
                "quantity": quantity,
                "amount": amount,
                "discount_amount": discount,
                "refund_amount": refund,
                "status": status,
            })
        day += timedelta(days=1)
    return orders


def write_orders(orders: list[dict[str, Any]], path: Path = DATA_FILE) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for order in orders:
            writer.writerow({**order, "paid_at": order["paid_at"].strftime("%Y-%m-%d %H:%M:%S")})


def load_orders(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            {
                **row,
                "order_id": int(row["order_id"]),
                "paid_at": datetime.strptime(row["paid_at"], "%Y-%m-%d %H:%M:%S"),
                "customer_id": int(row["customer_id"]),
                "quantity": int(row["quantity"]),
                "amount": float(row["amount"]),
                "discount_amount": float(row["discount_amount"]),
                "refund_amount": float(row["refund_amount"]),
            }
            for row in csv.DictReader(handle)
        ]


# --- Published metrics ------------------------------------------------------

COMPLETED = {"field": "status", "operator": "in", "value": ["paid", "refunded"]}


@dataclass(frozen=True)
class Metric:
    code: str
    name: str
    aliases: list[str]
    description: str
    expression: str
    aggregation: str
    filters: list[dict[str, Any]]
    unit: str
    oracle: Callable[[list[dict[str, Any]]], Optional[float]]


def _sum(key: str) -> Callable[[list[dict[str, Any]]], Optional[float]]:
    return lambda rows: sum(row[key] for row in rows) if rows else None


METRICS = [
    Metric("gmv", "成交总额", ["GMV", "成交额", "流水"],
           "已支付订单（含之后退款的订单）的原始金额合计，不扣优惠和退款",
           "amount", "SUM", [COMPLETED], "元", _sum("amount")),
    Metric("net_sales", "净销售额", ["净收入", "实收金额"],
           "扣除优惠金额和退款金额后的实际收入",
           "amount - discount_amount - refund_amount", "SUM", [COMPLETED], "元",
           lambda rows: sum(r["amount"] - r["discount_amount"] - r["refund_amount"] for r in rows)
           if rows else None),
    Metric("order_count", "订单数", ["单量", "订单量"],
           "已支付订单（含之后退款的订单）数量，不含取消订单",
           "order_id", "COUNT", [COMPLETED], "单", lambda rows: len(rows)),
    Metric("buyer_count", "下单客户数", ["买家数", "购买人数"],
           "有已支付订单的去重客户数量",
           "customer_id", "COUNT_DISTINCT", [COMPLETED], "人",
           lambda rows: len({row["customer_id"] for row in rows})),
    Metric("avg_order_value", "客单价", ["平均订单金额", "AOV"],
           "成交总额除以订单数",
           "SUM(amount) / COUNT(order_id)", "CUSTOM", [COMPLETED], "元",
           lambda rows: sum(row["amount"] for row in rows) / len(rows) if rows else None),
    Metric("refund_amount", "退款金额", ["退款额"],
           "已退款订单退还给客户的金额",
           "refund_amount", "SUM", [{"field": "status", "operator": "=", "value": "refunded"}], "元",
           _sum("refund_amount")),
    Metric("discount_total", "优惠金额", ["折扣金额", "让利金额"],
           "已支付订单享受的优惠金额合计",
           "discount_amount", "SUM", [COMPLETED], "元", _sum("discount_amount")),
    Metric("units_sold", "销量", ["销售件数", "件数"],
           "已支付订单的商品件数合计",
           "quantity", "SUM", [COMPLETED], "件", _sum("quantity")),
    Metric("cancelled_orders", "取消订单数", ["取消单量"],
           "被取消的订单数量",
           "order_id", "COUNT", [{"field": "status", "operator": "=", "value": "cancelled"}], "单",
           lambda rows: len(rows)),
]
METRICS_BY_CODE = {metric.code: metric for metric in METRICS}

# What an administrator would enter so the planner can map 线上 to "online".
MANUAL_DIMENSION_VALUES = {
    "channel": [{"value": value, "label": label} for value, label in CHANNEL_LABELS.items()],
}


# --- Questions ----------------------------------------------------------------

def period(start: str, end: str) -> dict[str, str]:
    return {"start": start + "T00:00:00", "end": end + "T00:00:00"}


def eq(field_name: str, value: Any) -> dict[str, Any]:
    return {"field": field_name, "operator": "=", "value": value}


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    question: str
    metric: Optional[str]  # None: the catalog cannot answer, expect a refusal
    dimensions: list[str] = field(default_factory=list)
    filters: list[dict[str, Any]] = field(default_factory=list)
    time_range: Optional[dict[str, str]] = None
    note: str = ""
    # Earlier questions of the same conversation, oldest first.
    context: list[str] = field(default_factory=list)

    def gold_plan(self) -> Optional[dict[str, Any]]:
        if self.metric is None:
            return None
        return {"metric": self.metric, "dimensions": self.dimensions, "filters": self.filters,
                "time_range": self.time_range, "limit": None}


AUG = period("2026-08-01", "2026-09-01")

CASES = [
    # Plain metric questions
    Case("basic-gmv-total", "basic", "总的成交总额是多少？", "gmv"),
    Case("basic-orders-aug", "basic", "2026年8月的订单数", "order_count", time_range=AUG),
    Case("basic-net-sales-dec", "basic", "2025年12月净销售额是多少", "net_sales",
         time_range=period("2025-12-01", "2026-01-01")),
    Case("basic-cancelled-aug", "basic", "2026年8月有多少取消订单数", "cancelled_orders", time_range=AUG),
    # Absolute periods
    Case("abs-month-without-year", "time_absolute", "8月的销量", "units_sold", time_range=AUG,
         note="月份不带年份时取最近一个已开始的月份"),
    Case("abs-past-month-without-year", "time_absolute", "11月的成交总额", "gmv",
         time_range=period("2025-11-01", "2025-12-01"), note="今天是 2026-09-25，最近的 11 月在 2025 年"),
    Case("abs-year", "time_absolute", "2025年的退款金额", "refund_amount",
         time_range=period("2025-01-01", "2026-01-01")),
    Case("abs-quarter", "time_absolute", "2026年第二季度的下单客户数", "buyer_count",
         time_range=period("2026-04-01", "2026-07-01")),
    Case("abs-day", "time_absolute", "2026年9月1日的订单数", "order_count",
         time_range=period("2026-09-01", "2026-09-02")),
    Case("abs-month-range", "time_absolute", "2026年7月到8月的优惠金额", "discount_total",
         time_range=period("2026-07-01", "2026-09-01")),
    # Relative periods (today is Friday 2026-09-25)
    Case("rel-last-month", "time_relative", "上个月的净销售额", "net_sales", time_range=AUG),
    Case("rel-this-month", "time_relative", "本月成交总额是多少", "gmv",
         time_range=period("2026-09-01", "2026-10-01")),
    Case("rel-last-7-days", "time_relative", "近7天的订单数", "order_count",
         time_range=period("2026-09-19", "2026-09-26")),
    Case("rel-last-30-days", "time_relative", "近30天的下单客户数", "buyer_count",
         time_range=period("2026-08-27", "2026-09-26")),
    Case("rel-last-week", "time_relative", "上周的销量", "units_sold",
         time_range=period("2026-09-14", "2026-09-21")),
    Case("rel-yesterday", "time_relative", "昨天的成交总额", "gmv",
         time_range=period("2026-09-24", "2026-09-25")),
    Case("rel-this-year", "time_relative", "今年以来的下单客户数", "buyer_count",
         time_range=period("2026-01-01", "2027-01-01")),
    Case("rel-last-year", "time_relative", "去年的取消订单数", "cancelled_orders",
         time_range=period("2025-01-01", "2026-01-01")),
    Case("rel-this-quarter", "time_relative", "本季度的退款金额", "refund_amount",
         time_range=period("2026-07-01", "2026-10-01")),
    Case("rel-last-quarter", "time_relative", "上季度的客单价", "avg_order_value",
         time_range=period("2026-04-01", "2026-07-01")),
    # Group by dimensions
    Case("dim-region", "dimension", "8月各区域的净销售额", "net_sales", ["region"], time_range=AUG),
    Case("dim-channel", "dimension", "按渠道看上个月的订单数", "order_count", ["channel"], time_range=AUG),
    Case("dim-category-year", "dimension", "2025年各品类的销量", "units_sold", ["category"],
         time_range=period("2025-01-01", "2026-01-01")),
    Case("dim-two", "dimension", "8月各区域各品类的成交总额", "gmv", ["region", "category"], time_range=AUG),
    # Filters on dimension values
    Case("filter-region", "filter", "华东地区8月的成交总额", "gmv", filters=[eq("region", "华东")],
         time_range=AUG),
    Case("filter-channel-label", "filter", "线上渠道上个月的订单数", "order_count",
         filters=[eq("channel", "online")], time_range=AUG, note="线上 是 online 的业务名称"),
    Case("filter-channel-label-store", "filter", "门店渠道今年的净销售额", "net_sales",
         filters=[eq("channel", "store")], time_range=period("2026-01-01", "2027-01-01")),
    Case("filter-in", "filter", "华东和华南8月的销量", "units_sold",
         filters=[{"field": "region", "operator": "in", "value": ["华东", "华南"]}], time_range=AUG),
    Case("filter-not", "filter", "除了分销渠道以外，8月的成交总额是多少", "gmv",
         filters=[{"field": "channel", "operator": "!=", "value": "distributor"}], time_range=AUG),
    Case("filter-with-dimension", "filter", "线上渠道8月各区域的订单数", "order_count", ["region"],
         filters=[eq("channel", "online")], time_range=AUG),
    Case("filter-category", "filter", "家电品类今年的退款金额", "refund_amount",
         filters=[eq("category", "家电")], time_range=period("2026-01-01", "2027-01-01")),
    Case("filter-combined", "filter", "2025年第四季度华南地区线上渠道的净销售额", "net_sales",
         filters=[eq("region", "华南"), eq("channel", "online")],
         time_range=period("2025-10-01", "2026-01-01")),
    # Synonyms and paraphrases (candidate recall)
    Case("syn-gmv", "synonym", "8月GMV", "gmv", time_range=AUG),
    Case("syn-order-alias", "synonym", "上个月单量", "order_count", time_range=AUG),
    Case("syn-buyer-alias", "synonym", "8月买家数", "buyer_count", time_range=AUG),
    Case("syn-aov-dimension", "synonym", "今年各渠道的AOV", "avg_order_value", ["channel"],
         time_range=period("2026-01-01", "2027-01-01")),
    Case("syn-buyer-paraphrase", "synonym", "上个月下单的客户有多少", "buyer_count", time_range=AUG,
         note="名称不在问题里，靠字符重叠召回"),
    Case("syn-discount-paraphrase", "synonym", "8月一共让利了多少钱", "discount_total", time_range=AUG,
         note="只有语义召回能找到"),
    Case("syn-cancelled-paraphrase", "synonym", "8月被取消的订单有几单", "cancelled_orders", time_range=AUG,
         note="字符重叠同时召回订单数与取消订单数，需要模型区分"),
    Case("syn-units-paraphrase", "synonym", "8月卖出去多少件", "units_sold", time_range=AUG,
         note="只有语义召回能找到"),
    Case("syn-refund-paraphrase", "synonym", "上个月退了多少钱", "refund_amount", time_range=AUG,
         note="只有语义召回能找到"),
    # Follow-ups that only make sense with the earlier question
    Case("follow-month", "follow_up", "那7月呢？", "net_sales", ["region"],
         time_range=period("2026-07-01", "2026-08-01"), context=["8月各区域的净销售额"]),
    Case("follow-filter", "follow_up", "只看线上渠道", "order_count",
         filters=[eq("channel", "online")], time_range=AUG, context=["上个月的订单数"]),
    Case("follow-metric-switch", "follow_up", "换成订单数看看", "order_count", ["region"],
         time_range=AUG, context=["8月各区域的净销售额"]),
    # Outside the catalog or ambiguous: the planner must not guess
    Case("refuse-margin", "refusal", "上个月的毛利率是多少", None),
    Case("refuse-inventory", "refusal", "各仓库现在的库存量", None),
    Case("clarify-sales", "refusal", "上个月卖了多少钱", None,
         note="成交总额与净销售额都说得通，应追问而不是猜"),
]


def oracle_rows(orders: list[dict[str, Any]], case: Case) -> list[dict[str, Any]]:
    """Answer key computed in Python, independent of the SQL compiler."""
    metric = METRICS_BY_CODE[case.metric]

    def matches(row: dict[str, Any], condition: dict[str, Any]) -> bool:
        value, operator = row[condition["field"]], condition["operator"]
        if operator == "=":
            return value == condition["value"]
        if operator == "!=":
            return value != condition["value"]
        if operator == "in":
            return value in condition["value"]
        raise ValueError(f"oracle does not support operator {operator}")

    selected = [row for row in orders
                if all(matches(row, condition) for condition in metric.filters + case.filters)]
    if case.time_range:
        start = datetime.fromisoformat(case.time_range["start"])
        end = datetime.fromisoformat(case.time_range["end"])
        selected = [row for row in selected if start <= row["paid_at"] < end]
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in selected:
        groups.setdefault(tuple(row[name] for name in case.dimensions), []).append(row)
    if not case.dimensions:
        groups = {(): selected}
    result = []
    for key, rows in sorted(groups.items()):
        value = metric.oracle(rows)
        result.append({**dict(zip(case.dimensions, key, strict=True)),
                       metric.code: round(value, 4) if isinstance(value, float) else value})
    return result

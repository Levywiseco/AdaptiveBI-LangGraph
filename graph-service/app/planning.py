"""Strict metric-plan contracts; metric formulas remain in the business gateway."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FilterOperator = Literal[
    "=", "!=", ">", ">=", "<", "<=", "in", "not_in", "between",
    "like", "not_like", "is_null", "is_not_null",
]


class MetricCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: int = Field(gt=0)
    metric_code: str = Field(min_length=1, max_length=128)
    metric_name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    description: str | None = Field(default=None, max_length=4000)
    metric_version_id: int = Field(gt=0)
    metric_version: int = Field(gt=0)
    dimensions: list[str] = Field(default_factory=list, max_length=100)
    time_field: str | None = Field(default=None, max_length=255)
    grain: str | None = Field(default=None, max_length=128)
    unit: str | None = Field(default=None, max_length=64)
    required_tables: list[str] = Field(default_factory=list, max_length=10)
    score: int = Field(ge=1)


class MetricPlanFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=255)
    operator: FilterOperator = "="
    value: Any = None


class MetricPlanTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid_range(self):
        if (self.start.tzinfo is None) != (self.end.tzinfo is None):
            raise ValueError("time_range_timezone_mismatch")
        if self.end <= self.start:
            raise ValueError("time_range_invalid")
        return self


class MetricQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: int = Field(gt=0)
    metric_version_id: int = Field(gt=0)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    filters: list[MetricPlanFilter] = Field(default_factory=list, max_length=50)
    time_range: MetricPlanTimeRange | None = None
    limit: int | None = Field(default=None, ge=1, le=10000)


class MetricPlanningError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def planning_prompt(question: str, candidates: list[MetricCandidate]) -> list[dict[str, str]]:
    """Return a provider-neutral prompt without formulas or connection details."""
    catalog = [candidate.model_dump(mode="json") for candidate in candidates]
    return [
        {
            "role": "system",
            "content": (
                "Select exactly one authorized metric and return one JSON object only. "
                "Use only the candidate metric/version IDs, dimensions and time field. "
                "Do not write SQL or invent fields. Use start-inclusive, end-exclusive time ranges. "
                "Required keys: metric_id, metric_version_id, dimensions, filters, time_range, limit. "
                "Authorized candidates: " + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
            ),
        },
        {"role": "user", "content": question},
    ]


def parse_metric_plan(raw: str, candidates: list[MetricCandidate]) -> MetricQueryPlan:
    """Validate model output against the exact authorized candidate snapshot."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 16000 or "```" in raw:
        raise MetricPlanningError("metric_plan_invalid")
    try:
        parsed = json.loads(raw)
        required = {"metric_id", "metric_version_id", "dimensions", "filters", "time_range", "limit"}
        if not isinstance(parsed, dict) or set(parsed) != required:
            raise ValueError("metric_plan_keys_invalid")
        plan = MetricQueryPlan.model_validate(parsed)
    except Exception as exc:
        raise MetricPlanningError("metric_plan_invalid") from exc

    candidate = next(
        (
            item for item in candidates
            if item.metric_id == plan.metric_id
            and item.metric_version_id == plan.metric_version_id
        ),
        None,
    )
    if candidate is None:
        raise MetricPlanningError("metric_not_authorized")

    actual_dimensions = {name.casefold(): name for name in candidate.dimensions}
    normalized_dimensions: list[str] = []
    seen: set[str] = set()
    for requested in plan.dimensions:
        key = requested.casefold()
        if key not in actual_dimensions:
            raise MetricPlanningError("metric_dimension_not_allowed")
        if key not in seen:
            normalized_dimensions.append(actual_dimensions[key])
            seen.add(key)

    filter_fields = dict(actual_dimensions)
    if candidate.time_field:
        filter_fields[candidate.time_field.casefold()] = candidate.time_field
    normalized_filters: list[MetricPlanFilter] = []
    for item in plan.filters:
        key = item.field.casefold()
        if key not in filter_fields:
            raise MetricPlanningError("metric_filter_not_allowed")
        normalized_filters.append(item.model_copy(update={"field": filter_fields[key]}))

    if plan.time_range is not None and not candidate.time_field:
        raise MetricPlanningError("metric_time_range_not_allowed")
    return plan.model_copy(
        update={"dimensions": normalized_dimensions, "filters": normalized_filters}
    )

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, field_validator, model_validator

Aggregation = Literal["SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX", "CUSTOM"]
MetricFilterOperator = Literal[
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not_in",
    "between",
    "like",
    "not_like",
    "is_null",
    "is_not_null",
]


def _clean_string_list(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


class MetricCalculation(BaseModel):
    expression: str = Field(min_length=1, max_length=8000)
    aggregation: Aggregation = "SUM"
    time_field: Optional[str] = Field(default=None, max_length=255)
    grain: Optional[str] = Field(default=None, max_length=128)
    required_tables: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    join_rules: list[dict[str, Any]] = Field(default_factory=list)
    unit: Optional[str] = Field(default=None, max_length=64)
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None

    @field_validator("expression")
    @classmethod
    def strip_expression(cls, value: str) -> str:
        return value.strip()

    @field_validator("required_tables", "dimensions")
    @classmethod
    def normalize_lists(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)

    @model_validator(mode="after")
    def validate_effective_range(self):
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self


class MetricCreate(MetricCalculation):
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    aliases: list[str] = Field(default_factory=list)
    description: Optional[str] = Field(default=None, max_length=4000)
    datasource_id: int

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", cleaned):
            raise ValueError("code must start with a letter and contain only a-z, 0-9 and underscore")
        return cleaned

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)


class MetricVersionCreate(BaseModel):
    expression: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    aggregation: Optional[Aggregation] = None
    time_field: Optional[str] = Field(default=None, max_length=255)
    grain: Optional[str] = Field(default=None, max_length=128)
    required_tables: Optional[list[str]] = None
    dimensions: Optional[list[str]] = None
    filters: Optional[list[dict[str, Any]]] = None
    join_rules: Optional[list[dict[str, Any]]] = None
    unit: Optional[str] = Field(default=None, max_length=64)
    effective_from: Optional[datetime] = None
    effective_to: Optional[datetime] = None

    @field_validator("expression")
    @classmethod
    def strip_optional_expression(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value

    @field_validator("required_tables", "dimensions")
    @classmethod
    def normalize_optional_lists(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return _clean_string_list(value) if value is not None else value


class MetricDefinitionUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    aliases: Optional[list[str]] = None
    description: Optional[str] = Field(default=None, max_length=4000)
    datasource_id: Optional[int] = None
    calculation: Optional[MetricVersionCreate] = None

    @field_validator("code")
    @classmethod
    def validate_optional_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", cleaned):
            raise ValueError("code must start with a letter and contain only a-z, 0-9 and underscore")
        return cleaned

    @field_validator("name")
    @classmethod
    def strip_optional_name(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value is not None else value

    @field_validator("aliases")
    @classmethod
    def normalize_optional_aliases(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return _clean_string_list(value) if value is not None else value


class MetricPublish(BaseModel):
    review_note: str = Field(min_length=3, max_length=2000)

    @field_validator("review_note")
    @classmethod
    def strip_review_note(cls, value: str) -> str:
        return value.strip()


class MetricQueryFilter(BaseModel):
    field: str = Field(min_length=1, max_length=255)
    operator: MetricFilterOperator = "="
    value: Any = None

    @field_validator("field")
    @classmethod
    def strip_field(cls, value: str) -> str:
        return value.strip()

    @field_validator("operator", mode="before")
    @classmethod
    def normalize_operator(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
        return {"==": "=", "<>": "!="}.get(normalized, normalized)


class MetricTimeRange(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_range(self):
        if (self.start.tzinfo is None) != (self.end.tzinfo is None):
            raise ValueError("time_range.start and time_range.end must use the same timezone mode")
        if self.end <= self.start:
            raise ValueError("time_range.end must be later than time_range.start")
        return self


class MetricQueryPlanRequest(BaseModel):
    version_id: int | None = Field(default=None, gt=0)
    dimensions: list[str] = Field(default_factory=list, max_length=20)
    filters: list[MetricQueryFilter] = Field(default_factory=list, max_length=50)
    time_range: MetricTimeRange | None = None
    limit: int | None = Field(default=None, ge=1, le=10000)

    @field_validator("dimensions")
    @classmethod
    def normalize_dimensions(cls, value: list[str]) -> list[str]:
        return _clean_string_list(value)


class MetricQueryPlanRead(BaseModel):
    metric_id: int
    metric_code: str
    metric_name: str
    metric_version_id: int
    metric_version: int
    datasource_id: int
    datasource_type: str | None
    dimensions: list[str]
    applied_filters: list[dict[str, Any]]
    time_range: dict[str, str] | None
    required_tables: list[str]
    row_permission_applied: bool = False
    sql: str
    sql_fingerprint: str
    compiler: str = "metric-plan-v1"


class MetricCandidateRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_id: int
    metric_code: str
    metric_name: str
    aliases: list[str]
    description: str | None
    metric_version_id: int
    metric_version: int
    dimensions: list[str]
    time_field: str | None
    grain: str | None
    unit: str | None
    required_tables: list[str]
    score: int


class MetricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    oid: int
    code: str
    name: str
    aliases: list[str]
    description: Optional[str]
    datasource_id: int
    datasource_name: Optional[str] = None
    owner_user_id: int
    status: str
    current_version_id: Optional[int]
    current_version: Optional[dict[str, Any]] = None
    latest_version: Optional[dict[str, Any]] = None
    versions: Optional[list[dict[str, Any]]] = None
    has_draft: bool = False
    created_at: datetime
    updated_at: datetime


class MetricDimensionValueItem(BaseModel):
    """One stored value; ``label`` is the business name the planner may see in questions."""

    model_config = ConfigDict(extra="forbid")

    value: StrictStr | StrictInt | StrictFloat
    label: str | None = Field(default=None, max_length=64)

    @field_validator("value")
    @classmethod
    def bounded_value(cls, value):
        if isinstance(value, str) and not (0 < len(value) <= 64):
            raise ValueError("value must contain 1 to 64 characters")
        return value

    @field_validator("label")
    @classmethod
    def blank_label_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class MetricDimensionValuesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: list[MetricDimensionValueItem] = Field(min_length=1, max_length=200)


class MetricDimensionValueRead(BaseModel):
    dimension: str
    values: list[dict[str, Any]]
    status: str
    source: str
    updated_at: datetime | None = None

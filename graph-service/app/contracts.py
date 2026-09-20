from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator
from uuid import UUID


class QueryState(TypedDict, total=False):
    question: str
    datasource_id: str
    schema: str
    sql: str
    columns: list[str]
    rows: list[list]
    truncated: bool
    answer: str
    status: Literal["running", "completed", "rejected", "failed"]
    error: str


@dataclass(frozen=True)
class Principal:
    """Trusted server-side identity; never populated from request JSON."""

    user_id: str
    workspace_id: str
    datasource_ids: frozenset[str]


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list]
    truncated: bool = False


class QueryTools(Protocol):
    def authorize(self, principal: Principal, datasource_id: str) -> None: ...
    def schema(self, principal: Principal, datasource_id: str) -> str: ...
    def validate(self, sql: str) -> None: ...
    def execute(self, principal: Principal, datasource_id: str, sql: str) -> QueryResult: ...


class DemoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str


class RunEvent(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    sequence: int
    type: Literal["progress", "completed", "rejected", "failed"]
    node: str
    payload: dict


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    datasource_id: Literal["synthetic-sales"] = "synthetic-sales"

    @field_validator("question")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("question_required")
        return value


class InternalQuestion(QuestionRequest):
    run_id: UUID


class MetricQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    datasource_id: int = Field(gt=0)

    @field_validator("question")
    @classmethod
    def metric_question_not_blank(cls, value):
        if not value.strip():
            raise ValueError("question_required")
        return value


class InternalMetricQuestion(MetricQuestionRequest):
    run_id: UUID


class MetricCandidateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: int = Field(gt=0)
    metric_version_id: int = Field(gt=0)


class MetricModelRequest(InternalMetricQuestion):
    candidates: list[MetricCandidateRef] = Field(min_length=1, max_length=20)


class MetricCompileRequest(InternalMetricQuestion):
    plan: dict


class ModelUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class SafeResult(BaseModel):
    status: Literal["completed", "failed", "rejected"]
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    truncated: bool = False
    answer: str = ""
    error: Literal["model_timeout", "model_call_failed", "model_output_invalid",
                   "gateway_unavailable", "gateway_rejected", "sql_validation_failed",
                   "datasource_access_denied", "query_execution_failed",
                   "graph_execution_failed"] | None = None


class SafeResponse(SafeResult):
    mode: Literal["synthetic-live"] = "synthetic-live"
    run_id: UUID
    model_config_id: int | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model_calls: int | None = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)


class MetricPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["metric-plan"] = "metric-plan"
    run_id: UUID
    status: Literal["completed", "failed", "rejected"]
    metric_id: int | None = None
    metric_code: str | None = None
    metric_name: str | None = None
    metric_version_id: int | None = None
    metric_version: int | None = None
    dimensions: list[str] = Field(default_factory=list)
    time_range: dict[str, str] | None = None
    unit: str | None = None
    sql_fingerprint: str | None = None
    compiler: str | None = None
    error: Literal["metric_not_found", "metric_plan_invalid", "metric_not_authorized",
                   "metric_dimension_not_allowed", "metric_filter_not_allowed",
                   "metric_time_range_not_allowed", "metric_compile_failed",
                   "model_timeout", "model_call_failed", "model_output_invalid",
                   "gateway_unavailable", "gateway_rejected", "graph_execution_failed"] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model_calls: int | None = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)


class ModelCallError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

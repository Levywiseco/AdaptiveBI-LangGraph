from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    # Earlier questions of the same conversation, oldest first (follow-ups).
    context: list[str] = Field(default_factory=list, max_length=3)

    @field_validator("context")
    @classmethod
    def bounded_context(cls, value):
        if any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in value):
            raise ValueError("context_invalid")
        return value

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


class PlanRepair(BaseModel):
    """One rejected model reply and why it was rejected, for a bounded retry."""
    model_config = ConfigDict(extra="forbid")
    previous: str = Field(min_length=1, max_length=16000)
    error: Literal["metric_plan_invalid", "metric_not_authorized", "metric_dimension_not_allowed",
                   "metric_filter_not_allowed", "metric_time_range_not_allowed", "metric_compile_failed"]


class MetricModelRequest(InternalMetricQuestion):
    candidates: list[MetricCandidateRef] = Field(min_length=1, max_length=20)
    repairs: list[PlanRepair] = Field(default_factory=list, max_length=3)


class MetricCompileRequest(InternalMetricQuestion):
    plan: dict


class MetricExecuteRequest(InternalMetricQuestion):
    plan: dict


class ModelUsage(BaseModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class SafeResponse(BaseModel):
    """An explicit allowlist even when an internal service adds fields."""
    mode: Literal["synthetic-live"] = "synthetic-live"
    run_id: UUID
    status: Literal["completed", "failed", "rejected"]
    model_config_id: int | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    truncated: bool = False
    answer: str = ""
    error: Literal["model_timeout", "model_call_failed", "model_output_invalid",
                   "gateway_unavailable", "gateway_rejected", "sql_validation_failed",
                   "datasource_access_denied", "query_execution_failed",
                   "graph_execution_failed"] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model_calls: int | None = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)


class MetricPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["metric-plan"] = "metric-plan"
    run_id: UUID
    status: Literal["completed", "failed", "rejected", "needs_clarification"]
    clarification: str | None = None
    clarification_reason: Literal["ambiguous", "unsupported"] | None = None
    repairs: int = Field(default=0, ge=0)
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
                   "gateway_unavailable", "gateway_rejected", "graph_execution_failed",
                   "graph_deadline_exceeded"] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model_calls: int | None = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)


class MetricQueryResponse(BaseModel):
    """Public allowlist for the plan-and-execute path; SQL and formulas never appear."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["metric-query"] = "metric-query"
    run_id: UUID
    status: Literal["completed", "failed", "rejected", "needs_clarification"]
    clarification: str | None = None
    clarification_reason: Literal["ambiguous", "unsupported"] | None = None
    repairs: int = Field(default=0, ge=0)
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
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    row_count: int = Field(default=0, ge=0)
    truncated: bool = False
    error: Literal["metric_not_found", "metric_plan_invalid", "metric_not_authorized",
                   "metric_dimension_not_allowed", "metric_filter_not_allowed",
                   "metric_time_range_not_allowed", "metric_compile_failed",
                   "metric_execution_failed", "metric_execution_timeout",
                   "model_timeout", "model_call_failed", "model_output_invalid",
                   "gateway_unavailable", "gateway_rejected", "graph_execution_failed",
                   "graph_deadline_exceeded"] | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    model_calls: int | None = Field(default=0, ge=0)
    elapsed_ms: float = Field(default=0, ge=0)

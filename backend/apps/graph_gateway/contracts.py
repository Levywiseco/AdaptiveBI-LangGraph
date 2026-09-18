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

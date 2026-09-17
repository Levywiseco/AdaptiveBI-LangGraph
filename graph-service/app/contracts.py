from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict


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

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MetricDefinition(SQLModel, table=True):
    """Stable identity and ownership of a business metric."""

    __tablename__ = "metric_definition"
    __table_args__ = (
        UniqueConstraint("oid", "code", name="uq_metric_definition_oid_code"),
    )

    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(always=True), primary_key=True),
    )
    oid: int = Field(sa_column=Column(BigInteger, nullable=False, index=True))
    code: str = Field(max_length=128, nullable=False)
    name: str = Field(max_length=255, nullable=False)
    aliases: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    description: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    datasource_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("core_datasource.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
    )
    owner_user_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    status: str = Field(default="draft", max_length=32, nullable=False)
    current_version_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime(timezone=False), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime(timezone=False), nullable=False),
    )


class MetricVersion(SQLModel, table=True):
    """Versioned calculation rules. Published rows are never edited in place."""

    __tablename__ = "metric_version"
    __table_args__ = (
        UniqueConstraint("metric_id", "version", name="uq_metric_version_number"),
    )

    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(always=True), primary_key=True),
    )
    metric_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("metric_definition.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    version: int = Field(nullable=False)
    expression: str = Field(sa_column=Column(Text, nullable=False))
    aggregation: str = Field(default="SUM", max_length=32, nullable=False)
    time_field: Optional[str] = Field(default=None, max_length=255)
    grain: Optional[str] = Field(default=None, max_length=128)
    required_tables: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    dimensions: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    filters: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    join_rules: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    unit: Optional[str] = Field(default=None, max_length=64)
    status: str = Field(default="draft", max_length=32, nullable=False, index=True)
    validation_status: str = Field(default="pending", max_length=32, nullable=False)
    validation_message: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    effective_from: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=False), nullable=True),
    )
    effective_to: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=False), nullable=True),
    )
    created_by: int = Field(sa_column=Column(BigInteger, nullable=False))
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime(timezone=False), nullable=False),
    )
    published_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=False), nullable=True),
    )
    reviewed_by: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    reviewed_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=False), nullable=True),
    )
    review_note: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))


class MetricDimensionValue(SQLModel, table=True):
    """Known values of one dimension of a published metric version.

    Kept outside ``metric_version`` so published calculation rules stay immutable
    while this derived metadata can be refreshed. ``values`` holds
    ``{"value": ..., "label": ...}`` entries; ``source="manual"`` rows are
    administrator-maintained and never overwritten by sampling.
    """

    __tablename__ = "metric_dimension_value"
    __table_args__ = (
        UniqueConstraint("metric_version_id", "dimension", name="uq_metric_dimension_value"),
    )

    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, Identity(always=True), primary_key=True),
    )
    metric_version_id: int = Field(
        sa_column=Column(
            BigInteger,
            ForeignKey("metric_version.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    dimension: str = Field(max_length=255, nullable=False)
    values: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, default=list),
    )
    # ok | high_cardinality | failed
    status: str = Field(default="ok", max_length=32, nullable=False)
    source: str = Field(default="sampled", max_length=32, nullable=False)
    updated_by: Optional[int] = Field(default=None, sa_column=Column(BigInteger, nullable=True))
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime(timezone=False), nullable=False),
    )

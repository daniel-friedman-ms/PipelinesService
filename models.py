"""SQLAlchemy models — Pipeline, PipelineVersion, PipelineTestRun, PipelineDeployment."""

from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, DateTime, JSON, Text, ForeignKey, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    flow: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    definition: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    __table_args__ = (UniqueConstraint("pipeline_id", "version", name="uq_pipeline_version"),)


class PipelineTestRun(Base):
    __tablename__ = "pipeline_test_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer)
    image_filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result: Mapped[dict] = mapped_column(JSON)
    execution_time_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PipelineDeployment(Base):
    __tablename__ = "pipeline_deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pipeline_id: Mapped[str] = mapped_column(String(36), ForeignKey("pipelines.id", ondelete="CASCADE"), index=True)
    pipeline_version: Mapped[int] = mapped_column(Integer)
    flow: Mapped[str] = mapped_column(String(100), index=True)
    action: Mapped[str] = mapped_column(String(20))  # "publish" or "rollback"
    deployed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    deployed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

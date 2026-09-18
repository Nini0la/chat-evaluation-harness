import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TurnStatus(enum.StrEnum):
    pending = "pending"
    completed = "completed"
    failed = "failed"


class ModelDeployment(Base):
    __tablename__ = "model_deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(50))
    model_id: Mapped[str] = mapped_column(String(255))
    model_version: Mapped[str] = mapped_column(String(255))
    endpoint_reference: Mapped[str | None] = mapped_column(String(1000))
    configuration_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


Index(
    "uq_active_model_deployment",
    ModelDeployment.active,
    unique=True,
    postgresql_where=ModelDeployment.active.is_(True),
    sqlite_where=ModelDeployment.active.is_(True),
)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    anonymous_tester_id: Mapped[str | None] = mapped_column(String(255), index=True)
    model_deployment_id: Mapped[int] = mapped_column(ForeignKey("model_deployments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_metadata: Mapped[dict | None] = mapped_column(JSON)
    model_deployment: Mapped[ModelDeployment] = relationship()
    turns: Mapped[list["Turn"]] = relationship(back_populates="session")


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("session_id", "turn_number", name="uq_turn_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    turn_number: Mapped[int] = mapped_column(Integer)
    user_message: Mapped[str] = mapped_column(Text)
    assistant_response: Mapped[str | None] = mapped_column(Text)
    request_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inference_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    time_to_first_token_ms: Mapped[float | None] = mapped_column(Float)
    inference_latency_ms: Mapped[float | None] = mapped_column(Float)
    total_latency_ms: Mapped[float | None] = mapped_column(Float)
    tokens_per_second: Mapped[float | None] = mapped_column(Float)
    model_deployment_id: Mapped[int] = mapped_column(ForeignKey("model_deployments.id"))
    status: Mapped[TurnStatus] = mapped_column(Enum(TurnStatus), default=TurnStatus.pending)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session: Mapped[Session] = relationship(back_populates="turns")
    feedback: Mapped[list["Feedback"]] = relationship(back_populates="turn")


Index(
    "uq_pending_turn_per_session",
    Turn.session_id,
    unique=True,
    postgresql_where=text("status = 'pending'"),
    sqlite_where=text("status = 'pending'"),
)


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("rating IS NULL OR rating IN (-1, 1)", name="ck_feedback_rating"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), index=True)
    rating: Mapped[int | None] = mapped_column(Integer)
    failure_category: Mapped[str | None] = mapped_column(String(100))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    turn: Mapped[Turn] = relationship(back_populates="feedback")

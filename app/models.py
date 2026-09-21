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
    shadows: Mapped[list["SessionShadow"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="SessionShadow.id"
    )


class SessionShadow(Base):
    __tablename__ = "session_shadows"
    __table_args__ = (
        CheckConstraint("slot IN ('red', 'green', 'blue')", name="ck_session_shadow_slot"),
        UniqueConstraint("session_id", "slot", name="uq_session_shadow_slot"),
        UniqueConstraint("session_id", "model_deployment_id", name="uq_session_shadow_deployment"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    slot: Mapped[str] = mapped_column(String(5))
    model_deployment_id: Mapped[int] = mapped_column(ForeignKey("model_deployments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session: Mapped[Session] = relationship(back_populates="shadows")
    model_deployment: Mapped[ModelDeployment] = relationship()
    responses: Mapped[list["ShadowResponse"]] = relationship(back_populates="session_shadow")


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
    shadow_responses: Mapped[list["ShadowResponse"]] = relationship(
        back_populates="turn",
        cascade="all, delete-orphan",
        order_by="ShadowResponse.session_shadow_id",
    )
    eval_candidates: Mapped[list["EvalCandidate"]] = relationship(
        back_populates="turn", cascade="all, delete-orphan"
    )


Index(
    "uq_pending_turn_per_session",
    Turn.session_id,
    unique=True,
    postgresql_where=text("status = 'pending'"),
    sqlite_where=text("status = 'pending'"),
)


class ShadowResponse(Base):
    __tablename__ = "shadow_responses"
    __table_args__ = (
        UniqueConstraint("turn_id", "session_shadow_id", name="uq_shadow_response_turn"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), index=True)
    session_shadow_id: Mapped[int] = mapped_column(ForeignKey("session_shadows.id"), index=True)
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
    status: Mapped[TurnStatus] = mapped_column(Enum(TurnStatus), default=TurnStatus.pending)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    provider_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    turn: Mapped[Turn] = relationship(back_populates="shadow_responses")
    session_shadow: Mapped[SessionShadow] = relationship(back_populates="responses")

    @property
    def slot(self) -> str:
        return self.session_shadow.slot

    @property
    def model_deployment_id(self) -> int:
        return self.session_shadow.model_deployment_id


class EvalCandidate(Base):
    __tablename__ = "eval_candidates"
    __table_args__ = (
        CheckConstraint(
            "source IN ('primary', 'red', 'green', 'blue')",
            name="ck_eval_candidate_source",
        ),
        UniqueConstraint("turn_id", "source", name="uq_eval_candidate_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), index=True)
    source: Mapped[str] = mapped_column(String(10))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    turn: Mapped[Turn] = relationship(back_populates="eval_candidates")


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

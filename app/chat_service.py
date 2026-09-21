import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.models import (
    ModelDeployment,
    Session,
    SessionShadow,
    ShadowResponse,
    Turn,
    TurnStatus,
    utcnow,
)
from app.providers.base import GenerationResult, ModelMessage, ModelProvider, ProviderError

ProviderFactory = Callable[[object], ModelProvider]
logger = logging.getLogger(__name__)
APPLICATION_ERROR_MESSAGE = "Unexpected application error"


class TurnConflict(RuntimeError):
    """Another request is already submitting a turn for the session."""


class ApplicationTurnError(RuntimeError):
    """A sanitized application failure safe to return to clients."""


def _recover_stale_turn(db: DbSession, turn: Turn, stale_after_seconds: float) -> bool:
    started_at = turn.request_started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if utcnow() - started_at <= timedelta(seconds=stale_after_seconds):
        return False
    completed_at = utcnow()
    for shadow in turn.shadow_responses:
        if shadow.status == TurnStatus.pending:
            shadow.status = TurnStatus.failed
            shadow.error_type = "interrupted_request"
            shadow.error_message = "Interrupted model request"
            shadow.response_completed_at = completed_at
    if turn.assistant_response is not None:
        turn.status = TurnStatus.completed
    else:
        turn.status = TurnStatus.failed
        turn.error_type = "interrupted_request"
        turn.error_message = "Interrupted model request"
        turn.response_completed_at = completed_at
    db.commit()
    return True


def _messages_for_session(db: DbSession, session_id: uuid.UUID) -> list[ModelMessage]:
    turns = db.scalars(
        select(Turn)
        .where(Turn.session_id == session_id, Turn.status == TurnStatus.completed)
        .order_by(Turn.turn_number, Turn.created_at)
    ).all()
    messages: list[ModelMessage] = []
    for turn in turns:
        messages.append(ModelMessage(role="user", content=turn.user_message))
        if turn.assistant_response is not None:
            messages.append(ModelMessage(role="assistant", content=turn.assistant_response))
    return messages


def _messages_for_shadow(
    db: DbSession,
    session_id: uuid.UUID,
    session_shadow_id: int,
    before_turn_number: int,
) -> list[ModelMessage]:
    turns = db.scalars(
        select(Turn)
        .options(selectinload(Turn.shadow_responses))
        .where(Turn.session_id == session_id, Turn.turn_number < before_turn_number)
        .order_by(Turn.turn_number, Turn.created_at)
    ).all()
    messages: list[ModelMessage] = []
    for turn in turns:
        messages.append(ModelMessage(role="user", content=turn.user_message))
        response = next(
            (
                item
                for item in turn.shadow_responses
                if item.session_shadow_id == session_shadow_id
                and item.status == TurnStatus.completed
            ),
            None,
        )
        if response is not None and response.assistant_response is not None:
            messages.append(ModelMessage(role="assistant", content=response.assistant_response))
    return messages


def _is_turn_number_conflict(exc: IntegrityError) -> bool:
    detail = str(exc).casefold()
    return (
        "uq_turn_number" in detail
        or "uq_pending_turn_per_session" in detail
        or detail.count("unique constraint failed: turns.session_id") > 0
        or (
            "unique constraint failed" in detail
            and "turns.session_id" in detail
            and "turns.turn_number" in detail
        )
    )


def _persist_failure(
    db: DbSession,
    record_type: type[Turn] | type[ShadowResponse],
    record_id: uuid.UUID,
    *,
    error_type: str,
    error_message: str,
    total_latency_ms: float,
) -> None:
    failed_record = db.get(record_type, record_id)
    if failed_record is None:
        raise RuntimeError("Pending response disappeared before failure finalization")
    failed_record.status = TurnStatus.failed
    failed_record.error_type = error_type
    failed_record.error_message = error_message
    failed_record.total_latency_ms = total_latency_ms
    failed_record.response_completed_at = utcnow()
    db.commit()


def _recover_failure(
    db: DbSession,
    record_type: type[Turn] | type[ShadowResponse],
    record_id: uuid.UUID,
    original: Exception,
    *,
    error_type: str,
    error_message: str,
    total_latency_ms: float,
) -> None:
    original_traceback = original.__traceback__
    db.rollback()
    try:
        _persist_failure(
            db,
            record_type,
            record_id,
            error_type=error_type,
            error_message=error_message,
            total_latency_ms=total_latency_ms,
        )
    except Exception:
        db.rollback()
        logger.exception("Could not persist failed response %s", record_id)
        raise original.with_traceback(original_traceback) from None


def _apply_result(
    record: Turn | ShadowResponse,
    result: GenerationResult,
    provider_started_at,
    measured_ms: float,
) -> None:
    completed_ms = result.completed_offset_ms
    if completed_ms is None:
        completed_ms = measured_ms
    record.assistant_response = result.text
    record.input_tokens = result.input_tokens
    record.output_tokens = result.output_tokens
    record.provider_request_id = result.provider_request_id
    record.provider_metadata = result.provider_metadata
    record.total_latency_ms = completed_ms
    record.time_to_first_token_ms = result.first_token_offset_ms
    record.inference_latency_ms = result.inference_latency_ms
    if result.inference_started_offset_ms is not None:
        record.inference_started_at = provider_started_at + timedelta(
            milliseconds=result.inference_started_offset_ms
        )
        if record.inference_latency_ms is None:
            record.inference_latency_ms = max(
                0.0, completed_ms - result.inference_started_offset_ms
            )
    if result.first_token_offset_ms is not None:
        record.first_token_at = provider_started_at + timedelta(
            milliseconds=result.first_token_offset_ms
        )
    if result.completed_offset_ms is not None:
        record.response_completed_at = provider_started_at + timedelta(milliseconds=completed_ms)
    else:
        record.response_completed_at = utcnow()
    if record.output_tokens is not None and record.inference_latency_ms:
        record.tokens_per_second = record.output_tokens / (record.inference_latency_ms / 1000)


def _generate_response(
    db: DbSession,
    record: Turn | ShadowResponse,
    deployment: ModelDeployment,
    messages: list[ModelMessage],
    provider_factory: ProviderFactory,
    started_clock: float,
    *,
    complete: bool = True,
) -> Exception | None:
    record_type = type(record)
    record_id = record.id
    try:
        provider_started_at = utcnow()
        provider = provider_factory(deployment)
        try:
            result = provider.generate(messages, deployment)
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    logger.warning("Could not close provider for deployment %s", deployment.id)
        measured_ms = (time.perf_counter() - started_clock) * 1000
        _apply_result(record, result, provider_started_at, measured_ms)
        if complete:
            record.status = TurnStatus.completed
        db.commit()
        return None
    except ProviderError as exc:
        _recover_failure(
            db,
            record_type,
            record_id,
            exc,
            error_type=exc.error_type,
            error_message=str(exc)[:2000],
            total_latency_ms=(time.perf_counter() - started_clock) * 1000,
        )
        return exc
    except Exception as exc:
        logger.exception("Unexpected exception while processing response %s", record_id)
        _recover_failure(
            db,
            record_type,
            record_id,
            exc,
            error_type="application_exception",
            error_message=APPLICATION_ERROR_MESSAGE,
            total_latency_ms=(time.perf_counter() - started_clock) * 1000,
        )
        return ApplicationTurnError(APPLICATION_ERROR_MESSAGE)


def run_turn(
    db: DbSession,
    session: Session,
    user_message: str,
    provider_factory: ProviderFactory,
    stale_after_seconds: float = 900,
) -> Turn:
    pending_turn = db.scalar(
        select(Turn)
        .options(selectinload(Turn.shadow_responses))
        .where(
            Turn.session_id == session.id,
            Turn.status == TurnStatus.pending,
        )
    )
    if pending_turn is not None and not _recover_stale_turn(db, pending_turn, stale_after_seconds):
        raise TurnConflict("Another message is already being submitted for this session")

    next_number = (
        db.scalar(select(func.max(Turn.turn_number)).where(Turn.session_id == session.id)) or 0
    ) + 1
    started_clock = time.perf_counter()
    started_at = utcnow()
    turn = Turn(
        session_id=session.id,
        turn_number=next_number,
        user_message=user_message,
        request_started_at=started_at,
        model_deployment_id=session.model_deployment_id,
        status=TurnStatus.pending,
    )
    shadow_ids = [shadow.id for shadow in session.shadows]
    for session_shadow_id in shadow_ids:
        turn.shadow_responses.append(
            ShadowResponse(
                session_shadow_id=session_shadow_id,
                request_started_at=started_at,
                status=TurnStatus.pending,
            )
        )
    db.add(turn)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_turn_number_conflict(exc):
            raise TurnConflict(
                "Another message is already being submitted for this session"
            ) from exc
        raise
    db.refresh(turn)
    turn_id = turn.id

    messages = _messages_for_session(db, session.id)
    messages.append(ModelMessage(role="user", content=user_message))
    primary_error = _generate_response(
        db,
        turn,
        session.model_deployment,
        messages,
        provider_factory,
        started_clock,
        complete=not shadow_ids,
    )

    for session_shadow_id in shadow_ids:
        session_shadow = db.get(SessionShadow, session_shadow_id)
        shadow_response = db.scalar(
            select(ShadowResponse).where(
                ShadowResponse.turn_id == turn_id,
                ShadowResponse.session_shadow_id == session_shadow_id,
            )
        )
        if session_shadow is None or shadow_response is None:
            raise RuntimeError("Pending shadow response disappeared before generation")
        shadow_messages = _messages_for_shadow(db, session.id, session_shadow_id, turn.turn_number)
        shadow_messages.append(ModelMessage(role="user", content=user_message))
        shadow_started_clock = time.perf_counter()
        shadow_response.request_started_at = utcnow()
        db.commit()
        _generate_response(
            db,
            shadow_response,
            session_shadow.model_deployment,
            shadow_messages,
            provider_factory,
            shadow_started_clock,
        )

    if primary_error is not None:
        raise primary_error
    if shadow_ids:
        completed_turn = db.get(Turn, turn_id)
        if completed_turn is None:
            raise RuntimeError("Pending turn disappeared before completion")
        completed_turn.status = TurnStatus.completed
        db.commit()
    return db.scalar(
        select(Turn)
        .options(selectinload(Turn.shadow_responses).selectinload(ShadowResponse.session_shadow))
        .execution_options(populate_existing=True)
        .where(Turn.id == turn_id)
    )

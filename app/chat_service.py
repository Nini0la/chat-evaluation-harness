import logging
import time
import uuid
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.models import Session, Turn, TurnStatus, utcnow
from app.providers.base import ModelMessage, ModelProvider, ProviderError

ProviderFactory = Callable[[object], ModelProvider]
logger = logging.getLogger(__name__)
APPLICATION_ERROR_MESSAGE = "Unexpected application error"


class TurnConflict(RuntimeError):
    """Another request is already submitting a turn for the session."""


class ApplicationTurnError(RuntimeError):
    """A sanitized application failure safe to return to clients."""


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
    turn_id: uuid.UUID,
    *,
    error_type: str,
    error_message: str,
    total_latency_ms: float,
) -> None:
    failed_turn = db.get(Turn, turn_id)
    if failed_turn is None:
        raise RuntimeError("Pending turn disappeared before failure finalization")
    failed_turn.status = TurnStatus.failed
    failed_turn.error_type = error_type
    failed_turn.error_message = error_message
    failed_turn.total_latency_ms = total_latency_ms
    failed_turn.response_completed_at = utcnow()
    db.commit()


def _recover_failure(
    db: DbSession,
    turn_id: uuid.UUID,
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
            turn_id,
            error_type=error_type,
            error_message=error_message,
            total_latency_ms=total_latency_ms,
        )
    except Exception:
        db.rollback()
        logger.exception("Could not persist failed turn %s", turn_id)
        raise original.with_traceback(original_traceback) from None


def run_turn(
    db: DbSession,
    session: Session,
    user_message: str,
    provider_factory: ProviderFactory,
) -> Turn:
    pending_turn_id = db.scalar(
        select(Turn.id).where(
            Turn.session_id == session.id,
            Turn.status == TurnStatus.pending,
        )
    )
    if pending_turn_id is not None:
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
    try:
        provider_started_at = utcnow()
        result = provider_factory(session.model_deployment).generate(
            messages, session.model_deployment
        )
        measured_ms = (time.perf_counter() - started_clock) * 1000
        completed_ms = result.completed_offset_ms
        if completed_ms is None:
            completed_ms = measured_ms
        turn.assistant_response = result.text
        turn.input_tokens = result.input_tokens
        turn.output_tokens = result.output_tokens
        turn.provider_request_id = result.provider_request_id
        turn.provider_metadata = result.provider_metadata
        turn.total_latency_ms = completed_ms
        turn.time_to_first_token_ms = result.first_token_offset_ms
        turn.inference_latency_ms = result.inference_latency_ms
        if result.inference_started_offset_ms is not None:
            turn.inference_started_at = provider_started_at + timedelta(
                milliseconds=result.inference_started_offset_ms
            )
            if turn.inference_latency_ms is None:
                turn.inference_latency_ms = max(
                    0.0, completed_ms - result.inference_started_offset_ms
                )
        if result.first_token_offset_ms is not None:
            turn.first_token_at = provider_started_at + timedelta(
                milliseconds=result.first_token_offset_ms
            )
        if result.completed_offset_ms is not None:
            turn.response_completed_at = provider_started_at + timedelta(milliseconds=completed_ms)
        else:
            turn.response_completed_at = utcnow()
        if turn.output_tokens is not None and turn.inference_latency_ms:
            turn.tokens_per_second = turn.output_tokens / (turn.inference_latency_ms / 1000)
        turn.status = TurnStatus.completed
        db.commit()
        db.refresh(turn)
        return turn
    except ProviderError as exc:
        _recover_failure(
            db,
            turn_id,
            exc,
            error_type=exc.error_type,
            error_message=str(exc)[:2000],
            total_latency_ms=(time.perf_counter() - started_clock) * 1000,
        )
        raise
    except Exception as exc:
        logger.exception("Unexpected exception while processing turn %s", turn_id)
        _recover_failure(
            db,
            turn_id,
            exc,
            error_type="application_exception",
            error_message=APPLICATION_ERROR_MESSAGE,
            total_latency_ms=(time.perf_counter() - started_clock) * 1000,
        )
        raise ApplicationTurnError(APPLICATION_ERROR_MESSAGE) from exc

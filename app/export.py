import csv
import io
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.models import Feedback, Session, Turn


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def export_records(db: DbSession) -> list[dict[str, Any]]:
    turns: Iterable[Turn] = db.scalars(
        select(Turn)
        .options(
            selectinload(Turn.feedback),
            selectinload(Turn.session).selectinload(Session.model_deployment),
        )
        .order_by(Turn.created_at, Turn.turn_number)
    ).all()
    records = []
    for turn in turns:
        deployment = turn.session.model_deployment
        records.append(
            {
                "session": {
                    "id": str(turn.session_id),
                    "anonymous_tester_id": turn.session.anonymous_tester_id,
                    "created_at": _iso(turn.session.created_at),
                    "client_metadata": turn.session.client_metadata,
                },
                "model": {
                    "deployment_id": deployment.id,
                    "provider": deployment.provider,
                    "model_id": deployment.model_id,
                    "version": deployment.model_version,
                    "configuration": deployment.configuration_json,
                },
                "turn": {
                    "id": str(turn.id),
                    "turn_number": turn.turn_number,
                    "user_message": turn.user_message,
                    "assistant_response": turn.assistant_response,
                    "status": turn.status.value,
                    "error_type": turn.error_type,
                    "error_message": turn.error_message,
                    "request_started_at": _iso(turn.request_started_at),
                    "inference_started_at": _iso(turn.inference_started_at),
                    "first_token_at": _iso(turn.first_token_at),
                    "response_completed_at": _iso(turn.response_completed_at),
                    "input_tokens": turn.input_tokens,
                    "output_tokens": turn.output_tokens,
                    "time_to_first_token_ms": turn.time_to_first_token_ms,
                    "inference_latency_ms": turn.inference_latency_ms,
                    "total_latency_ms": turn.total_latency_ms,
                    "tokens_per_second": turn.tokens_per_second,
                    "provider_request_id": turn.provider_request_id,
                    "provider_metadata": turn.provider_metadata,
                },
                "feedback": [_feedback_record(item) for item in turn.feedback],
            }
        )
    return records


def _feedback_record(feedback: Feedback) -> dict[str, Any]:
    return {
        "id": str(feedback.id),
        "rating": feedback.rating,
        "failure_category": feedback.failure_category,
        "comment": feedback.comment,
        "created_at": _iso(feedback.created_at),
    }


def as_jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def as_csv(records: list[dict[str, Any]]) -> str:
    fields = [
        "session_id",
        "tester_id",
        "model_deployment_id",
        "provider",
        "model_id",
        "model_version",
        "turn_id",
        "turn_number",
        "user_message",
        "assistant_response",
        "status",
        "error_type",
        "error_message",
        "request_started_at",
        "first_token_at",
        "response_completed_at",
        "input_tokens",
        "output_tokens",
        "time_to_first_token_ms",
        "inference_latency_ms",
        "total_latency_ms",
        "tokens_per_second",
        "provider_request_id",
        "feedback_ratings",
        "feedback_categories",
        "feedback_comments",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for record in records:
        session, model, turn, feedback = (
            record["session"],
            record["model"],
            record["turn"],
            record["feedback"],
        )
        row = {
            "session_id": session["id"],
            "tester_id": session["anonymous_tester_id"],
            "model_deployment_id": model["deployment_id"],
            "provider": model["provider"],
            "model_id": model["model_id"],
            "model_version": model["version"],
            "turn_id": turn["id"],
            "turn_number": turn["turn_number"],
            "user_message": turn["user_message"],
            "assistant_response": turn["assistant_response"],
            "status": turn["status"],
            "error_type": turn["error_type"],
            "error_message": turn["error_message"],
            "request_started_at": turn["request_started_at"],
            "first_token_at": turn["first_token_at"],
            "response_completed_at": turn["response_completed_at"],
            "input_tokens": turn["input_tokens"],
            "output_tokens": turn["output_tokens"],
            "time_to_first_token_ms": turn["time_to_first_token_ms"],
            "inference_latency_ms": turn["inference_latency_ms"],
            "total_latency_ms": turn["total_latency_ms"],
            "tokens_per_second": turn["tokens_per_second"],
            "provider_request_id": turn["provider_request_id"],
            "feedback_ratings": "|".join(str(item["rating"]) for item in feedback),
            "feedback_categories": "|".join(item["failure_category"] or "" for item in feedback),
            "feedback_comments": "|".join(item["comment"] or "" for item in feedback),
        }
        writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return output.getvalue()

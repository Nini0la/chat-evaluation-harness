import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.models import EvalCandidate, Session, SessionShadow, ShadowResponse, Turn


def export_eval_candidates(db: DbSession) -> list[dict[str, Any]]:
    candidates = db.scalars(
        select(EvalCandidate)
        .options(
            selectinload(EvalCandidate.turn)
            .selectinload(Turn.session)
            .selectinload(Session.model_deployment),
            selectinload(EvalCandidate.turn)
            .selectinload(Turn.shadow_responses)
            .selectinload(ShadowResponse.session_shadow)
            .selectinload(SessionShadow.model_deployment),
        )
        .order_by(EvalCandidate.created_at, EvalCandidate.id)
    ).all()
    records = []
    for candidate in candidates:
        turn = candidate.turn
        if candidate.source == "primary":
            response = turn.assistant_response
            deployment = turn.session.model_deployment
        else:
            shadow = next(item for item in turn.shadow_responses if item.slot == candidate.source)
            response = shadow.assistant_response
            deployment = shadow.session_shadow.model_deployment
        records.append(
            {
                "schema_version": "eval_candidate_v1",
                "candidate_id": str(candidate.id),
                "captured_at": candidate.created_at.isoformat(),
                "source": candidate.source,
                "note": candidate.note,
                "provenance": {
                    "session_id": str(turn.session_id),
                    "turn_id": str(turn.id),
                    "turn_number": turn.turn_number,
                },
                "model": {
                    "deployment_id": deployment.id,
                    "provider": deployment.provider,
                    "model_id": deployment.model_id,
                    "model_version": deployment.model_version,
                    "endpoint_reference": deployment.endpoint_reference,
                    "configuration": deployment.configuration_json,
                },
                "prompt": turn.user_message,
                "response": response,
            }
        )
    return records


def eval_candidates_jsonl(records: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)

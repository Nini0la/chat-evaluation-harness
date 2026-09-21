import hmac
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.auth import require_admin, require_tester
from app.chat_service import ApplicationTurnError, TurnConflict, run_turn
from app.config import Settings, get_settings
from app.database import get_db
from app.eval_candidates import eval_candidates_jsonl, export_eval_candidates
from app.export import as_csv, as_jsonl, export_records
from app.models import (
    EvalCandidate,
    Feedback,
    ModelDeployment,
    Session,
    SessionShadow,
    ShadowResponse,
    Turn,
)
from app.providers.base import ProviderError
from app.providers.factory import build_provider
from app.schemas import (
    DeploymentCreate,
    DeploymentOptionRead,
    DeploymentRead,
    EvalCandidateCreate,
    EvalCandidateRead,
    FeedbackCreate,
    FeedbackRead,
    MessageCreate,
    SessionCreate,
    SessionRead,
    TurnRead,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app = FastAPI(title=app_settings.app_name, version="0.1.0")
    app.state.settings = app_settings
    app.state.provider_factory = lambda deployment: build_provider(deployment, app_settings)
    app.dependency_overrides[get_settings] = lambda: app_settings
    assets = Path(__file__).parent
    templates = Jinja2Templates(directory=assets / "templates")
    app.mount("/static", StaticFiles(directory=assets / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.post("/access")
    def tester_access(access_code: str = Form()):
        if not hmac.compare_digest(access_code, app_settings.tester_access_code.get_secret_value()):
            raise HTTPException(status_code=401, detail="Invalid access code")
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "tester_access",
            access_code,
            httponly=True,
            secure=app_settings.cookie_secure,
            samesite="strict",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    @app.post("/admin/access")
    def admin_access(access_code: str = Form()):
        if not hmac.compare_digest(access_code, app_settings.admin_access_code.get_secret_value()):
            raise HTTPException(status_code=401, detail="Invalid admin code")
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie(
            "admin_access",
            access_code,
            httponly=True,
            secure=app_settings.cookie_secure,
            samesite="strict",
            max_age=60 * 60 * 8,
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/sessions",
        response_model=SessionRead,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_tester)],
    )
    def create_session(payload: SessionCreate, db: DbSession = Depends(get_db)) -> Session:
        deployment = db.scalar(
            select(ModelDeployment)
            .where(ModelDeployment.active.is_(True))
            .order_by(ModelDeployment.activated_at.desc(), ModelDeployment.id.desc())
        )
        if deployment is None:
            raise HTTPException(status_code=503, detail="No active model deployment")
        shadow_ids = [shadow.model_deployment_id for shadow in payload.shadows]
        if deployment.id in shadow_ids:
            raise HTTPException(
                status_code=422, detail="A shadow deployment cannot equal the primary deployment"
            )
        shadow_deployments = {
            item.id: item
            for item in db.scalars(
                select(ModelDeployment).where(ModelDeployment.id.in_(shadow_ids))
            ).all()
        }
        missing_ids = sorted(set(shadow_ids) - shadow_deployments.keys())
        if missing_ids:
            raise HTTPException(
                status_code=422, detail=f"Unknown shadow deployment IDs: {missing_ids}"
            )
        slot_order = {"red": 0, "green": 1, "blue": 2}
        session = Session(
            anonymous_tester_id=payload.anonymous_tester_id,
            client_metadata=payload.client_metadata,
            model_deployment_id=deployment.id,
            shadows=[
                SessionShadow(slot=shadow.slot, model_deployment_id=shadow.model_deployment_id)
                for shadow in sorted(payload.shadows, key=lambda item: slot_order[item.slot])
            ],
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @app.get(
        "/api/deployments",
        response_model=list[DeploymentOptionRead],
        dependencies=[Depends(require_tester)],
    )
    def list_deployments(db: DbSession = Depends(get_db)) -> list[ModelDeployment]:
        return list(db.scalars(select(ModelDeployment).order_by(ModelDeployment.id)).all())

    @app.get(
        "/api/sessions/{session_id}",
        response_model=SessionRead,
        dependencies=[Depends(require_tester)],
    )
    def get_session(session_id: uuid.UUID, db: DbSession = Depends(get_db)) -> Session:
        session = db.get(Session, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    @app.get(
        "/api/sessions/{session_id}/turns",
        response_model=list[TurnRead],
        dependencies=[Depends(require_tester)],
    )
    def list_turns(session_id: uuid.UUID, db: DbSession = Depends(get_db)) -> list[Turn]:
        if db.get(Session, session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return list(
            db.scalars(
                select(Turn)
                .options(
                    selectinload(Turn.shadow_responses).selectinload(ShadowResponse.session_shadow)
                )
                .where(Turn.session_id == session_id)
                .order_by(Turn.turn_number, Turn.created_at)
            ).all()
        )

    @app.post(
        "/api/sessions/{session_id}/messages",
        response_model=TurnRead,
        dependencies=[Depends(require_tester)],
    )
    def send_message(
        session_id: uuid.UUID,
        payload: MessageCreate,
        db: DbSession = Depends(get_db),
    ) -> Turn:
        session = db.get(Session, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            stale_after_seconds = max(
                300.0,
                app_settings.model_timeout_seconds * 2 * (len(session.shadows) + 1),
            )
            return run_turn(
                db,
                session,
                payload.message,
                app.state.provider_factory,
                stale_after_seconds=stale_after_seconds,
            )
        except TurnConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ApplicationTurnError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post(
        "/api/turns/{turn_id}/feedback",
        response_model=FeedbackRead,
        status_code=201,
        dependencies=[Depends(require_tester)],
    )
    def create_feedback(
        turn_id: uuid.UUID,
        payload: FeedbackCreate,
        db: DbSession = Depends(get_db),
    ) -> Feedback:
        if db.get(Turn, turn_id) is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        feedback = Feedback(turn_id=turn_id, **payload.model_dump())
        db.add(feedback)
        db.commit()
        db.refresh(feedback)
        return feedback

    @app.post(
        "/api/turns/{turn_id}/eval-candidates",
        response_model=EvalCandidateRead,
        status_code=201,
        dependencies=[Depends(require_tester)],
    )
    def create_eval_candidate(
        turn_id: uuid.UUID,
        payload: EvalCandidateCreate,
        db: DbSession = Depends(get_db),
    ) -> EvalCandidate:
        turn = db.scalar(
            select(Turn)
            .options(
                selectinload(Turn.shadow_responses).selectinload(ShadowResponse.session_shadow)
            )
            .where(Turn.id == turn_id)
        )
        if turn is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        if payload.source == "primary":
            available = turn.assistant_response is not None
        else:
            available = any(
                item.slot == payload.source and item.assistant_response is not None
                for item in turn.shadow_responses
            )
        if not available:
            raise HTTPException(status_code=422, detail="Selected response is not available")
        existing = db.scalar(
            select(EvalCandidate).where(
                EvalCandidate.turn_id == turn_id,
                EvalCandidate.source == payload.source,
            )
        )
        if existing is not None:
            return existing
        candidate = EvalCandidate(turn_id=turn_id, **payload.model_dump())
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        return candidate

    @app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
    def admin_review(request: Request, db: DbSession = Depends(get_db)):
        sessions = list(
            db.scalars(
                select(Session)
                .options(
                    selectinload(Session.model_deployment),
                    selectinload(Session.turns).selectinload(Turn.feedback),
                )
                .order_by(Session.created_at.desc())
            ).all()
        )
        turns = [turn for session in sessions for turn in session.turns]
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={"sessions": sessions, "turns": turns},
        )

    @app.get("/admin/export", dependencies=[Depends(require_admin)])
    def export_interactions(
        format: str = Query(pattern="^(jsonl|csv)$"),
        db: DbSession = Depends(get_db),
    ) -> PlainTextResponse:
        records = export_records(db)
        if format == "jsonl":
            return PlainTextResponse(
                as_jsonl(records),
                media_type="application/x-ndjson",
                headers={"Content-Disposition": "attachment; filename=interactions.jsonl"},
            )
        return PlainTextResponse(
            as_csv(records),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=interactions.csv"},
        )

    @app.get("/admin/eval-candidates", dependencies=[Depends(require_admin)])
    def export_candidates(db: DbSession = Depends(get_db)) -> PlainTextResponse:
        records = export_eval_candidates(db)
        return PlainTextResponse(
            eval_candidates_jsonl(records),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=eval-candidates.jsonl"},
        )

    @app.post(
        "/admin/deployments",
        response_model=DeploymentRead,
        status_code=201,
        dependencies=[Depends(require_admin)],
    )
    def register_deployment(
        payload: DeploymentCreate, db: DbSession = Depends(get_db)
    ) -> ModelDeployment:
        now = datetime.now(UTC)
        if payload.activate:
            db.execute(
                update(ModelDeployment)
                .where(ModelDeployment.active.is_(True))
                .values(active=False, deactivated_at=now)
            )
        data = payload.model_dump(exclude={"activate"})
        deployment = ModelDeployment(
            **data,
            active=payload.activate,
            activated_at=now if payload.activate else None,
        )
        db.add(deployment)
        db.commit()
        db.refresh(deployment)
        return deployment

    return app


app = create_app()

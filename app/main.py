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
from app.export import as_csv, as_jsonl, export_records
from app.models import Feedback, ModelDeployment, Session, Turn
from app.providers.base import ProviderError
from app.providers.factory import build_provider
from app.schemas import (
    DeploymentCreate,
    DeploymentRead,
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
        session = Session(
            anonymous_tester_id=payload.anonymous_tester_id,
            client_metadata=payload.client_metadata,
            model_deployment_id=deployment.id,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

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
            return run_turn(db, session, payload.message, app.state.provider_factory)
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

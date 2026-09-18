import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import get_db
from app.main import create_app
from app.models import Feedback, ModelDeployment, Session, Turn, TurnStatus, utcnow
from app.providers.base import GenerationResult

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for PostgreSQL integration tests",
)


@pytest.fixture
def postgres_harness():
    engine = create_engine(POSTGRES_TEST_URL, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as connection:
        for model in (Feedback, Turn, Session, ModelDeployment):
            connection.execute(delete(model))

    application = create_app(
        Settings(
            environment="development",
            database_url=POSTGRES_TEST_URL,
            tester_access_code="postgres-test-access",
            admin_access_code="postgres-test-admin",
            cookie_secure=False,
        )
    )

    def override_db():
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_db
    application.state.test_session_factory = factory

    with factory() as db:
        deployment = ModelDeployment(
            provider="mock",
            model_id="postgres-concurrency-model",
            model_version="test-v1",
            endpoint_reference="mock://local",
            configuration_json={},
            active=True,
            activated_at=utcnow(),
        )
        db.add(deployment)
        db.commit()

    yield application, factory

    with engine.begin() as connection:
        for model in (Feedback, Turn, Session, ModelDeployment):
            connection.execute(delete(model))
    engine.dispose()


def _create_session(client):
    response = client.post(
        "/api/sessions",
        headers={"X-Access-Code": "postgres-test-access"},
        json={},
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_postgresql_rejects_two_pending_turns_with_different_numbers(postgres_harness):
    application, factory = postgres_harness
    with TestClient(application) as client:
        session_id = _create_session(client)

    with factory() as db:
        session = db.get(Session, session_id)
        db.add(
            Turn(
                session_id=session.id,
                turn_number=1,
                user_message="first pending message",
                request_started_at=utcnow(),
                model_deployment_id=session.model_deployment_id,
                status=TurnStatus.pending,
            )
        )
        db.commit()
        db.add(
            Turn(
                session_id=session.id,
                turn_number=2,
                user_message="competing pending message",
                request_started_at=utcnow(),
                model_deployment_id=session.model_deployment_id,
                status=TurnStatus.pending,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_competing_postgresql_requests_cannot_diverge_generation(postgres_harness):
    application, factory = postgres_harness

    class BlockingProvider:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.started = threading.Event()
            self.release = threading.Event()

        def generate(self, messages, deployment):
            with self.lock:
                self.calls += 1
            self.started.set()
            assert self.release.wait(timeout=10)
            return GenerationResult(text=f"reply:{messages[-1].content}")

    provider = BlockingProvider()
    application.state.provider_factory = lambda deployment: provider
    headers = {"X-Access-Code": "postgres-test-access"}

    with TestClient(application) as first_client, TestClient(application) as second_client:
        session_id = _create_session(first_client)
        url = f"/api/sessions/{session_id}/messages"
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                first_client.post,
                url,
                headers=headers,
                json={"message": "first"},
            )
            assert provider.started.wait(timeout=5)
            second = second_client.post(url, headers=headers, json={"message": "second"})
            provider.release.set()
            first = first_future.result(timeout=10)

    assert first.status_code == 200
    assert second.status_code == 409
    assert provider.calls == 1

    with factory() as db:
        turns = db.query(Turn).order_by(Turn.turn_number).all()
        assert len(turns) == 1
        assert turns[0].status == TurnStatus.completed
        assert turns[0].user_message == "first"
        assert turns[0].assistant_response == "reply:first"

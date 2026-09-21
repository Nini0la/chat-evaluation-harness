import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base, get_db
from app.main import create_app
from app.models import Session, Turn, TurnStatus, utcnow
from app.providers.base import GenerationResult, ProviderError
from tests.test_sessions import add_deployment


class RecordingProvider:
    def __init__(self, fail: bool = False):
        self.calls = []
        self.fail = fail

    def generate(self, messages, deployment):
        self.calls.append((messages, deployment.model_version))
        if self.fail:
            raise ProviderError("network_failure", "upstream unavailable")
        return GenerationResult(
            text=f"reply:{messages[-1].content}",
            input_tokens=10,
            output_tokens=5,
            provider_request_id="req-123",
            inference_started_offset_ms=2,
            first_token_offset_ms=12,
            completed_offset_ms=22,
        )


def create_session(client, app, headers):
    add_deployment(app, "v1")
    return client.post("/api/sessions", headers=headers, json={}).json()["id"]


def test_database_allows_only_one_pending_turn_per_session(client, app, auth_headers):
    session_id = create_session(client, app, auth_headers)
    with app.state.test_session_factory() as db:
        session = db.get(Session, uuid.UUID(session_id))
        db.add(
            Turn(
                session_id=session.id,
                turn_number=1,
                user_message="first in flight",
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
                user_message="second in flight",
                request_started_at=utcnow(),
                model_deployment_id=session.model_deployment_id,
                status=TurnStatus.pending,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_chat_reconstructs_ordered_history_and_persists_exact_response(client, app, auth_headers):
    provider = RecordingProvider()
    app.state.provider_factory = lambda deployment: provider
    session_id = create_session(client, app, auth_headers)

    first = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "one"}
    )
    second = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "two"}
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["assistant_response"] == "reply:one"
    assert [message.model_dump() for message in provider.calls[1][0]] == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "reply:one"},
        {"role": "user", "content": "two"},
    ]
    history = client.get(f"/api/sessions/{session_id}/turns", headers=auth_headers).json()
    assert [turn["turn_number"] for turn in history] == [1, 2]
    with app.state.test_session_factory() as db:
        turns = db.query(Turn).order_by(Turn.turn_number).all()
        assert turns[0].assistant_response == "reply:one"
        assert turns[0].status == TurnStatus.completed
        assert turns[0].provider_request_id == "req-123"


def test_latency_metrics_are_calculated_from_provider_events(client, app, auth_headers):
    app.state.provider_factory = lambda deployment: RecordingProvider()
    session_id = create_session(client, app, auth_headers)

    body = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "timing"}
    ).json()

    assert body["time_to_first_token_ms"] == 12
    assert body["inference_latency_ms"] == 20
    assert body["total_latency_ms"] == 22
    assert body["tokens_per_second"] == 250


def test_inference_duration_without_start_offset_does_not_invent_start_time(
    client, app, auth_headers
):
    class DurationOnlyProvider:
        def generate(self, messages, deployment):
            return GenerationResult(
                text="duration only",
                output_tokens=5,
                inference_latency_ms=20,
                completed_offset_ms=22,
            )

    app.state.provider_factory = lambda deployment: DurationOnlyProvider()
    session_id = create_session(client, app, auth_headers)

    body = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "timing"}
    ).json()

    assert body["inference_latency_ms"] == 20
    assert body["tokens_per_second"] == 250
    with app.state.test_session_factory() as db:
        turn = db.query(Turn).one()
        assert turn.inference_latency_ms == 20
        assert turn.inference_started_at is None


def test_provider_failure_is_persisted_and_returned_without_retry(client, app, auth_headers):
    provider = RecordingProvider(fail=True)
    app.state.provider_factory = lambda deployment: provider
    session_id = create_session(client, app, auth_headers)

    response = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "fail"}
    )

    assert response.status_code == 502
    assert len(provider.calls) == 1
    with app.state.test_session_factory() as db:
        turn = db.query(Turn).one()
        assert turn.status == TurnStatus.failed
        assert turn.error_type == "network_failure"
        assert turn.error_message == "upstream unavailable"
        assert turn.assistant_response is None


def test_real_provider_factory_uses_mock_adapter_for_message_request(client, app, auth_headers):
    session_id = create_session(client, app, auth_headers)

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        headers=auth_headers,
        json={"message": "factory path"},
    )

    assert response.status_code == 200
    assert response.json()["assistant_response"] == "Mock response: factory path"
    assert response.json()["provider_request_id"] == "mock-local"


def test_unexpected_application_exception_is_sanitized_everywhere(
    client, app, auth_headers, admin_headers, caplog
):
    secret = "DATABASE_PASSWORD=top-secret-value"

    class CrashingProvider:
        def generate(self, messages, deployment):
            raise RuntimeError(secret)

    app.state.provider_factory = lambda deployment: CrashingProvider()
    session_id = create_session(client, app, auth_headers)

    with (
        caplog.at_level(logging.ERROR),
        TestClient(app, raise_server_exceptions=False) as recovering_client,
    ):
        response = recovering_client.post(
            f"/api/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"message": "trigger bug"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Unexpected application error"}
    with app.state.test_session_factory() as db:
        turn = db.query(Turn).one()
        assert turn.status == TurnStatus.failed
        assert turn.error_type == "application_exception"
        assert turn.error_message == "Unexpected application error"
    assert secret not in response.text
    assert secret not in client.get("/admin/export?format=jsonl", headers=admin_headers).text
    assert secret not in client.get("/admin/export?format=csv", headers=admin_headers).text
    assert secret in caplog.text


def test_provider_event_offsets_use_provider_call_origin(monkeypatch, client, app, auth_headers):
    request_started = datetime(2026, 1, 1, tzinfo=UTC)
    provider_started = request_started + timedelta(seconds=5)
    times = iter((request_started, provider_started))
    monkeypatch.setattr("app.chat_service.utcnow", lambda: next(times))
    app.state.provider_factory = lambda deployment: RecordingProvider()
    session_id = create_session(client, app, auth_headers)

    response = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "timing"}
    )

    assert response.status_code == 200
    with app.state.test_session_factory() as db:
        turn = db.query(Turn).one()
        assert turn.request_started_at.replace(tzinfo=UTC) == request_started
        assert turn.inference_started_at.replace(tzinfo=UTC) == provider_started + timedelta(
            milliseconds=2
        )
        assert turn.first_token_at.replace(tzinfo=UTC) == provider_started + timedelta(
            milliseconds=12
        )


def test_finalization_validation_failure_rolls_back_and_marks_turn_failed(
    client, app, auth_headers
):
    class InvalidMetadataProvider:
        def generate(self, messages, deployment):
            return GenerationResult(
                text="generated but invalid", provider_metadata={"bad": object()}
            )

    app.state.provider_factory = lambda deployment: InvalidMetadataProvider()
    session_id = create_session(client, app, auth_headers)

    with TestClient(app, raise_server_exceptions=False) as recovering_client:
        response = recovering_client.post(
            f"/api/sessions/{session_id}/messages",
            headers=auth_headers,
            json={"message": "trigger invalid finalization"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Unexpected application error"}
    with app.state.test_session_factory() as db:
        turn = db.query(Turn).one()
        assert turn.status == TurnStatus.failed
        assert turn.error_type == "application_exception"
        assert turn.error_message == "Unexpected application error"
        assert turn.assistant_response is None


def test_pending_turn_number_conflict_returns_409_without_generation(client, app, auth_headers):
    provider = RecordingProvider()
    app.state.provider_factory = lambda deployment: provider
    session_id = create_session(client, app, auth_headers)
    with app.state.test_session_factory() as setup_db:
        session = setup_db.get(Session, uuid.UUID(session_id))
        setup_db.add(
            Turn(
                session_id=session.id,
                turn_number=1,
                user_message="concurrent request",
                request_started_at=utcnow(),
                model_deployment_id=session.model_deployment_id,
                status=TurnStatus.pending,
            )
        )
        setup_db.commit()

    def stale_number_db():
        with app.state.test_session_factory() as db:
            scalar_results = iter((None, 0))
            db.scalar = lambda statement: next(scalar_results)
            yield db

    app.dependency_overrides[get_db] = stale_number_db
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        headers=auth_headers,
        json={"message": "racing request"},
    )

    assert response.status_code == 409
    assert provider.calls == []
    with app.state.test_session_factory() as db:
        assert db.query(Turn).count() == 1


def test_in_flight_turn_returns_409_before_second_provider_generation(tmp_path, auth_headers):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    application = create_app(
        Settings(
            database_url=str(engine.url),
            tester_access_code="tester-secret",
            admin_access_code="admin-secret",
            cookie_secure=False,
        )
    )

    def override_db():
        with session_factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_db
    application.state.test_session_factory = session_factory

    class InFlightProvider:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.first_call_started = threading.Event()
            self.release_first_call = threading.Event()

        def generate(self, messages, deployment):
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_call_started.set()
                assert self.release_first_call.wait(timeout=5)
            return GenerationResult(text=f"reply:{messages[-1].content}")

    provider = InFlightProvider()
    application.state.provider_factory = lambda deployment: provider

    with TestClient(application) as concurrent_client:
        session_id = create_session(concurrent_client, application, auth_headers)
        url = f"/api/sessions/{session_id}/messages"
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                concurrent_client.post,
                url,
                headers=auth_headers,
                json={"message": "first"},
            )
            assert provider.first_call_started.wait(timeout=2)
            try:
                second = executor.submit(
                    concurrent_client.post,
                    url,
                    headers=auth_headers,
                    json={"message": "second"},
                ).result(timeout=2)
            finally:
                provider.release_first_call.set()
            first = first_future.result(timeout=2)

    assert first.status_code == 200
    assert second.status_code == 409
    assert provider.calls == 1


def test_failed_turn_does_not_block_later_send(client, app, auth_headers):
    provider = RecordingProvider(fail=True)
    app.state.provider_factory = lambda deployment: provider
    session_id = create_session(client, app, auth_headers)

    failed = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "fail"}
    )
    provider.fail = False
    completed = client.post(
        f"/api/sessions/{session_id}/messages", headers=auth_headers, json={"message": "recover"}
    )

    assert failed.status_code == 502
    assert completed.status_code == 200
    assert len(provider.calls) == 2


def test_stale_interrupted_turn_is_recovered_before_next_send(client, app, auth_headers):
    provider = RecordingProvider()
    app.state.provider_factory = lambda deployment: provider
    session_id = create_session(client, app, auth_headers)
    with app.state.test_session_factory() as db:
        session = db.get(Session, uuid.UUID(session_id))
        stale = Turn(
            session_id=session.id,
            turn_number=1,
            user_message="interrupted",
            request_started_at=utcnow() - timedelta(hours=1),
            model_deployment_id=session.model_deployment_id,
            status=TurnStatus.pending,
        )
        db.add(stale)
        db.commit()
        stale_id = stale.id

    response = client.post(
        f"/api/sessions/{session_id}/messages",
        headers=auth_headers,
        json={"message": "continue"},
    )

    assert response.status_code == 200
    with app.state.test_session_factory() as db:
        recovered = db.get(Turn, stale_id)
        assert recovered.status == TurnStatus.failed
        assert recovered.error_type == "interrupted_request"


def test_unknown_session_does_not_create_turn(client, app, auth_headers):
    response = client.post(
        f"/api/sessions/{uuid.uuid4()}/messages",
        headers=auth_headers,
        json={"message": "hello"},
    )
    assert response.status_code == 404

import uuid

from app.models import ModelDeployment, Session
from app.providers.base import GenerationResult


def add_deployment(app, version: str, active: bool = True):
    factory = app.state.test_session_factory
    with factory() as db:
        if active:
            db.query(ModelDeployment).update({"active": False})
        deployment = ModelDeployment(
            provider="mock",
            model_id="mock-chat",
            model_version=version,
            endpoint_reference="mock://local",
            configuration_json={},
            active=active,
        )
        db.add(deployment)
        db.commit()
        return deployment.id


def test_session_creation_assigns_active_deployment(client, app, auth_headers):
    deployment_id = add_deployment(app, "v1")

    response = client.post(
        "/api/sessions",
        headers=auth_headers,
        json={"anonymous_tester_id": "browser-id", "client_metadata": {"locale": "en"}},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["model_deployment_id"] == deployment_id
    with app.state.test_session_factory() as db:
        saved = db.get(Session, uuid.UUID(body["id"]))
        assert saved.anonymous_tester_id == "browser-id"
        assert saved.client_metadata == {"locale": "en"}


def test_existing_session_remains_pinned_when_active_model_changes(client, app, auth_headers):
    first_id = add_deployment(app, "v1")
    first = client.post("/api/sessions", headers=auth_headers, json={}).json()
    second_id = add_deployment(app, "v2")
    second = client.post("/api/sessions", headers=auth_headers, json={}).json()

    assert first["model_deployment_id"] == first_id
    assert second["model_deployment_id"] == second_id
    assert (
        client.get(f"/api/sessions/{first['id']}", headers=auth_headers).json()[
            "model_deployment_id"
        ]
        == first_id
    )


def test_existing_session_sends_later_turns_to_pinned_deployment(client, app, auth_headers):
    called_versions = []

    class VersionProvider:
        def generate(self, messages, deployment):
            called_versions.append(deployment.model_version)
            return GenerationResult(text=f"reply from {deployment.model_version}")

    app.state.provider_factory = lambda deployment: VersionProvider()
    add_deployment(app, "v1")
    original = client.post("/api/sessions", headers=auth_headers, json={}).json()
    first = client.post(
        f"/api/sessions/{original['id']}/messages",
        headers=auth_headers,
        json={"message": "before activation"},
    )
    add_deployment(app, "v2")
    second = client.post(
        f"/api/sessions/{original['id']}/messages",
        headers=auth_headers,
        json={"message": "after activation"},
    )

    assert first.json()["assistant_response"] == "reply from v1"
    assert second.json()["assistant_response"] == "reply from v1"
    assert called_versions == ["v1", "v1"]


def test_new_chat_creates_distinct_session(client, app, auth_headers):
    add_deployment(app, "v1")
    first = client.post("/api/sessions", headers=auth_headers, json={}).json()
    second = client.post("/api/sessions", headers=auth_headers, json={}).json()

    assert first["id"] != second["id"]

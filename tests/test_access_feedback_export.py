import csv
import io
import json
import uuid

from app.export import as_csv, as_jsonl
from app.models import Feedback
from app.providers.base import GenerationResult
from tests.test_sessions import add_deployment


class Provider:
    def generate(self, messages, deployment):
        return GenerationResult(text="answer")


def completed_turn(client, app, auth_headers):
    add_deployment(app, "v1")
    app.state.provider_factory = lambda deployment: Provider()
    session_id = client.post("/api/sessions", headers=auth_headers, json={}).json()["id"]
    return client.post(
        f"/api/sessions/{session_id}/messages",
        headers=auth_headers,
        json={"message": "question"},
    ).json()


def test_feedback_is_optional_and_persisted(client, app, auth_headers):
    turn = completed_turn(client, app, auth_headers)

    response = client.post(
        f"/api/turns/{turn['id']}/feedback",
        headers=auth_headers,
        json={"rating": -1, "failure_category": "hallucination", "comment": "invented fact"},
    )

    assert response.status_code == 201
    with app.state.test_session_factory() as db:
        feedback = db.query(Feedback).one()
        assert feedback.turn_id == uuid.UUID(turn["id"])
        assert feedback.rating == -1
        assert feedback.failure_category == "hallucination"
        assert feedback.comment == "invented fact"


def test_feedback_rejects_unknown_turn(client, auth_headers):
    response = client.post(
        f"/api/turns/{uuid.uuid4()}/feedback", headers=auth_headers, json={"rating": 1}
    )
    assert response.status_code == 404


def test_tester_and_admin_routes_are_separately_protected(client, app, auth_headers):
    add_deployment(app, "v1")
    assert client.post("/api/sessions", json={}).status_code == 401
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", headers=auth_headers).status_code == 401
    assert client.get("/admin", headers={"X-Admin-Code": "admin-secret"}).status_code == 200


def test_access_code_exchange_uses_httponly_cookie(client, app):
    add_deployment(app, "v1")
    response = client.post("/access", data={"access_code": "tester-secret"}, follow_redirects=False)
    assert response.status_code == 303
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.post("/api/sessions", json={}).status_code == 201


def test_frontend_never_contains_provider_or_access_secrets(client, app):
    app.state.settings.model_api_key = "provider-super-secret"
    response = client.get("/")
    assert response.status_code == 200
    assert "provider-super-secret" not in response.text
    assert "tester-secret" not in response.text
    assert "admin-secret" not in response.text


def test_admin_jsonl_and_csv_exports_include_model_and_feedback(
    client, app, auth_headers, admin_headers
):
    turn = completed_turn(client, app, auth_headers)
    client.post(
        f"/api/turns/{turn['id']}/feedback",
        headers=auth_headers,
        json={"rating": 1, "comment": "good"},
    )

    jsonl = client.get("/admin/export?format=jsonl", headers=admin_headers)
    assert jsonl.status_code == 200
    row = json.loads(jsonl.text.strip())
    assert row["model"]["version"] == "v1"
    assert row["turn"]["assistant_response"] == "answer"
    assert row["feedback"][0]["rating"] == 1

    csv_response = client.get("/admin/export?format=csv", headers=admin_headers)
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert rows[0]["model_version"] == "v1"
    assert rows[0]["assistant_response"] == "answer"
    assert rows[0]["feedback_ratings"] == "1"


def test_csv_neutralizes_formula_fields_but_jsonl_preserves_raw_values():
    dangerous = {
        "tester": '\t=WEBSERVICE("https://example.test/id")',
        "prompt": " =SUM(1,1)",
        "response": "+cmd|' /C calc'!A0",
        "error": "\r-2+3",
        "category": '\n@IMPORTXML("https://example.test")',
        "comment": '=HYPERLINK("https://example.test")',
    }
    record = {
        "session": {
            "id": "session-1",
            "anonymous_tester_id": dangerous["tester"],
            "created_at": None,
            "client_metadata": None,
        },
        "model": {
            "deployment_id": 1,
            "provider": "mock",
            "model_id": "model",
            "version": "v1",
            "configuration": {},
        },
        "turn": {
            "id": "turn-1",
            "turn_number": 1,
            "user_message": dangerous["prompt"],
            "assistant_response": dangerous["response"],
            "status": "failed",
            "error_type": "provider_failure",
            "error_message": dangerous["error"],
            "request_started_at": None,
            "inference_started_at": None,
            "first_token_at": None,
            "response_completed_at": None,
            "input_tokens": None,
            "output_tokens": None,
            "time_to_first_token_ms": None,
            "inference_latency_ms": None,
            "total_latency_ms": None,
            "tokens_per_second": None,
            "provider_request_id": None,
            "provider_metadata": None,
        },
        "feedback": [
            {
                "id": "feedback-1",
                "rating": -1,
                "failure_category": dangerous["category"],
                "comment": dangerous["comment"],
                "created_at": None,
            }
        ],
    }

    csv_row = next(csv.DictReader(io.StringIO(as_csv([record]))))
    for field in (
        "tester_id",
        "user_message",
        "assistant_response",
        "error_message",
        "feedback_categories",
        "feedback_comments",
    ):
        assert csv_row[field].startswith("'")
    assert json.loads(as_jsonl([record]))["turn"]["user_message"] == dangerous["prompt"]
    assert json.loads(as_jsonl([record]))["session"]["anonymous_tester_id"] == dangerous["tester"]


def test_admin_can_activate_deployment_for_only_new_sessions(
    client, app, auth_headers, admin_headers
):
    first_id = add_deployment(app, "v1")
    old_session = client.post("/api/sessions", headers=auth_headers, json={}).json()
    response = client.post(
        "/admin/deployments",
        headers=admin_headers,
        json={
            "provider": "mock",
            "model_id": "next",
            "model_version": "v2",
            "endpoint_reference": "mock://local",
            "configuration_json": {"temperature": 0.1},
            "activate": True,
        },
    )
    assert response.status_code == 201
    new_session = client.post("/api/sessions", headers=auth_headers, json={}).json()
    assert old_session["model_deployment_id"] == first_id
    assert new_session["model_deployment_id"] == response.json()["id"]

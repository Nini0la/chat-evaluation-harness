import json

from app.models import EvalCandidate, ShadowResponse, TurnStatus
from app.providers.base import GenerationResult, ProviderError
from tests.test_sessions import add_deployment


class VersionedProvider:
    def __init__(self, failing_versions=()):
        self.calls = []
        self.failing_versions = set(failing_versions)

    def generate(self, messages, deployment):
        history = [message.model_dump() for message in messages]
        self.calls.append((deployment.model_version, history))
        if deployment.model_version in self.failing_versions:
            raise ProviderError("network_failure", f"{deployment.model_version} unavailable")
        return GenerationResult(
            text=f"{deployment.model_version}:{messages[-1].content}",
            input_tokens=len(messages),
            output_tokens=1,
            provider_request_id=f"request-{deployment.model_version}",
            provider_metadata={"version": deployment.model_version},
        )


def create_shadow_session(client, app, auth_headers):
    red = add_deployment(app, "red-v", active=False)
    green = add_deployment(app, "green-v", active=False)
    blue = add_deployment(app, "blue-v", active=False)
    primary = add_deployment(app, "primary-v")
    response = client.post(
        "/api/sessions",
        headers=auth_headers,
        json={
            "shadows": [
                {"slot": "red", "model_deployment_id": red},
                {"slot": "green", "model_deployment_id": green},
                {"slot": "blue", "model_deployment_id": blue},
            ]
        },
    )
    assert response.status_code == 201
    assert response.json()["model_deployment_id"] == primary
    return response.json()


def test_single_model_fallback_has_no_shadow_calls_or_responses(client, app, auth_headers):
    provider = VersionedProvider()
    app.state.provider_factory = lambda deployment: provider
    add_deployment(app, "primary-v")
    session = client.post("/api/sessions", headers=auth_headers, json={}).json()

    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        headers=auth_headers,
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert response.json()["shadow_responses"] == []
    assert [version for version, _ in provider.calls] == ["primary-v"]


def test_three_shadow_responses_are_persisted_and_exposed(client, app, auth_headers):
    provider = VersionedProvider()
    app.state.provider_factory = lambda deployment: provider
    session = create_shadow_session(client, app, auth_headers)

    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        headers=auth_headers,
        json={"message": "compare"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_response"] == "primary-v:compare"
    assert {
        item["slot"]: (item["assistant_response"], item["status"])
        for item in body["shadow_responses"]
    } == {
        "red": ("red-v:compare", "completed"),
        "green": ("green-v:compare", "completed"),
        "blue": ("blue-v:compare", "completed"),
    }
    listed = client.get(f"/api/sessions/{session['id']}/turns", headers=auth_headers).json()
    assert listed[0]["shadow_responses"] == body["shadow_responses"]
    with app.state.test_session_factory() as db:
        assert db.query(ShadowResponse).count() == 3


def test_each_shadow_history_contains_only_its_own_responses(client, app, auth_headers):
    provider = VersionedProvider()
    app.state.provider_factory = lambda deployment: provider
    session = create_shadow_session(client, app, auth_headers)
    url = f"/api/sessions/{session['id']}/messages"
    client.post(url, headers=auth_headers, json={"message": "one"})

    second = client.post(url, headers=auth_headers, json={"message": "two"})

    assert second.status_code == 200
    second_calls = provider.calls[4:]
    for version, history in second_calls:
        assert history == [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": f"{version}:one"},
            {"role": "user", "content": "two"},
        ]


def test_shadow_failure_does_not_erase_other_results_or_retry(client, app, auth_headers):
    provider = VersionedProvider(failing_versions={"green-v"})
    app.state.provider_factory = lambda deployment: provider
    session = create_shadow_session(client, app, auth_headers)

    response = client.post(
        f"/api/sessions/{session['id']}/messages",
        headers=auth_headers,
        json={"message": "partial"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["assistant_response"] == "primary-v:partial"
    shadows = {item["slot"]: item for item in body["shadow_responses"]}
    assert shadows["red"]["assistant_response"] == "red-v:partial"
    assert shadows["blue"]["assistant_response"] == "blue-v:partial"
    assert shadows["green"]["status"] == "failed"
    assert shadows["green"]["error_type"] == "network_failure"
    assert shadows["green"]["assistant_response"] is None
    assert [version for version, _ in provider.calls].count("green-v") == 1
    with app.state.test_session_factory() as db:
        failed = db.query(ShadowResponse).filter(ShadowResponse.status == TurnStatus.failed).one()
        assert failed.error_message == "green-v unavailable"


def test_session_rejects_primary_or_duplicate_shadow_deployments(client, app, auth_headers):
    shadow = add_deployment(app, "shadow-v", active=False)
    primary = add_deployment(app, "primary-v")

    same_as_primary = client.post(
        "/api/sessions",
        headers=auth_headers,
        json={"shadows": [{"slot": "red", "model_deployment_id": primary}]},
    )
    duplicate = client.post(
        "/api/sessions",
        headers=auth_headers,
        json={
            "shadows": [
                {"slot": "red", "model_deployment_id": shadow},
                {"slot": "green", "model_deployment_id": shadow},
            ]
        },
    )

    assert same_as_primary.status_code == 422
    assert duplicate.status_code == 422


def test_primary_and_shadow_responses_can_be_exported_as_review_candidates(
    client, app, auth_headers, admin_headers
):
    provider = VersionedProvider()
    app.state.provider_factory = lambda deployment: provider
    session = create_shadow_session(client, app, auth_headers)
    turn = client.post(
        f"/api/sessions/{session['id']}/messages",
        headers=auth_headers,
        json={"message": "interesting"},
    ).json()

    for source in ("primary", "red"):
        marked = client.post(
            f"/api/turns/{turn['id']}/eval-candidates",
            headers=auth_headers,
            json={"source": source, "note": "review later"},
        )
        assert marked.status_code == 201

    interactions = client.get("/admin/export?format=jsonl", headers=admin_headers)
    interaction = json.loads(interactions.text)
    assert {response["slot"] for response in interaction["shadow_responses"]} == {
        "red",
        "green",
        "blue",
    }

    exported = client.get("/admin/eval-candidates", headers=admin_headers)
    records = [json.loads(line) for line in exported.text.splitlines()]

    assert exported.status_code == 200
    assert {record["source"] for record in records} == {"primary", "red"}
    assert all(record["schema_version"] == "eval_candidate_v1" for record in records)
    assert all("expected_behavior" not in record for record in records)
    with app.state.test_session_factory() as db:
        assert db.query(EvalCandidate).count() == 2

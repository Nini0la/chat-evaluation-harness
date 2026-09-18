import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def test_openapi_schema_does_not_embed_secret_values():
    app = create_app(
        Settings(
            tester_access_code="TESTER_VALUE_NEVER_PUBLIC",
            admin_access_code="ADMIN_VALUE_NEVER_PUBLIC",
            model_api_key="MODEL_VALUE_NEVER_PUBLIC",
        )
    )
    schema = str(app.openapi())
    assert "TESTER_VALUE_NEVER_PUBLIC" not in schema
    assert "ADMIN_VALUE_NEVER_PUBLIC" not in schema
    assert "MODEL_VALUE_NEVER_PUBLIC" not in schema


def test_frontend_assets_do_not_embed_server_secrets(client):
    static_directory = Path(__file__).parents[1] / "app/static"
    public_assets = ["/", *(f"/static/{path.name}" for path in static_directory.iterdir())]
    secrets = ["tester-secret", "admin-secret", "model-secret"]

    for asset in public_assets:
        response = client.get(asset)
        assert response.status_code == 200
        for secret in secrets:
            assert secret not in response.text


@pytest.mark.parametrize(
    ("tester_code", "admin_code"),
    [
        ("change-me", "production-admin"),
        ("production-tester", "change-admin-me"),
        ("replace-with-a-long-random-code", "production-admin"),
        ("production-tester", "replace-with-a-different-long-random-code"),
        ("", "production-admin"),
        ("   ", "production-admin"),
        ("production-tester", ""),
        ("same-code", "same-code"),
        ("a", "b" * 16),
        ("a" * 16, "b"),
        ("a" + " " * 15, "b" * 16),
        (" change-me " + " " * 8, "b" * 16),
    ],
)
def test_non_development_rejects_insecure_access_codes(tester_code, admin_code):
    with pytest.raises(ValidationError, match="access codes"):
        Settings(
            environment="production",
            tester_access_code=tester_code,
            admin_access_code=admin_code,
        )


def test_non_development_accepts_distinct_sixteen_character_access_codes():
    settings = Settings(
        environment="production",
        tester_access_code="t" * 16,
        admin_access_code="a" * 16,
    )

    assert settings.tester_access_code.get_secret_value() == "t" * 16
    assert settings.admin_access_code.get_secret_value() == "a" * 16


def test_cloud_run_explicitly_enables_production_configuration_validation():
    terraform = (Path(__file__).parents[1] / "infra/main.tf").read_text()

    assert re.search(
        r'env\s*\{\s*name\s*=\s*"ENVIRONMENT"\s*value\s*=\s*"production"\s*\}',
        terraform,
    )


def test_development_allows_default_access_codes():
    settings = Settings(
        environment="development",
        tester_access_code="change-me",
        admin_access_code="change-admin-me",
    )
    assert settings.tester_access_code.get_secret_value() == "change-me"
    assert settings.admin_access_code.get_secret_value() == "change-admin-me"

from functools import lru_cache

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Chat Evaluation Harness"
    environment: str = "development"
    database_url: str = "sqlite:///./chat_eval.db"
    tester_access_code: SecretStr = SecretStr("change-me")
    admin_access_code: SecretStr = SecretStr("change-admin-me")
    cookie_secure: bool = True
    model_provider: str = "mock"
    model_endpoint: str | None = None
    model_api_key: SecretStr | None = None
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str = "2024-10-21"
    model_id: str = "mock-chat"
    model_version: str = "development"
    model_timeout_seconds: float = 60.0

    @model_validator(mode="after")
    def validate_access_codes(self) -> "Settings":
        if self.environment.casefold() == "development":
            return self
        tester = self.tester_access_code.get_secret_value()
        admin = self.admin_access_code.get_secret_value()
        normalized_tester = tester.strip()
        normalized_admin = admin.strip()
        insecure_defaults = {
            "change-me",
            "change-admin-me",
            "replace-with-a-long-random-code",
            "replace-with-a-different-long-random-code",
        }
        if (
            tester != normalized_tester
            or admin != normalized_admin
            or len(normalized_tester) < 16
            or len(normalized_admin) < 16
            or normalized_tester == normalized_admin
            or normalized_tester in insecure_defaults
            or normalized_admin in insecure_defaults
        ):
            raise ValueError(
                "non-development access codes must be at least 16 characters, non-empty, "
                "distinct, and not defaults"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

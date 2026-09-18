from app.config import Settings
from app.models import ModelDeployment
from app.providers.base import ModelProvider, ProviderError
from app.providers.http import HttpModelProvider
from app.providers.mock import MockModelProvider


def build_provider(deployment: ModelDeployment, settings: Settings) -> ModelProvider:
    if deployment.provider == "mock":
        return MockModelProvider()
    if deployment.provider in {"http", "modal"}:
        return HttpModelProvider(settings)
    raise ProviderError("configuration_error", f"Unsupported provider: {deployment.provider}")

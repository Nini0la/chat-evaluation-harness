import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.models import ModelDeployment

SCHEMA_VERSION = "1"
SCORING_VERSION = "1"
RUNNER_VERSION = "1"
MANIFEST_VERSION = "1"

_SECRET_FRAGMENTS = (
    "secret",
    "password",
    "token",
    "key",
    "auth",
    "bearer",
    "cookie",
    "credential",
    "signature",
)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(child)
            for key, child in value.items()
            if not any(fragment in str(key).casefold() for fragment in _SECRET_FRAGMENTS)
        }
    if isinstance(value, list | tuple):
        return [_without_secrets(child) for child in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def deployment_snapshot(deployment: ModelDeployment) -> dict[str, Any]:
    return {
        "id": deployment.id,
        "provider": deployment.provider,
        "model_id": deployment.model_id,
        "model_version": deployment.model_version,
        "endpoint_reference": deployment.endpoint_reference,
        "configuration": _without_secrets(deployment.configuration_json or {}),
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def manifest_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(manifest)
    identity.pop("suite_reference", None)
    return identity


def build_manifest(
    *,
    suite_sha256: str,
    deployments: Sequence[ModelDeployment],
    generation_config: Mapping[str, Any],
    judge: ModelDeployment,
    judge_config: Mapping[str, Any],
    execution_config: Mapping[str, Any] | None = None,
    suite_reference: str | None = None,
    suite_version: str | None = None,
) -> dict[str, Any]:
    ordered_deployments = sorted(deployments, key=lambda item: item.id)
    return {
        "manifest_version": MANIFEST_VERSION,
        "suite_sha256": suite_sha256,
        "suite_version": suite_version,
        "suite_reference": suite_reference,
        "models": [deployment_snapshot(item) for item in ordered_deployments],
        "generation_config": _without_secrets(generation_config),
        "effective_model_configurations": [
            {
                "deployment_id": item.id,
                "configuration": _without_secrets(generation_config),
            }
            for item in ordered_deployments
        ],
        "judge": deployment_snapshot(judge),
        "judge_config": _without_secrets(judge_config),
        "execution_config": _without_secrets(execution_config or {}),
        "schema_version": SCHEMA_VERSION,
        "scoring_version": SCORING_VERSION,
        "runner_version": RUNNER_VERSION,
    }


def stable_run_id(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest_identity(manifest)).encode("ascii")).hexdigest()

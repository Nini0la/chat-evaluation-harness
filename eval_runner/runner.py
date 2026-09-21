import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.models import ModelDeployment
from app.providers.base import GenerationResult, ModelMessage, ModelProvider, ProviderError
from app.providers.factory import build_provider
from eval_runner.checks import deterministic_checks
from eval_runner.files import append_jsonl_fsync, load_jsonl_by_key, prepare_run_directory, run_lock
from eval_runner.judge import JudgeOutputError, run_judge
from eval_runner.manifest import build_manifest, deployment_snapshot, stable_run_id
from eval_runner.reporting import write_reports
from eval_runner.schema import EvalItem
from eval_runner.scoring import combined_score

ProviderBuilder = Callable[[ModelDeployment, Settings], ModelProvider]


@dataclass(frozen=True)
class EvalRunConfig:
    output_root: Path = Path("eval_runs")
    generation_config: Mapping[str, Any] = field(default_factory=dict)
    judge_config: Mapping[str, Any] = field(default_factory=lambda: {"temperature": 0})
    concurrency: int = 4
    retries: int = 2
    retry_backoff_seconds: float = 1.0
    timeout_seconds: float = 60.0
    resume: bool = False
    suite_reference: str | None = None

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.retries < 0 or self.retry_backoff_seconds < 0 or self.timeout_seconds <= 0:
            raise ValueError("invalid retry or timeout settings")


def _configured_deployment(
    deployment: ModelDeployment, configuration: Mapping[str, Any]
) -> ModelDeployment:
    # A fresh value avoids sharing SQLAlchemy session state across worker threads.
    return ModelDeployment(
        id=deployment.id,
        provider=deployment.provider,
        model_id=deployment.model_id,
        model_version=deployment.model_version,
        endpoint_reference=deployment.endpoint_reference,
        configuration_json=dict(configuration),
        active=deployment.active,
    )


def _attempt(
    operation: Callable[[], GenerationResult], retries: int, backoff: float
) -> tuple[GenerationResult | None, dict[str, str] | None, int]:
    for attempt in range(retries + 1):
        try:
            return operation(), None, attempt + 1
        except ProviderError as exc:
            error = {"type": exc.error_type, "message": str(exc)}
            if not exc.retryable:
                return None, error, attempt + 1
        except JudgeOutputError as exc:
            error = {"type": "invalid_judge_output", "message": str(exc)}
        except Exception as exc:
            error = {"type": "application_exception", "message": str(exc)}
            return None, error, attempt + 1
        if attempt < retries:
            time.sleep(backoff * (2**attempt))
    return None, error, retries + 1


def _generate_one(
    item: EvalItem,
    deployment: ModelDeployment,
    config: EvalRunConfig,
    settings: Settings,
    provider_builder: ProviderBuilder,
) -> dict[str, Any]:
    configured = _configured_deployment(deployment, config.generation_config)
    provider: ModelProvider | None = None
    started = time.perf_counter()

    def operation() -> GenerationResult:
        nonlocal provider
        if provider is None:
            provider = provider_builder(configured, settings)
        return provider.generate([ModelMessage(role="user", content=item.prompt)], configured)

    result, error, attempts = _attempt(
        operation,
        config.retries,
        config.retry_backoff_seconds,
    )
    if provider is not None:
        close = getattr(provider, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    return {
        "item_id": item.id,
        "model_deployment_id": deployment.id,
        "model": deployment_snapshot(deployment),
        "candidate": result.text if result else None,
        "error": error,
        "attempts": attempts,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "usage": {
            "input_tokens": result.input_tokens if result else None,
            "output_tokens": result.output_tokens if result else None,
        },
        "provider_request_id": result.provider_request_id if result else None,
        "provider_metadata": result.provider_metadata if result else None,
    }


def _judge_one(
    item: EvalItem,
    response: dict[str, Any],
    judge: ModelDeployment,
    config: EvalRunConfig,
    settings: Settings,
    provider_builder: ProviderBuilder,
) -> dict[str, Any]:
    deterministic = deterministic_checks(
        item,
        response["candidate"],
        error=response["error"],
        provider_metadata=response["provider_metadata"],
    )
    raw_judge = None
    judged = None
    judge_error = None
    judge_attempts = 0
    if response["error"] is None and response["candidate"] is not None:
        configured_judge = _configured_deployment(judge, config.judge_config)
        provider: ModelProvider | None = None

        def operation() -> GenerationResult:
            nonlocal provider, raw_judge, judged
            if provider is None:
                provider = provider_builder(configured_judge, settings)
            try:
                raw_judge, judged = run_judge(
                    provider, configured_judge, item, response["candidate"]
                )
            except JudgeOutputError as exc:
                raw_judge = exc.raw
                raise
            return GenerationResult(text=raw_judge)

        _, judge_error, judge_attempts = _attempt(
            operation, config.retries, config.retry_backoff_seconds
        )
        if provider is not None:
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
    judge_score = judged.score if judged else None
    if response["error"] is not None or not (response["candidate"] or "").strip():
        judge_score = 0
    score = combined_score(judge_score, deterministic.cap)
    return {
        "item_id": item.id,
        "model_deployment_id": response["model_deployment_id"],
        "model": response["model"],
        "item": item.model_dump(mode="json"),
        "candidate": response["candidate"],
        "generation_error": response["error"],
        "deterministic": deterministic.as_dict(),
        "judge": judged.model_dump(mode="json") if judged else None,
        "judge_raw": raw_judge,
        "judge_error": judge_error,
        "judge_attempts": judge_attempts,
        "scoring": score,
    }


def run_evaluation(
    *,
    items: Sequence[EvalItem],
    suite_sha256: str,
    deployments: Sequence[ModelDeployment],
    judge: ModelDeployment,
    config: EvalRunConfig,
    settings: Settings | None = None,
    provider_builder: ProviderBuilder = build_provider,
) -> Path:
    item_ids = [item.id for item in items]
    if not item_ids:
        raise ValueError("evaluation requires at least one item")
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("duplicate eval item ids")
    suite_versions = {item.suite_version for item in items}
    if len(suite_versions) != 1:
        raise ValueError("evaluation requires one consistent suite_version")
    deployment_ids = [deployment.id for deployment in deployments]
    if not deployment_ids:
        raise ValueError("evaluation requires at least one model deployment")
    if None in deployment_ids or len(deployment_ids) != len(set(deployment_ids)):
        raise ValueError("model deployments must have unique persisted ids")
    manifest = build_manifest(
        suite_sha256=suite_sha256,
        deployments=deployments,
        generation_config=config.generation_config,
        judge=judge,
        judge_config=config.judge_config,
        execution_config={
            "concurrency": config.concurrency,
            "retries": config.retries,
            "retry_backoff_seconds": config.retry_backoff_seconds,
            "timeout_seconds": config.timeout_seconds,
        },
        suite_reference=config.suite_reference,
        suite_version=next(iter(suite_versions)),
    )
    run_id = stable_run_id(manifest)
    run_dir = prepare_run_directory(config.output_root, run_id, manifest, config.resume)
    with run_lock(run_dir):
        return _run_locked(
            items=items,
            deployments=deployments,
            judge=judge,
            config=config,
            run_dir=run_dir,
            settings=settings,
            provider_builder=provider_builder,
        )


def _run_locked(
    *,
    items: Sequence[EvalItem],
    deployments: Sequence[ModelDeployment],
    judge: ModelDeployment,
    config: EvalRunConfig,
    run_dir: Path,
    settings: Settings | None,
    provider_builder: ProviderBuilder,
) -> Path:
    responses_path = run_dir / "responses.jsonl"
    judged_path = run_dir / "judged.jsonl"
    key_fields = ("model_deployment_id", "item_id")
    responses = load_jsonl_by_key(responses_path, key_fields)
    settings = (settings or get_settings()).model_copy(
        update={"model_timeout_seconds": config.timeout_seconds}
    )

    pending = [
        (item, deployment)
        for deployment in deployments
        for item in items
        if (deployment.id, item.id) not in responses
    ]
    with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
        futures = {
            executor.submit(_generate_one, item, deployment, config, settings, provider_builder): (
                item,
                deployment,
            )
            for item, deployment in pending
        }
        for future in as_completed(futures):
            record = future.result()
            append_jsonl_fsync(responses_path, record)
            responses[(record["model_deployment_id"], record["item_id"])] = record

    # Judging starts only after all newly generated raw responses are durable.
    judged_records = load_jsonl_by_key(judged_path, key_fields)
    pending_judgments = [
        (item, deployment)
        for deployment in deployments
        for item in items
        if (deployment.id, item.id) not in judged_records
    ]
    with ThreadPoolExecutor(max_workers=config.concurrency) as executor:
        futures = {
            executor.submit(
                _judge_one,
                item,
                responses[(deployment.id, item.id)],
                judge,
                config,
                settings,
                provider_builder,
            ): (item, deployment)
            for item, deployment in pending_judgments
        }
        for future in as_completed(futures):
            record = future.result()
            append_jsonl_fsync(judged_path, record)
            judged_records[(record["model_deployment_id"], record["item_id"])] = record

    ordered = [
        judged_records[(deployment.id, item.id)] for deployment in deployments for item in items
    ]
    write_reports(run_dir, ordered)
    return run_dir

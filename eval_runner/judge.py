import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import ModelDeployment
from app.providers.base import ModelMessage, ModelProvider
from eval_runner.schema import EvalItem

JUDGE_SYSTEM_PROMPT = """You are a strict, domain-neutral response evaluator.
Evaluate only the supplied rubric data. Return one JSON object with exactly these fields:
score (integer 0-3), required_points_met (array of strings),
required_points_missed (array of strings), forbidden_points_present (array of strings),
reason (string), judge_confidence (one of high, medium, low).
Do not add domain facts, requirements, or prohibitions that are absent from the supplied rubric.
Do not include markdown or additional text."""


class JudgeOutputError(ValueError):
    def __init__(self, raw: str, message: str):
        self.raw = raw
        super().__init__(message)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    score: int = Field(ge=0, le=3)
    required_points_met: list[str]
    required_points_missed: list[str]
    forbidden_points_present: list[str]
    reason: str = Field(min_length=1)
    judge_confidence: Literal["high", "medium", "low"]


def judge_payload(item: EvalItem, candidate: str) -> dict[str, Any]:
    return {
        "prompt": item.prompt,
        "candidate": candidate,
        "expected_behavior": item.expected_behavior,
        "required_points": item.required_points,
        "forbidden_points": item.forbidden_points,
    }


def parse_judge_output(raw: str) -> JudgeResult:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"judge returned invalid JSON: {exc.msg}") from exc
    try:
        return JudgeResult.model_validate(value)
    except Exception as exc:
        raise ValueError(f"judge returned an invalid result: {exc}") from exc


def validate_judge_rubric(result: JudgeResult, item: EvalItem) -> None:
    met = result.required_points_met
    missed = result.required_points_missed
    present = result.forbidden_points_present
    if len(met) != len(set(met)) or len(missed) != len(set(missed)):
        raise ValueError("judge returned duplicate required points")
    if len(present) != len(set(present)):
        raise ValueError("judge returned duplicate forbidden points")
    if set(met) & set(missed):
        raise ValueError("judge marked a required point both met and missed")
    if set(met) | set(missed) != set(item.required_points):
        raise ValueError("judge required-point lists do not match the supplied rubric")
    if not set(present) <= set(item.forbidden_points):
        raise ValueError("judge added forbidden points outside the supplied rubric")


def run_judge(
    provider: ModelProvider,
    deployment: ModelDeployment,
    item: EvalItem,
    candidate: str,
) -> tuple[str, JudgeResult]:
    request = json.dumps(judge_payload(item, candidate), sort_keys=True, ensure_ascii=True)
    generated = provider.generate(
        [
            ModelMessage(role="system", content=JUDGE_SYSTEM_PROMPT),
            ModelMessage(role="user", content=request),
        ],
        deployment,
    )
    try:
        parsed = parse_judge_output(generated.text)
        validate_judge_rubric(parsed, item)
    except ValueError as exc:
        raise JudgeOutputError(generated.text, str(exc)) from exc
    return generated.text, parsed

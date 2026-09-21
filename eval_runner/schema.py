import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvalItem(BaseModel):
    """One domain-neutral, single-turn evaluation case."""

    model_config = ConfigDict(extra="allow", strict=True)

    id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)
    category: str = Field(min_length=1)
    subcategory: str = Field(min_length=1)
    severity: Literal["low", "medium", "high", "critical"]
    required_points: list[str]
    forbidden_points: list[str]
    source_of_truth: str = Field(min_length=1)
    leakage_group: str = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)
    expected_output_mode: Literal["free_form", "json"] | None = "free_form"
    required_literals: list[str] = Field(default_factory=list)
    forbidden_literals: list[str] = Field(default_factory=list)
    refusal_expectation: Literal["required", "forbidden", "allowed"] | None = None
    min_chars: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=0)
    metric_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_constraints(self) -> "EvalItem":
        if self.max_chars is not None and self.min_chars is not None:
            if self.min_chars > self.max_chars:
                raise ValueError("min_chars cannot exceed max_chars")
        for field_name in (
            "required_points",
            "forbidden_points",
            "required_literals",
            "forbidden_literals",
            "metric_tags",
        ):
            values = getattr(self, field_name)
            if any(not value for value in values):
                raise ValueError(f"{field_name} cannot contain empty strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} cannot contain duplicates")
        overlap = set(self.required_literals) & set(self.forbidden_literals)
        if overlap:
            raise ValueError("the same literal cannot be both required and forbidden")
        return self


def load_suite(path: str | Path) -> tuple[list[EvalItem], str]:
    suite_path = Path(path)
    raw = suite_path.read_bytes()
    items: list[EvalItem] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{suite_path}:{line_number}: invalid JSON: {exc.msg}") from exc
        try:
            item = EvalItem.model_validate(value)
        except Exception as exc:
            raise ValueError(f"{suite_path}:{line_number}: invalid eval item: {exc}") from exc
        if item.id in seen:
            raise ValueError(f"{suite_path}:{line_number}: duplicate eval item id {item.id!r}")
        seen.add(item.id)
        items.append(item)
    if not items:
        raise ValueError(f"{suite_path}: suite contains no eval items")
    suite_versions = {item.suite_version for item in items}
    if len(suite_versions) != 1:
        raise ValueError(f"{suite_path}: suite contains mixed suite_version values")
    return items, hashlib.sha256(raw).hexdigest()

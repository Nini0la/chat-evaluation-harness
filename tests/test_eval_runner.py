import json

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import ModelDeployment
from app.providers.base import GenerationResult, ModelProvider
from eval_runner.checks import deterministic_checks
from eval_runner.files import load_jsonl_by_key, run_lock
from eval_runner.judge import JudgeResult, judge_payload, parse_judge_output, validate_judge_rubric
from eval_runner.manifest import build_manifest, stable_run_id
from eval_runner.reporting import build_summary
from eval_runner.runner import EvalRunConfig, run_evaluation
from eval_runner.schema import EvalItem, load_suite


def item(item_id: str = "case-1", **updates) -> EvalItem:
    values = {
        "id": item_id,
        "suite_version": "gate2_freeform_dev_v1",
        "prompt": "Return the requested synthetic value.",
        "expected_behavior": "Return OK without extra claims.",
        "category": "formatting",
        "subcategory": "literal",
        "severity": "high",
        "required_points": ["return the requested value"],
        "forbidden_points": ["add an unsupported value"],
        "source_of_truth": "synthetic fixture specification",
        "leakage_group": "synthetic-literal",
        "metadata": {},
        "required_literals": ["OK"],
        "metric_tags": ["exactness"],
    }
    values.update(updates)
    return EvalItem.model_validate(values)


def deployment(identifier: int, version: str) -> ModelDeployment:
    return ModelDeployment(
        id=identifier,
        provider="mock",
        model_id=f"model-{identifier}",
        model_version=version,
        endpoint_reference=None,
        configuration_json={},
        active=False,
    )


def test_suite_schema_requires_v1_contract_allows_metadata_and_rejects_duplicate_ids(
    tmp_path,
):
    enriched = item(review_batch="batch-7", custom_metadata={"owner": "eval-team"})
    assert enriched.model_extra == {
        "review_batch": "batch-7",
        "custom_metadata": {"owner": "eval-team"},
    }
    missing_version = item().model_dump()
    missing_version.pop("suite_version")
    with pytest.raises(ValidationError):
        EvalItem.model_validate(missing_version)
    with pytest.raises(ValidationError):
        item(min_chars=10, max_chars=2)

    suite = tmp_path / "suite.jsonl"
    line = json.dumps(item().model_dump(mode="json"))
    suite.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate eval item id"):
        load_suite(suite)

    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        "\n".join(
            json.dumps(record.model_dump(mode="json"))
            for record in (item("case-1"), item("case-2", suite_version="v2"))
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="mixed suite_version"):
        load_suite(mixed)


def test_run_id_is_canonical_and_manifest_omits_secrets():
    model = deployment(1, "candidate-v1")
    model.configuration_json = {"temperature": 0.1, "api_key": "do-not-store"}
    judge = deployment(2, "judge-v1")
    first = build_manifest(
        suite_sha256="abc",
        deployments=[model],
        generation_config={"top_p": 0.9, "temperature": 0.2},
        judge=judge,
        judge_config={"temperature": 0},
    )
    second = build_manifest(
        suite_sha256="abc",
        deployments=[model],
        generation_config={"temperature": 0.2, "top_p": 0.9},
        judge=judge,
        judge_config={"temperature": 0},
    )
    moved_suite = build_manifest(
        suite_sha256="abc",
        suite_reference="/different/path/suite.jsonl",
        deployments=[model],
        generation_config={"temperature": 0.2, "top_p": 0.9},
        judge=judge,
        judge_config={"temperature": 0},
    )
    assert stable_run_id(first) == stable_run_id(second)
    assert stable_run_id(first) == stable_run_id(moved_suite)
    assert "do-not-store" not in json.dumps(first)


def test_deterministic_checks_apply_explicit_caps():
    result = deterministic_checks(
        item(expected_output_mode="json", min_chars=5, max_chars=20),
        "OK OK OK OK OK OK",
        provider_metadata={"finish_reason": "length"},
    )
    failed = {check["name"] for check in result.checks if not check["passed"]}
    assert {"output_mode", "repetition", "clean_termination"} <= failed
    assert result.cap == 1


@pytest.mark.parametrize(
    "value",
    [
        {
            "score": 4,
            "required_points_met": [],
            "required_points_missed": [],
            "forbidden_points_present": [],
            "reason": "x",
            "judge_confidence": "high",
        },
        {
            "score": 2,
            "required_points_met": [],
            "required_points_missed": [],
            "forbidden_points_present": [],
            "reason": "x",
            "judge_confidence": "certain",
        },
        {
            "score": 2,
            "required_points_met": [],
            "required_points_missed": [],
            "forbidden_points_present": [],
            "reason": "x",
            "judge_confidence": "medium",
            "extra": True,
        },
    ],
)
def test_judge_validation_rejects_invalid_contract(value):
    with pytest.raises(ValueError, match="invalid result"):
        parse_judge_output(json.dumps(value))


def test_judge_payload_contains_only_allowed_rubric_data():
    enriched = item(
        metric_tags=["over_refusal", "negation"],
        review_batch="batch-7",
    )
    assert set(judge_payload(enriched, "OK")) == {
        "prompt",
        "candidate",
        "expected_behavior",
        "required_points",
        "forbidden_points",
    }


def test_judge_points_must_match_the_supplied_rubric():
    result = JudgeResult(
        score=3,
        required_points_met=["invented criterion"],
        required_points_missed=[],
        forbidden_points_present=[],
        reason="Looks good.",
        judge_confidence="high",
    )
    with pytest.raises(ValueError, match="supplied rubric"):
        validate_judge_rubric(result, item())


def test_jsonl_loader_discards_only_a_torn_final_record(tmp_path):
    path = tmp_path / "responses.jsonl"
    path.write_bytes(b'{"model_deployment_id":1,"item_id":"complete"}\n{"item_id":')

    records = load_jsonl_by_key(path, ("model_deployment_id", "item_id"))

    assert set(records) == {(1, "complete")}
    assert path.read_bytes().endswith(b"\n")


def test_run_lock_rejects_a_concurrent_resume(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already active"):
            with run_lock(tmp_path):
                pass


def test_summary_groups_by_dimensions_and_metric_tags():
    record = {
        "model": {"model_version": "candidate-v1"},
        "item": item().model_dump(mode="json"),
        "scoring": {"score": 3},
    }
    summary = build_summary([record])
    assert summary["overall"]["count"] == 1
    assert summary["overall"]["mean_score"] == 3.0
    assert summary["overall"]["correct_rate"] == 1.0
    assert summary["groups"]["metric_tag"][0]["metric_tag"] == "exactness"


class SyntheticProvider(ModelProvider):
    def __init__(self, generate):
        self._generate = generate

    def generate(self, messages, deployment):
        return self._generate(messages, deployment)


def test_runner_flushes_all_responses_before_judging_and_resumes(tmp_path):
    cases = [item("case-1"), item("case-2")]
    candidates = [
        deployment(1, "candidate-v1"),
        deployment(3, "candidate-v2"),
        deployment(4, "candidate-v3"),
    ]
    judge = deployment(2, "judge-v1")
    output_root = tmp_path / "runs"
    build_calls = []

    def provider_builder(selected, settings):
        build_calls.append((selected.id, settings.model_timeout_seconds))
        if selected.id in {candidate.id for candidate in candidates}:

            def candidate_generate(messages, selected):
                assert [message.model_dump() for message in messages] == [
                    {"role": "user", "content": "Return the requested synthetic value."}
                ]
                return GenerationResult(text="OK", provider_metadata={"finish_reason": "stop"})

            return SyntheticProvider(candidate_generate)

        def judge_generate(messages, selected):
            run_dirs = list(output_root.iterdir())
            response_lines = (run_dirs[0] / "responses.jsonl").read_text().splitlines()
            assert len(response_lines) == len(cases) * len(candidates)
            return GenerationResult(
                text=json.dumps(
                    {
                        "score": 3,
                        "required_points_met": ["return the requested value"],
                        "required_points_missed": [],
                        "forbidden_points_present": [],
                        "reason": "Matches the synthetic rubric.",
                        "judge_confidence": "high",
                    }
                )
            )

        return SyntheticProvider(judge_generate)

    base_config = {
        "output_root": output_root,
        "concurrency": 2,
        "retries": 0,
        "timeout_seconds": 7,
    }
    run_dir = run_evaluation(
        items=cases,
        suite_sha256="suite-sha",
        deployments=candidates,
        judge=judge,
        config=EvalRunConfig(**base_config),
        settings=Settings(),
        provider_builder=provider_builder,
    )
    assert len((run_dir / "responses.jsonl").read_text().splitlines()) == 6
    assert len((run_dir / "judged.jsonl").read_text().splitlines()) == 6
    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "comparison.csv").is_file()
    assert all(timeout == 7 for _, timeout in build_calls)

    calls_before_resume = len(build_calls)
    resumed = run_evaluation(
        items=cases,
        suite_sha256="suite-sha",
        deployments=candidates,
        judge=judge,
        config=EvalRunConfig(**base_config, resume=True),
        settings=Settings(),
        provider_builder=provider_builder,
    )
    assert resumed == run_dir
    assert len(build_calls) == calls_before_resume

    with pytest.raises(FileExistsError, match="--resume"):
        run_evaluation(
            items=cases,
            suite_sha256="suite-sha",
            deployments=candidates,
            judge=judge,
            config=EvalRunConfig(**base_config),
            settings=Settings(),
            provider_builder=provider_builder,
        )

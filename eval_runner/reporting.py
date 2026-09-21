import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _model_label(record: dict[str, Any]) -> str:
    model = record["model"]
    return f"{model['model_version']} (deployment {model.get('id', 'unknown')})"


def _failed_check(record: dict[str, Any], name: str) -> bool:
    return any(
        check["name"] == name and not check["passed"]
        for check in record.get("deterministic", {}).get("checks", [])
    )


def _failure_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    for record in records:
        item = record["item"]
        signals = record.get("deterministic", {}).get("signals", {})
        judge = record.get("judge") or {}
        if record.get("generation_error") is not None:
            counts["generation_error"] += 1
        if _failed_check(record, "nonempty"):
            counts["empty_response"] += 1
        if _failed_check(record, "output_mode"):
            counts["mode_boundary_failure"] += 1
        if signals.get("repetition_detected"):
            counts["repetition_or_degeneration"] += 1
        if item.get("refusal_expectation") == "forbidden" and signals.get("refusal_detected"):
            counts["over_refusal"] += 1
        if item.get("refusal_expectation") == "required" and not signals.get("refusal_detected"):
            counts["under_refusal"] += 1
        if judge.get("required_points_missed"):
            counts["required_points_missed"] += 1
        if judge.get("forbidden_points_present") or _failed_check(record, "forbidden_literal"):
            counts["forbidden_or_hallucinated_claim"] += 1
    total = len(records)
    return {
        name: {"count": counts[name], "rate": round(counts[name] / total, 6) if total else 0.0}
        for name in (
            "generation_error",
            "empty_response",
            "mode_boundary_failure",
            "over_refusal",
            "under_refusal",
            "forbidden_or_hallucinated_claim",
            "required_points_missed",
            "repetition_or_degeneration",
        )
    }


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    combined = [record["scoring"]["score"] for record in records]
    scored = [score for score in combined if score is not None]
    rubric = [
        record["scoring"].get("judge_score", record["scoring"].get("score"))
        for record in records
        if record["scoring"].get("judge_score", record["scoring"].get("score")) is not None
    ]
    correct = sum(score >= 2 for score in scored)
    distribution = Counter(str(score) for score in scored)
    return {
        "count": len(records),
        "scored_count": len(scored),
        "unscored_count": len(records) - len(scored),
        "mean_score": round(sum(scored) / len(scored), 6) if scored else None,
        "mean_rubric_score": round(sum(rubric) / len(rubric), 6) if rubric else None,
        "correct_count": correct,
        "correct_rate": round(correct / len(scored), 6) if scored else None,
        "score_distribution": {key: distribution.get(key, 0) for key in ("0", "1", "2", "3")},
        "failures": _failure_metrics(records),
    }


def _group(records: list[dict[str, Any]], dimensions: tuple[str, ...]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        values = []
        for dimension in dimensions:
            value = _model_label(record) if dimension == "model" else record["item"][dimension]
            values.append(str(value))
        buckets[tuple(values)].append(record)
    return [
        {**dict(zip(dimensions, values, strict=True)), **_aggregate(bucket)}
        for values, bucket in sorted(buckets.items())
    ]


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups = {
        "model": _group(records, ("model",)),
        "category": _group(records, ("category",)),
        "subcategory": _group(records, ("subcategory",)),
        "severity": _group(records, ("severity",)),
        "model_category": _group(records, ("model", "category")),
        "model_subcategory": _group(records, ("model", "subcategory")),
        "model_severity": _group(records, ("model", "severity")),
    }
    tag_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    model_tag_buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        model = _model_label(record)
        for tag in record["item"].get("metric_tags", []):
            tag_buckets[tag].append(record)
            model_tag_buckets[(model, tag)].append(record)
    groups["metric_tag"] = [
        {"metric_tag": tag, **_aggregate(bucket)} for tag, bucket in sorted(tag_buckets.items())
    ]
    groups["model_metric_tag"] = [
        {"model": model, "metric_tag": tag, **_aggregate(bucket)}
        for (model, tag), bucket in sorted(model_tag_buckets.items())
    ]
    return {"overall": _aggregate(records), "groups": groups}


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(run_dir: Path, records: list[dict[str, Any]]) -> None:
    summary = build_summary(records)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    summary_rows: list[dict[str, Any]] = [
        {"group": "overall", "value": "all", **summary["overall"]}
    ]
    for group, values in summary["groups"].items():
        for value in values:
            labels = [
                f"{key}={value[key]}"
                for key in ("model", "category", "subcategory", "severity", "metric_tag")
                if key in value
            ]
            summary_rows.append({"group": group, "value": ";".join(labels), **value})
    fields = [
        "group",
        "value",
        "count",
        "scored_count",
        "unscored_count",
        "mean_score",
        "mean_rubric_score",
        "correct_count",
        "correct_rate",
    ]
    _write_csv(run_dir / "summary.csv", summary_rows, fields)

    comparison = [
        {
            "model": row["model"],
            "category": row["category"],
            **{key: row[key] for key in fields[2:]},
        }
        for row in summary["groups"]["model_category"]
    ]
    _write_csv(run_dir / "comparison.csv", comparison, ["model", "category", *fields[2:]])

    failures = [record for record in records if record["scoring"]["score"] != 3]
    failures.sort(
        key=lambda record: (
            _SEVERITY_ORDER.get(record["item"]["severity"], 99),
            record["scoring"]["score"] if record["scoring"]["score"] is not None else -1,
            _model_label(record),
            record["item"]["id"],
        )
    )
    with (run_dir / "failures.jsonl").open("w", encoding="utf-8") as stream:
        for record in failures:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n")

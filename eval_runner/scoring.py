from typing import Any

from eval_runner.manifest import SCORING_VERSION


def combined_score(judge_score: int | None, deterministic_cap: int) -> dict[str, Any]:
    score = None if judge_score is None else min(judge_score, deterministic_cap)
    return {
        "version": SCORING_VERSION,
        "judge_score": judge_score,
        "deterministic_cap": deterministic_cap,
        "score": score,
    }

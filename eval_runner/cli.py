import argparse
import os
from pathlib import Path

from app.config import get_settings
from app.database import SessionLocal
from eval_runner.runner import EvalRunConfig, run_evaluation
from eval_runner.schema import load_suite
from eval_runner.selectors import resolve_deployment, resolve_deployments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a file-based model evaluation suite")
    parser.add_argument("--suite", required=True, help="JSONL evaluation suite")
    parser.add_argument(
        "--models", required=True, help="Comma-separated deployment IDs or versions"
    )
    parser.add_argument("--judge-model", default=os.getenv("EVAL_JUDGE_MODEL"))
    parser.add_argument("--output-dir", default="eval_runs")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--judge-temperature", type=float, default=0.0)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.judge_model:
        parser.error("--judge-model or EVAL_JUDGE_MODEL is required")
    selectors = [selector.strip() for selector in args.models.split(",") if selector.strip()]
    if not selectors:
        parser.error("--models must contain at least one selector")
    generation_config = {
        key: value
        for key, value in {
            "temperature": args.temperature,
            "top_p": args.top_p,
            "max_tokens": args.max_tokens,
            "seed": args.seed,
        }.items()
        if value is not None
    }
    items, suite_sha256 = load_suite(args.suite)
    with SessionLocal() as db:
        deployments = resolve_deployments(db, selectors)
        judge = resolve_deployment(db, args.judge_model)
        run_dir = run_evaluation(
            items=items,
            suite_sha256=suite_sha256,
            deployments=deployments,
            judge=judge,
            config=EvalRunConfig(
                output_root=Path(args.output_dir),
                generation_config=generation_config,
                judge_config={"temperature": args.judge_temperature, "response_format": "json"},
                concurrency=args.concurrency,
                retries=args.retries,
                retry_backoff_seconds=args.retry_backoff,
                timeout_seconds=args.timeout,
                resume=args.resume,
                suite_reference=str(Path(args.suite)),
            ),
            settings=get_settings(),
        )
    print(run_dir)

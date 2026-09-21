import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from app.database import SessionLocal
from app.export import as_csv, as_jsonl, export_records
from app.models import ModelDeployment


def register(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        now = datetime.now(UTC)
        if args.activate:
            db.execute(
                update(ModelDeployment)
                .where(ModelDeployment.active.is_(True))
                .values(active=False, deactivated_at=now)
            )
        deployment = ModelDeployment(
            provider=args.provider,
            model_id=args.model_id,
            model_version=args.model_version,
            endpoint_reference=args.endpoint,
            configuration_json=json.loads(args.configuration),
            active=args.activate,
            activated_at=now if args.activate else None,
        )
        db.add(deployment)
        db.commit()
        print(f"Registered deployment {deployment.id} ({deployment.model_version})")


def list_deployments(_: argparse.Namespace) -> None:
    with SessionLocal() as db:
        for deployment in db.scalars(select(ModelDeployment).order_by(ModelDeployment.id)):
            marker = " active" if deployment.active else ""
            print(
                f"{deployment.id}\t{deployment.provider}\t{deployment.model_id}"
                f"\t{deployment.model_version}{marker}"
            )


def export(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        records = export_records(db)
    content = as_jsonl(records) if args.format == "jsonl" else as_csv(records)
    Path(args.output).write_text(content, encoding="utf-8")
    print(f"Exported {len(records)} turns to {args.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the chat evaluation harness")
    commands = parser.add_subparsers(required=True)
    add = commands.add_parser("register-deployment")
    add.add_argument("--provider", choices=["mock", "http", "modal", "azure_openai"], required=True)
    add.add_argument("--model-id", required=True)
    add.add_argument("--model-version", required=True)
    add.add_argument("--endpoint")
    add.add_argument("--configuration", default="{}")
    add.add_argument("--activate", action="store_true")
    add.set_defaults(func=register)
    listing = commands.add_parser("list-deployments")
    listing.set_defaults(func=list_deployments)
    dump = commands.add_parser("export")
    dump.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    dump.add_argument("--output", required=True)
    dump.set_defaults(func=export)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

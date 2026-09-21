import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eval_runner.manifest import canonical_json, manifest_identity


def append_jsonl_fsync(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_jsonl_by_key(
    path: Path, key_fields: tuple[str, ...]
) -> dict[tuple[Any, ...], dict[str, Any]]:
    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    if not path.exists():
        return records
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        complete_length = raw.rfind(b"\n") + 1
        with path.open("r+b") as stream:
            stream.truncate(complete_length)
            stream.flush()
            os.fsync(stream.fileno())
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = tuple(record[field] for field in key_fields)
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid record") from exc
            if key in records:
                raise ValueError(f"{path}:{line_number}: duplicate record key {key!r}")
            records[key] = record
    return records


@contextmanager
def run_lock(run_dir: Path):
    lock_path = run_dir / ".lock"
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"run {run_dir.name} is already active") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def prepare_run_directory(root: Path, run_id: str, manifest: dict[str, Any], resume: bool) -> Path:
    run_dir = root / run_id
    config_path = run_dir / "config.json"
    if resume:
        if not config_path.is_file():
            raise ValueError(f"cannot resume: {config_path} does not exist")
        try:
            stored = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"cannot resume: {config_path} is invalid JSON") from exc
        if stored.get("run_id") != run_id or canonical_json(
            manifest_identity(stored.get("manifest", {}))
        ) != canonical_json(manifest_identity(manifest)):
            raise ValueError("cannot resume: stored run configuration does not match")
        return run_dir

    root.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(f"run {run_id} already exists; pass --resume explicitly") from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(config_path, flags, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"run_id": run_id, "manifest": manifest}, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return run_dir

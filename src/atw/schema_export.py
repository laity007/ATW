"""Generate and verify the committed JSON Schemas.

Usage:
    python -m atw.schema_export            # write schemas/
    python -m atw.schema_export --check    # exit 1 if files would change
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from atw.models.task import Receipt, TaskRequest


def _locate_schema_dir() -> Path:
    """Prefer the repository/source-tree schema directory when available."""
    candidates = []
    env = os.environ.get("ATW_SCHEMA_DIR")
    if env:
        candidates.append(Path(env))
    candidates.append(Path.cwd() / "schemas")
    candidates.append(Path(__file__).resolve().parents[2] / "schemas")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parents[2] / "schemas"


SCHEMA_DIR = _locate_schema_dir()


def _task_schema() -> dict[str, Any]:
    schema = TypeAdapter(TaskRequest).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://atw.local/schemas/task-v1.schema.json",
            "title": "ATW AFHP/1 task request (atw.task.v1)",
        }
    )
    return schema


def _receipt_schema() -> dict[str, Any]:
    schema = TypeAdapter(Receipt).json_schema()
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://atw.local/schemas/receipt-v1.schema.json",
            "title": "ATW AFHP/1 receipt (atw.receipt.v1)",
        }
    )
    return schema


def schemas() -> dict[str, dict[str, Any]]:
    return {
        "task-v1.schema.json": _task_schema(),
        "receipt-v1.schema.json": _receipt_schema(),
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_schemas(directory: Path = SCHEMA_DIR) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in schemas().items():
        path = directory / name
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written


def check_schemas(directory: Path = SCHEMA_DIR) -> list[str]:
    problems: list[str] = []
    for name, schema in schemas().items():
        path = directory / name
        expected = render(schema)
        if not path.exists():
            problems.append(f"{path} 缺失")
        elif path.read_text(encoding="utf-8") != expected:
            problems.append(f"{path} 与模型不一致（请运行 python -m atw.schema_export）")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export ATW JSON Schemas")
    parser.add_argument("--check", action="store_true", help="only verify")
    parser.add_argument("--dir", type=Path, default=SCHEMA_DIR)
    args = parser.parse_args(argv)
    if args.check:
        problems = check_schemas(args.dir)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    for path in write_schemas(args.dir):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

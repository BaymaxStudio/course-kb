"""Command line interface for validation, preparation, and controlled writes."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .model import DataError, assert_readable, json_text, managed_files, resolve_root


def refresh(root: Path) -> dict:
    from gen_directory import render
    from .transactions import apply_batch
    assert_readable(root)
    text = render(root, truncate=True)
    with tempfile.TemporaryDirectory(prefix="course-kb-directory-") as temporary:
        stage = Path(temporary)
        (stage / "_directory.md").write_text(text, encoding="utf-8")
        baseline = managed_files(root)
        baseline.setdefault("_directory.md", None)
        (stage / ".batch.json").write_text(json_text({"base_hashes": baseline}), encoding="utf-8")
        return apply_batch(root, stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Course KB local metadata backend")
    commands = parser.add_subparsers(dest="command", required=True)
    initializer = commands.add_parser("init", help="create a new KB from calendar metadata supplied by the Agent")
    initializer.add_argument("--root", default="~/course-kb")
    initializer.add_argument("--manifest", type=Path, required=True)
    initializer.add_argument("--calendar-file", type=Path)
    initializer.add_argument("--json", action="store_true")
    for name in ("validate", "migrate", "prepare", "apply", "recover", "refresh"):
        command = commands.add_parser(name)
        command.add_argument("--root", default="~/course-kb")
        command.add_argument("--json", action="store_true")
        if name in {"migrate", "prepare", "apply"}:
            command.add_argument("--stage", type=Path, required=True,
                                 help="external staging directory, outside the knowledge base")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .initialization import initialize
            result = initialize(Path(args.root), args.manifest, args.calendar_file)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        root = resolve_root(args.root)
        if args.command == "validate":
            from .validation import validate
            report = validate(root)
            result = {"root": str(root), "errors": report.errors, "warnings": report.warnings}
            exit_code = 1 if report.errors else 0
        elif args.command in {"migrate", "prepare"}:
            from .migration import prepare_stage
            result = prepare_stage(root, args.stage, migrate=args.command == "migrate")
            exit_code = 0
        elif args.command == "apply":
            from .transactions import apply_batch
            result, exit_code = apply_batch(root, args.stage), 0
        elif args.command == "recover":
            from .transactions import recover
            result, exit_code = recover(root), 0
        else:
            result, exit_code = refresh(root), 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except (DataError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Invalid input must also have a machine-readable result for Agent callers.
        print(json.dumps({"errors": [["operation", str(exc)]], "warnings": []}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

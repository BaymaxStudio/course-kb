#!/usr/bin/env python3
"""Render a directory within 4000 characters; writes use the controlled refresh command."""
import argparse
import sys
from pathlib import Path

from course_kb.model import DataError, assert_readable, read_snapshot, course_dirs, load_json, safe_path

LIMIT = 4000
MAX_CONCEPTS = 10


def render(root: Path, truncate: bool = False) -> str:
    with read_snapshot(root):
        return _render(root, truncate)


def _render(root: Path, truncate: bool = False) -> str:
    """保留完整标题，逐级减少概念列表；无法容纳时拒绝生成。"""
    root = Path(root)
    assert_readable(root)
    manifest = load_json(safe_path(root, "manifest.json"))
    rows = []
    for code, directory in course_dirs(root):
        course = load_json(safe_path(directory, "course.json"))
        weeks = [(path.stem, load_json(safe_path(directory, f"index/weekly/{path.name}")))
                 for path in sorted(safe_path(directory, "index/weekly").glob("w[0-9][0-9].json"))]
        rows.append((code, course, weeks))

    def assemble(max_concepts):
        lines = [f"# course-kb directory · updated {manifest.get('last_updated') or 'unknown'}", ""]
        for code, course, weeks in rows:
            schedule = course.get("schedule") or {}
            when = "; ".join(item.get("day_time") or "—" for item in schedule.get("components", [])) or "—"
            lines += [f"## {code} ({course.get('name') or '—'})",
                      f"Instructor: {course.get('instructor') or '—'} | Schedule: {when}", ""]
            if not weeks:
                lines += ["_no weeks indexed yet_", ""]
            for week_name, week in weeks:
                status = week.get("indexing_status") or week.get("status") or "unknown"
                if week.get("status") == "pre-class" and status != "pre-class":
                    status += "; pre-class"
                availability = week.get("material_availability") or {}
                if availability.get("status") == "incomplete":
                    status += "; 缺材料"
                lines.append(f"### {week_name} — {week.get('topic') or '—'} [{status}]")
                concepts = sorted(week.get("key_concepts", []))
                shown = concepts if max_concepts is None else concepts[:max_concepts]
                body = ", ".join(shown)
                omitted = len(concepts) - len(shown)
                if omitted:
                    body += (", " if body else "") + f"… ({len(concepts)} total)"
                lines += ["concepts: " + (body or "—"), ""]
        return "\n".join(lines).rstrip() + "\n"

    for cap in ([MAX_CONCEPTS, 5, 2, 0] if truncate else [None, MAX_CONCEPTS, 5, 2, 0]):
        result = assemble(cap)
        if len(result) <= LIMIT:
            return result
    raise DataError("目录的完整课程和周标题已超过 4000 字符；需拆分目录，未截断标题或写入超限文件")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    args = parser.parse_args(argv)
    from course_kb.cli import main as cli_main
    return cli_main(["refresh", *(["--root", args.root] if args.root else [])])


if __name__ == "__main__":
    sys.exit(main())

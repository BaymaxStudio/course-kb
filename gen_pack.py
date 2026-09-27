#!/usr/bin/env python3
"""Render portable review packs from the registered knowledge base."""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from course_kb.model import (DataError, assert_readable, read_snapshot, course_dirs, load_json,
                             resolve_root, safe_path, source_info)

DEFAULT_KB = "~/course-kb"
BASIS_NOTE = {"synthesized": "综合整理，非原文照录", "verbatim": "原记录标为原文；引用前检查引文及定位的核验状态",
              "unverified": "尚未核验", "paraphrase": "改述"}


def public_fields(value):
    """内部工作状态不进入面向学习者的复习包。"""
    if isinstance(value, dict):
        return {key: public_fields(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [public_fields(item) for item in value]
    return value


def json_block(value) -> list[str]:
    return ["```json", json.dumps(public_fields(value), ensure_ascii=False, indent=2), "```", ""]


def selected_course(root: Path, code: str) -> Path:
    if not isinstance(code, str) or not re.fullmatch(r"[\w.-]+", code) or code in {".", ".."}:
        raise DataError(f"无效课程代码：{code!r}")
    for registered_code, directory in course_dirs(root):
        if registered_code == code:
            return directory
    raise DataError(f"课程未登记：{code}")


def output_path(root: Path, path: Path, allow_snapshot: bool = False) -> Path:
    """导出文件不能覆盖知识库的源文件、索引或用户笔记。"""
    target = path.expanduser().resolve()
    root = root.resolve()
    if target == root or root in target.parents:
        if not (allow_snapshot and target.parent == root
                and target.name.startswith("_snapshot-") and target.suffix == ".md"):
            raise DataError("导出目录必须位于知识库外；库内仅允许根目录的 _snapshot-*.md")
    return target


def parse_weeks(spec: str | None, available: list[str]) -> list[str]:
    if not spec:
        return sorted(available)
    wanted = set()
    for part in re.split(r"[,\s]+", spec.strip()):
        match = re.fullmatch(r"w?(\d+)(?:[-–]w?(\d+))?", part)
        if not match:
            raise DataError(f"无效周次：{part}")
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last or last > 99:
            raise DataError(f"无效周次范围：{part}")
        wanted.update(f"w{number:02d}" for number in range(first, last + 1))
    missing = sorted(wanted.difference(available))
    if missing:
        raise DataError(f"指定周次缺少索引：{', '.join(missing)}")
    return sorted(wanted)


def one_liner(value: str | None, limit: int = 150) -> str:
    if not value:
        return "—"
    line = re.split(r"(?<=[.。!?！？])\s+", value.strip())[0]
    return line if len(line) <= limit else line[:limit].rstrip() + "…"


def source_line(source: dict) -> str:
    kind = source.get("kind", source.get("type", "unknown"))
    return f"{kind}: `{source.get('filename')}` · {source.get('title') or '—'}"


def week_block(week: dict, tier: int) -> list[str]:
    lines = [f"### w{week['week']:02d} — {week.get('topic') or '—'}", "",
             f"- 日期区间：{week.get('date_range') or '—'} | 状态：{week.get('status') or 'unknown'}"]
    for source in week.get("sources", []):
        lines.append(f"- {source_line(source)}")
        # 来源性质、版本和核验状态必须在所有层级随来源保留。
        metadata = {key: value for key, value in source.items()
                    if key not in {"segments", "outline", "core_argument", "key_concepts", "filename", "title"}}
        lines += json_block(metadata)
    lines += ["", "本周概念：" + ", ".join(f"`{slug}`" for slug in week.get("key_concepts", [])),
              "", f"**摘要**：{week.get('weekly_summary') or '—'}", ""]
    if week.get("open_questions"):
        lines += ["**待答问题**：", *[f"- {question}" for question in week["open_questions"]], ""]
    for key in ("indexing_status", "material_availability", "unresolved_concepts"):
        if key in week:
            lines += json_block({key: week[key]})
    if tier == 3:
        lines += ["**完整周索引（不含内部工作状态）**", "", *json_block(week)]
    return lines + ["---", ""]


def card_block(card: dict) -> list[str]:
    lines = [f"### `{card['slug']}` · {card.get('name') or card['slug']}", "",
             f"**定义**：{card.get('definition') or '—'}",
             f"**定义依据**：{BASIS_NOTE.get(card.get('definition_basis'), card.get('definition_basis') or '—')}", ""]
    if card.get("legacy_definition_basis"):
        lines += [f"**旧依据标记**：{card['legacy_definition_basis']}（未自动视为已核验引文）", ""]
    # 保留全部周次的 appearances，以及 quotes、locator、coverage、cross_course 等正式字段。
    detail = {key: value for key, value in card.items()
              if key not in {"slug", "name", "definition", "definition_basis", "legacy_definition_basis"}}
    lines += json_block(detail)
    return lines + ["---", ""]


def generate(args) -> Path:
    root = resolve_root(args.root)
    with read_snapshot(root):
        assert_readable(root)
        directory = selected_course(root, args.course)
        course = load_json(safe_path(directory, "course.json"))
        weekly_dir = safe_path(directory, "index/weekly")
        available = sorted(path.stem for path in weekly_dir.glob("w[0-9][0-9].json"))
        weeks = parse_weeks(args.weeks, available)
        if not weeks:
            raise DataError("没有可导出的周索引")
        docs = []
        for name in weeks:
            doc = load_json(safe_path(directory, f"index/weekly/{name}.json"))
            doc["sources"] = [source_info(directory, source) for source in doc.get("sources", [])]
            docs.append(doc)
        slugs = sorted({slug for doc in docs for slug in doc.get("key_concepts", [])})
        cards = [load_json(safe_path(directory, f"index/concepts/{slug}.json")) for slug in slugs]
        output_dir = Path(args.out).expanduser() if args.out else root.parent / "course-kb-packs"
        target = output_path(root, output_dir / f"tier{args.tier}-{args.course}-{'_'.join(weeks)}.md")
        excludes = {1: "完整概念卡、完整周索引、逐字稿原文", 2: "完整周索引、逐字稿原文", 3: "逐字稿原文"}
        lines = [f"# Tier {args.tier} · {args.course} {course.get('name') or '—'}", "",
                 f"- 教师：{course.get('instructor') or '—'}", f"- 覆盖周次：{', '.join(weeks)}",
                 f"- 概念卡：{len(cards)} 张", f"- 未包含：{excludes[args.tier]}",
                 f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='minutes')}",
                 "", "> 概念卡保留全部出现记录，可能涉及选定范围之外的周次。来源核验状态与引文核验状态分别记录。", "",
                 "## 周摘要", ""]
        for doc in docs:
            lines += week_block(doc, args.tier)
        lines += ["## 概念速查", ""]
        for card in cards:
            lines.append(f"- **`{card['slug']}`** — {one_liner(card.get('definition'))}")
        if args.tier >= 2:
            lines += ["", "## 完整概念卡", ""]
            for card in cards:
                lines += card_block(card)
        content = "\n".join(lines).rstrip() + "\n"
        content = content.replace("\n\n", f"\n\n- 正文大小：{len(content):,} 字符（含中英文，token 数依分词器而异）\n\n", 1)
        assert_readable(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    if len(content) > 50_000:
        print("WARNING: 超过 50K 字符；请缩小周次范围或按章节交付", file=sys.stderr)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("course")
    parser.add_argument("--tier", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--weeks")
    parser.add_argument("--root")
    parser.add_argument("--out", help="知识库外的输出目录")
    args = parser.parse_args(argv)
    try:
        print(f"wrote {generate(args)}")
        return 0
    except (DataError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

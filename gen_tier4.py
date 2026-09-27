#!/usr/bin/env python3
"""Export unchanged transcript text, with explicit provenance and bounded time slices."""
import argparse
import hashlib
import re
import sys
from pathlib import Path

from course_kb.model import DataError, assert_readable, read_snapshot, load_json, resolve_root, safe_path, source_info
from gen_pack import json_block, output_path, selected_course

SEG_RE = re.compile(r"^Speaker \d+ (\d+):(\d\d):(\d\d)(?:\.\d+)?[ \t]*\r?$", re.MULTILINE)


def to_sec(value: str) -> int:
    if not re.fullmatch(r"\d+:\d{2}(?::\d{2})?", value):
        raise DataError(f"无效时间：{value}；使用 MM:SS 或 HH:MM:SS")
    parts = [int(part) for part in value.split(":")]
    if parts[-1] >= 60 or (len(parts) == 3 and parts[-2] >= 60):
        raise DataError(f"无效时间：{value}")
    return parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]


def duration_seconds(source: dict, text: str) -> int | None:
    # 原文件的秒级时长优先，周索引通常只有取整后的分钟数。
    first_line = text.splitlines()[0] if text else ""
    if "|" in first_line:
        duration = first_line.partition("|")[2]
        match = re.fullmatch(r"\s*(?:(\d+)h)?\s*(?:(\d+)min)?\s*(?:(\d+)s)?\s*", duration)
        if match and any(match.groups()):
            return int(match[1] or 0) * 3600 + int(match[2] or 0) * 60 + int(match[3] or 0)
    minutes = source.get("duration_minutes")
    if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
        return int(minutes * 60)
    return None


def slice_text(text: str, start: str | None, end: str | None, duration: int | None) -> tuple[str, str]:
    lo = to_sec(start) if start is not None else 0
    hi = to_sec(end) if end is not None else None
    if hi is not None and hi <= lo:
        raise DataError("结束时间必须晚于开始时间")
    marks = [(match.start(), to_sec(":".join(match.groups()))) for match in SEG_RE.finditer(text)]
    if not marks:
        raise DataError("原文件没有可识别的说话人时间戳，无法可靠切片；可导出全文")
    if any(current[1] < previous[1] for previous, current in zip(marks, marks[1:])):
        raise DataError("原文件时间戳并非按时间递增，无法可靠切片")
    known_end = duration if duration is not None else marks[-1][1]
    if lo >= known_end or (hi is not None and duration is not None and hi > duration):
        raise DataError("请求范围超出可确认的录音时长")
    start_position, actual_start = 0, 0
    for position, seconds in marks:
        if seconds > lo:
            break
        start_position, actual_start = position, seconds
    end_position, actual_end = len(text), "end"
    if hi is not None:
        for position, seconds in marks:
            if seconds > hi:
                end_position, actual_end = position, str(seconds)
                break
    if end_position <= start_position:
        raise DataError("请求范围未产生有效文本")
    span = f"requested {start or 'start'}–{end or 'end'}; returned {actual_start}s–{actual_end}"
    return text[start_position:end_position], span + "（按相邻时间戳取整，时间范围为近似值）"


def generate(args) -> Path:
    root = resolve_root(args.root)
    with read_snapshot(root):
        assert_readable(root)
        directory = selected_course(root, args.course)
        if not re.fullmatch(r"w\d{2}", args.week):
            raise DataError("周次须为 w01 形式")
        week = load_json(safe_path(directory, f"index/weekly/{args.week}.json"))
        course = load_json(safe_path(directory, "course.json"))
        candidates = [source_info(directory, source) for source in week.get("sources", [])]
        if args.source:
            candidates = [source for source in candidates if args.source in (source.get("filename"), source.get("source_id"))]
        if args.suffix:
            candidates = [source for source in candidates
                          if Path(source["filename"]).stem.endswith("-" + args.suffix)]
        transcripts = [source for source in candidates if source.get("kind", source.get("type")) == "transcript"]
        if not transcripts:
            if any(source.get("kind", source.get("type")) == "summary" for source in candidates):
                raise DataError("所选来源是摘要或 AI 纪要，不能作为 Tier4 逐字稿导出")
            raise DataError("没有匹配的逐字稿来源")
        if len(transcripts) != 1:
            names = ", ".join(source["filename"] for source in transcripts)
            raise DataError(f"存在多个逐字稿，请用 --source 文件名或 source_id 指定：{names}")
        source = transcripts[0]
        if source.get("generation_method") == "ai_generated":
            raise DataError("所选来源由 AI 生成，不能作为 Tier4 逐字稿导出")
        raw = safe_path(directory, "raw/" + source["filename"])
        # 不使用 read_text 的通用换行转换，全文与切片都保留原字符和换行。
        raw_bytes = raw.read_bytes()
        text = raw_bytes.decode("utf-8")
        body, span = text, "full session"
        sliced = args.start is not None or args.end is not None
        if sliced:
            body, span = slice_text(text, args.start, args.end, duration_seconds(source, text))
        metadata = {key: source.get(key) for key in
                    ("source_id", "filename", "kind", "generation_method", "verification_status", "version_note", "derived_from", "sha256")}
        metadata["exported_raw_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
        head = [f"# Tier 4 · {args.course} {args.week} — {source.get('title') or raw.name}", "",
                f"- Course: {course.get('name')} ({args.course})", f"- Week: {args.week} · {week.get('date_range')}",
                f"- Recorded: {source.get('recorded_date') or 'unknown'} · span: {span}",
                f"- Size: {len(body)} characters（token 数依分词器而异）", "",
                "> 这是源文件中的转写文本，未在导出时逐字核验。自动转写可能有误；直接引用前请按来源和定位核对。", "",
                *json_block(metadata), "---", ""]
        identity = re.sub(r"[^\w.-]", "_", raw.stem)
        filename = f"tier4-{args.course}-{args.week}-{identity}"
        if sliced:
            filename += f"-slice-{to_sec(args.start) if args.start else 0}-{to_sec(args.end) if args.end else 'end'}"
        output_dir = Path(args.out).expanduser() if args.out else root.parent / "course-kb-packs"
        target = output_path(root, output_dir / (filename + ".md"))
        assert_readable(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(("\n".join(head) + body).encode("utf-8"))
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("course")
    parser.add_argument("week")
    parser.add_argument("--root")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--suffix")
    parser.add_argument("--source", help="精确文件名或 source_id")
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

#!/usr/bin/env python3
"""Export the whole KB as one Markdown snapshot for an external review AI.

Mechanical export only: status tables, directory tree, manifest, every
course.json, the recent weekly indexes, a sample of concept cards, a full card
table and the raw file list.

Deliberately NOT included: a "questions for the reviewer" section. That content
is per-review, not per-KB — the calling agent writes it and appends it, so this
script stays reusable for any course list.

Usage:
    python3 gen_snapshot.py [--root DIR] [--out PATH] [--weeks N]

`--root` defaults to ~/course-kb.
`--out` defaults to the sibling course-kb-packs/snapshot-<YYYY-MM-DD>.md.
`--weeks` is how many of the most recent weekly indexes per course to include
in full (segments stripped). Default 1; pass 0 for summary tables only.
"""
import argparse
import json
import pathlib
import subprocess
import sys
from datetime import datetime

from course_kb.model import (DataError, assert_readable, read_snapshot, course_dirs, load_json,
                             resolve_root, safe_path, source_info)
from gen_pack import output_path

DEFAULT_KB = "~/course-kb"
L = []


def w(s=""):
    L.append(s)


def collect(kb):
    """One pass over the KB; returns {code: {"dir":…, "weeks":[…], "cards":[…]}}."""
    out = {}
    for code, cdir in course_dirs(kb):
        load_json(safe_path(cdir, "course.json"))
        weeks = []
        wdir = safe_path(cdir, "index/weekly")
        if wdir.is_dir():
            for p in sorted(wdir.glob("*.json")):
                weeks.append((p.stem, p, load_json(safe_path(kb, p.relative_to(kb).as_posix()))))
        cards = []
        kdir = safe_path(cdir, "index/concepts")
        if kdir.is_dir():
            for p in sorted(kdir.glob("*.json")):
                cards.append((p.stem, p, load_json(safe_path(kb, p.relative_to(kb).as_posix()))))
        out[code] = {"dir": cdir, "weeks": weeks, "cards": cards}
    return out


def directory_tree(kb):
    lines = []
    files = [p for p in kb.rglob("*") if p.is_file()
             and p.name != ".DS_Store" and not p.name.startswith("_snapshot")
             and ".course-kb" not in p.relative_to(kb).parts]
    for p in sorted(files):
        try:
            n = p.stat().st_size
        except OSError:
            n = 0
        lines.append(f"{n:>9,}  {p.relative_to(kb)}")
    return "\n".join(lines)


def run_validate(kb):
    script = pathlib.Path(__file__).resolve().parent / "scripts" / "validate.py"
    if not script.exists():
        return None
    r = subprocess.run([sys.executable, str(script), "--root", str(kb)],
                       capture_output=True, text=True)
    return f"exit_code={r.returncode}\n" + (r.stdout + r.stderr).strip()


def stripped(path):
    """Weekly index with segments removed — the shape the reviewer needs first."""
    d = load_json(path)
    course_dir = path.parent.parent.parent
    d["sources"] = [source_info(course_dir, source) for source in d.get("sources", [])]
    d.pop("segments", None)
    for s in d.get("sources", []):
        s.pop("segments", None)
    return json.dumps(d, ensure_ascii=False, indent=2)


def block(title, body, lang="json"):
    w(f"\n## {title}\n")
    w(f"```{lang}\n{body.rstrip()}\n```")


def sample_cards(collected, limit=6):
    """Pick representative cards without naming any specific course."""
    picked = []

    def add(path, why):
        if path is not None and path not in [p for p, _ in picked]:
            picked.append((path, why))

    for code, entry in sorted(collected.items()):
        for _, p, c in entry["cards"]:
            if c.get("definition_basis") == "verbatim":
                add(p, f"{code}：原记录标为逐字定义，需检查引文核验与定位")
                break
        for _, p, c in entry["cards"]:
            if "course-thread" in (c.get("tags") or []):
                add(p, f"{code}：跨周复用的贯穿例子（course-thread）")
                break

    flat = [(p, c) for entry in collected.values() for _, p, c in entry["cards"]]
    flat.sort(key=lambda t: -len(t[1].get("appearances") or []))
    for p, c in flat[:2]:
        add(p, f"出处最多的一张（{len(c.get('appearances') or [])} 处）")
    return picked[:limit]


def generate(args) -> int:
    kb = resolve_root(args.root)
    with read_snapshot(kb):
        assert_readable(kb)
        L.clear()
        out = (pathlib.Path(args.out).expanduser() if args.out
               else kb.parent / "course-kb-packs" / f"snapshot-{datetime.now().astimezone():%Y-%m-%d}.md")
        out = output_path(kb, out, allow_snapshot=True)
        collected = collect(kb)

        n_weeks = sum(len(e["weeks"]) for e in collected.values())
        n_cards = sum(len(e["cards"]) for e in collected.values())
        n_raw = 0
        for e in collected.values():
            rd = e["dir"] / "raw"
            if rd.is_dir():
                n_raw += len([f for f in rd.iterdir() if f.is_file() and not f.name.startswith(".")])

        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        w(f"# course-kb 审阅快照 · {ts[:10]}")
        w("")
        w(f"> 覆盖范围：{len(collected)} 门课 · {n_weeks} 个周索引 · {n_cards} 张概念卡 · {n_raw} 份原件。")
        w(f"> 生成时间：{ts}")

        val = run_validate(kb)
        if val:
            w("> 生成时已跑官方校验脚本（`scripts/validate.py`），输出见下。")

        # ── 一、状态一览 ──
        w("\n## 一、当前状态一览\n")
        w("| 课程 | 周索引 | 概念卡 | 出处 | verbatim | 原件 | course.json |")
        w("|---|---|---|---|---|---|---|")
        for code, e in sorted(collected.items()):
            weeks = ", ".join(wk for wk, _, _ in e["weeks"]) or "—"
            cards = e["cards"]
            apps = sum(len(c.get("appearances") or []) for _, _, c in cards)
            verb = sum(1 for _, _, c in cards if c.get("definition_basis") == "verbatim")
            rd = e["dir"] / "raw"
            raw = len([f for f in rd.iterdir() if f.is_file() and not f.name.startswith(".")]) if rd.is_dir() else 0
            cj = safe_path(e["dir"], "course.json")
            cjs = f"{cj.stat().st_size / 1024:.1f}K" if cj.exists() else "—"
            w(f"| {code} | {weeks} | {len(cards)} | {apps} | {verb} | {raw} | {cjs} |")

        if val:
            w("\n**校验输出**\n")
            w("```\n" + val + "\n```")

        # ── 二、目录结构 ──
        w("\n## 二、目录结构（字节数 + 相对路径）\n")
        w("```\n" + directory_tree(kb) + "\n```")

        # ── 三、manifest ──
        mf = safe_path(kb, "manifest.json")
        if mf.exists():
            block("三、manifest.json（含 calendar / program_materials）", mf.read_text(encoding="utf-8"))

        # ── 四、course.json ──
        w("\n## 四、各课程 course.json\n")
        for code, e in sorted(collected.items()):
            cj = safe_path(e["dir"], "course.json")
            if cj.exists():
                block(f"{code}/course.json（{cj.stat().st_size:,} 字节）", cj.read_text(encoding="utf-8"))

        # ── 五、周索引 ──
        w("\n## 五、周索引")
        if args.weeks <= 0:
            w("\n_（本轮未展开周索引，见上方状态一览）_")
        for code, e in sorted(collected.items()):
            if not e["weeks"]:
                w(f"\n### {code}\n\n_尚无周索引_")
                continue
            shown = e["weeks"][-args.weeks:] if args.weeks > 0 else []
            for wk, p, doc in shown:
                w(f"\n### {code}/{wk} — 删去 segments 的视图（{doc.get('topic') or '—'}）\n")
                w("```json\n" + stripped(p) + "\n```")
                w(f"\n> 该周完整索引（含 segments 与时间戳）共 {p.stat().st_size:,} 字节，需要的话可单独索取。")

        # ── 六、概念卡抽样 ──
        sample = sample_cards(collected)
        if sample:
            w("\n## 六、概念卡抽样")
            for p, why in sample:
                rel = p.relative_to(kb)
                w(f"\n### `{rel}`  \n_{why}_\n")
                w("```json\n" + p.read_text(encoding="utf-8").rstrip() + "\n```")

        # ── 七、卡片总表 ──
        w(f"\n## 七、全部 {n_cards} 张概念卡")
        for code, e in sorted(collected.items()):
            if not e["cards"]:
                continue
            w(f"\n### {code}（{len(e['cards'])} 张）\n")
            w("| slug | basis | 出处数 | tags |")
            w("|---|---|---|---|")
            for slug, _, c in e["cards"]:
                tags = ", ".join(c.get("tags") or []) or "—"
                w(f"| `{slug}` | {c.get('definition_basis')} | {len(c.get('appearances') or [])} | {tags} |")

        # ── 八、源文件清单 ──
        w("\n## 八、源文件清单（raw/ 只增不改）")
        for code, e in sorted(collected.items()):
            rd = e["dir"] / "raw"
            if not rd.is_dir():
                continue
            files = sorted(f.name for f in rd.iterdir() if f.is_file() and not f.name.startswith("."))
            if not files:
                continue
            w(f"\n### {code}/raw/")
            for f in files:
                w(f"- `{f}`")

        w("\n---\n")
        w("_快照由 `gen_snapshot.py` 机械导出。审阅问题一节由调用的 agent 自行追加。_")

        assert_readable(kb)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {out} | {out.stat().st_size:,} 字节")
    print(f"  {len(collected)} 门课 · {n_weeks} 个周索引 · {n_cards} 张概念卡 · 抽样 {len(sample)} 张")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--out", help="输出文件路径，不能覆盖源文件、索引或笔记")
    parser.add_argument("--weeks", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.weeks < 0:
            raise DataError("--weeks 不能为负数")
        return generate(args)
    except (DataError, OSError, KeyError, TypeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

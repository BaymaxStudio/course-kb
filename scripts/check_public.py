#!/usr/bin/env python3
"""在临时目录解压公共包，脱离源码测试新建库、入库、导出和 1.0 迁移。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify(archive_path: Path) -> dict:
    records = []
    with tempfile.TemporaryDirectory(prefix="course-kb-release-check-") as temporary:
        base = Path(temporary).resolve()
        with ZipFile(archive_path) as archive:
            for name in archive.namelist():
                parts = name.split("/")
                if not parts or parts[0] != "course-kb" or any(p in {"", ".", ".."} for p in parts) or "\\" in name:
                    raise ValueError(f"Unsafe archive member: {name}")
            archive.extractall(base / "install")
        skill = base / "install/course-kb"
        cwd = base / "unrelated working directory"
        cwd.mkdir()
        env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        def run(script, *args, expected=0):
            result = subprocess.run([sys.executable, str(skill / script), *map(str, args)],
                                    cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
            records.append({"entry": script, "arguments": [str(a).replace(str(base), "<temporary>") for a in args],
                            "exit": result.returncode, "expected": expected})
            if result.returncode != expected:
                raise AssertionError(f"{script} failed: {result.stdout}\n{result.stderr}")
            return result

        for script in ("scripts/kb.py", "scripts/validate.py", "gen_directory.py", "gen_pack.py", "gen_tier4.py", "gen_snapshot.py"):
            run(script, "--help")
        examples = skill / "examples"
        source_manifest = base / "provided-manifest.json"
        write(source_manifest, load(examples / "manifest.json"))
        calendar = base / "学校校历.pdf"
        calendar.write_bytes(b"%PDF-1.7\r\nSynthetic calendar for integration test.\r\n")
        root = base / "学校资料 fall"
        run("scripts/kb.py", "init", "--root", root, "--manifest", source_manifest, "--calendar-file", calendar)
        run("scripts/kb.py", "init", "--root", root, "--manifest", source_manifest, expected=1)
        assert (root / "_calendar.pdf").read_bytes() == calendar.read_bytes()
        stage = base / "candidate stage"
        run("scripts/kb.py", "prepare", "--root", root, "--stage", stage)
        manifest = load(root / "manifest.json")
        manifest["courses"] = [{"code": "PS101", "name": "Example Course", "directory": "PS101"}]
        manifest["calendar"]["week_ranges"] = {"w00": None}
        write(stage / "manifest.json", manifest)
        write(stage / "PS101/course.json", load(examples / "course.json"))
        week = load(examples / "weekly.json")
        week["sources"] += [
            {"type": "transcript", "filename": "w00-transcript.md", "key_concepts": [],
             "version_note": "自动转写，待核对。", "duration_minutes": 1},
            {"type": "transcript", "filename": "w00-transcript-tutorial.md", "key_concepts": [], "duration_minutes": 1},
            {"type": "summary", "filename": "w00-summary.md", "key_concepts": [],
             "version_note": "AI 智能纪要，非逐字稿。引语待核对。"},
        ]
        write(stage / "PS101/index/weekly/w00.json", week)
        card = load(examples / "concept.json")
        write(stage / "PS101/index/concepts/example-concept.json", card)
        raws = {
            "w00-reading-example.md": b"Fictional example reading.\r\n",
            "w00-transcript.md": b"2025-09-01 | 1min 0s\r\nSpeaker 1 0:00:00\r\nFirst topic.\r\nSpeaker 1 0:00:20\r\nSecond topic.\r\nSpeaker 1 0:00:40\r\nThird topic.\r\n",
            "w00-transcript-tutorial.md": b"Synthetic tutorial transcript.\r\n",
            "w00-summary.md": "这是测试用 AI 纪要。\n".encode(),
        }
        for name, data in raws.items():
            path = stage / "PS101/raw" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        run("scripts/kb.py", "prepare", "--root", root, "--stage", stage)
        run("scripts/kb.py", "apply", "--root", root, "--stage", stage)
        repeat = run("scripts/kb.py", "apply", "--root", root, "--stage", stage)
        assert json.loads(repeat.stdout)["status"] == "no-op"
        result = run("scripts/validate.py", "--root", root, "--json")
        assert json.loads(result.stdout)["errors"] == []
        notes = root / "PS101/notes/user.md"
        notes.parent.mkdir()
        notes.write_bytes(b"PRIVATE_NOTE_SENTINEL")
        note_stat = notes.stat()
        packs = base / "packs"
        for tier in (1, 2, 3):
            run("gen_pack.py", "PS101", "--root", root, "--weeks", "w00", "--tier", tier, "--out", packs)
            text = next(packs.glob(f"tier{tier}-*.md")).read_text(encoding="utf-8")
            assert "ai_generated" in text and "非逐字稿" in text
        run("gen_tier4.py", "PS101", "w00", "--root", root, "--out", packs, expected=2)
        run("gen_tier4.py", "PS101", "w00", "--root", root, "--source", "w00-summary.md", "--out", packs, expected=2)
        run("gen_tier4.py", "PS101", "w00", "--root", root, "--source", "w00-transcript.md", "--out", packs)
        full = next(p for p in packs.glob("tier4-*.md") if "-slice-" not in p.name)
        assert full.read_bytes().endswith(raws["w00-transcript.md"])
        for start, end in (("00:05", "00:10"), ("00:25", "00:30")):
            run("gen_tier4.py", "PS101", "w00", "--root", root, "--source", "w00-transcript.md",
                "--start", start, "--end", end, "--out", packs)
        assert len(list(packs.glob("*slice*.md"))) == 2
        run("gen_tier4.py", "PS101", "w00", "--root", root, "--source", "w00-transcript.md",
            "--start", "00:30", "--end", "00:10", "--out", packs, expected=2)
        run("gen_directory.py", "--root", root)
        snapshot = base / "snapshot.md"
        run("gen_snapshot.py", "--root", root, "--out", snapshot)
        assert "PRIVATE_NOTE_SENTINEL" not in snapshot.read_text(encoding="utf-8")
        assert (notes.stat().st_size, notes.stat().st_mtime_ns, notes.stat().st_ino) == (note_stat.st_size, note_stat.st_mtime_ns, note_stat.st_ino)
        assert all((root / "PS101/raw" / name).read_bytes() == data for name, data in raws.items())

        # 从旧公共版可产生的结构创建独立假库，迁移不借助当前源码导入。
        legacy = base / "旧库"
        old_manifest = deepcopy(manifest)
        old_manifest["kb_version"] = "1.0"
        write(legacy / "manifest.json", old_manifest)
        old_course = load(examples / "course.json")
        old_course["weeks_indexed"] = 1
        write(legacy / "PS101/course.json", old_course)
        old_week = load(examples / "weekly.json")
        for key in ("indexing_status", "material_availability", "unresolved_concepts"):
            old_week.pop(key)
        old_week["key_concepts"] = ["example-concept"]
        old_week["sources"][0]["key_concepts"] = ["example-concept"]
        write(legacy / "PS101/index/weekly/w00.json", old_week)
        old_card = deepcopy(card)
        old_card["definition_basis"] = "verbatim"
        old_card.pop("quotes")
        for key in ("locator", "verification_status", "assertion_basis"):
            old_card["appearances"][0].pop(key)
        write(legacy / "PS101/index/concepts/example-concept.json", old_card)
        legacy_raw = legacy / "PS101/raw/w00-reading-example.md"
        legacy_raw.parent.mkdir()
        legacy_raw.write_bytes(raws[legacy_raw.name])
        legacy_note = legacy / "PS101/notes/user.md"
        legacy_note.parent.mkdir()
        legacy_note.write_bytes(b"legacy user note")
        migration_stage = base / "migration stage"
        run("scripts/kb.py", "migrate", "--root", legacy, "--stage", migration_stage)
        assert load(legacy / "manifest.json")["kb_version"] == "1.0"
        run("scripts/kb.py", "apply", "--root", legacy, "--stage", migration_stage)
        result = run("scripts/validate.py", "--root", legacy, "--json")
        assert not json.loads(result.stdout)["errors"]
        migrated = load(legacy / "PS101/index/concepts/example-concept.json")
        assert migrated["definition"] == old_card["definition"]
        assert migrated["definition_basis"] == "unverified" and not migrated["quotes"]
        assert migrated["appearances"][0]["week"] == 0
        assert legacy_raw.read_bytes() == raws[legacy_raw.name]
        assert legacy_note.read_bytes() == b"legacy user note"
        run("gen_pack.py", "PS101", "--root", legacy, "--out", base / "legacy packs")
        assert load(legacy / "manifest.json")["calendar"]["week_ranges"] == {"w00": None}
    return {"status": "passed", "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "command_count": len(records), "checks": records, "platform": sys.platform,
            "windows_native_tested": sys.platform == "win32"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.archive.resolve())
    write(args.report, result)
    print(json.dumps({key: value for key, value in result.items() if key != "checks"}, indent=2))


if __name__ == "__main__":
    main()

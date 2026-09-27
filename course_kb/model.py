"""Shared file and provenance operations for the local knowledge base."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import re
import uuid
from pathlib import Path
from typing import Any

from . import locking

MODEL_VERSION = "1.1"
KINDS = {"reading", "slides", "transcript", "summary", "syllabus", "assignment", "dataset", "other"}
GENERATIONS = {"original", "asr", "ai_generated", "unknown"}
VERIFICATIONS = {"unverified", "verified"}


class DataError(ValueError):
    """Invalid or inconsistent KB input, safe to display to the operator."""


def load_json(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise DataError(f"{path}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(DataError(f"Invalid JSON constant: {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DataError(f"{path}: {exc}") from exc


def resolve_root(value: str | Path | None = None) -> Path:
    root = Path(value or "~/course-kb").expanduser().resolve()
    if not root.is_dir():
        raise DataError(f"KB root not found: {root}")
    return root


def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DataError(f"Invalid relative path: {relative!r}")
    rel = Path(relative)
    if rel.is_absolute() or any(part in {"..", "."} for part in relative.split("/")):
        raise DataError(f"Path must stay inside the KB: {relative!r}")
    root = root.resolve()
    path = root / rel
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise DataError(f"Path escapes the KB: {relative!r}") from exc
    return path


def course_dirs(root: Path) -> list[tuple[str, Path]]:
    manifest = load_json(root / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("courses"), list):
        raise DataError("manifest.courses must be an array")
    result = []
    seen_codes, seen_dirs = set(), set()
    for entry in manifest["courses"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("code"), str):
            raise DataError("Every course needs a code and directory")
        directory = entry.get("directory")
        if not isinstance(directory, str) or len(Path(directory).parts) != 1 or directory.startswith(("_", ".")):
            raise DataError(f"Invalid course directory: {directory!r}")
        cdir = safe_path(root, directory)
        if not cdir.is_dir():
            raise DataError(f"Course directory missing: {directory}")
        if entry["code"] in seen_codes or directory in seen_dirs:
            raise DataError("Duplicate course code or directory")
        seen_codes.add(entry["code"])
        seen_dirs.add(directory)
        result.append((entry["code"], cdir))
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_id_for(code: str, filename: str) -> str:
    return "src-" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"course-kb/{code}/{filename}"))


def infer_source_metadata(source: dict, filename: str | None = None) -> dict:
    """Only explicit legacy evidence establishes an AI summary or ASR origin."""
    filename = filename or source.get("filename", "")
    kind = source.get("kind", source.get("type", "other"))
    note = " ".join(str(source.get(k) or "") for k in ("version_note", "original_filename", "title"))
    explicit_summary = bool(re.search(r"智能纪要|AI[- ]generated (?:summary|summaries)|AI\s*生成的摘要|不是逐字稿|非逐字稿", note, re.I))
    if kind == "summary" or explicit_summary:
        kind, generation = "summary", "ai_generated"
    else:
        if kind not in KINDS or kind == "other":
            if filename.startswith("syllabus"):
                kind = "syllabus"
            elif filename.lower().endswith((".xlsx", ".csv")):
                kind = "dataset"
            elif filename.startswith("assignment"):
                kind = "assignment"
            elif "-slides" in filename:
                kind = "slides"
            elif "-reading" in filename:
                kind = "reading"
            elif "-transcript" in filename:
                kind = "transcript"
            else:
                kind = "other"
        generation = "asr" if kind == "transcript" and re.search(r"自动转写|speech.to.text|ASR", note, re.I) else "unknown"
    return {"kind": kind, "generation_method": generation, "verification_status": "unverified"}


def source_info(course_dir: Path, source: dict) -> dict:
    info = {**source, **infer_source_metadata(source)}
    registry = safe_path(course_dir, "sources.json")
    if registry.exists():
        data = load_json(registry)
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise DataError(f"Invalid source registry: {registry}")
        matches = [s for s in data["sources"] if isinstance(s, dict) and
                   ((source.get("source_id") and s.get("source_id") == source["source_id"]) or
                    (not source.get("source_id") and s.get("filename") == source.get("filename")))]
        if len(matches) != 1:
            raise DataError(f"Source not uniquely registered: {source.get('filename')}")
        item = matches[0]
        if source.get("filename") and source["filename"] != item.get("filename"):
            raise DataError(f"Source ID and filename disagree: {source['filename']}")
        info.update(item)
        # Weekly detail is retained; registry owns provenance classification.
        for key in ("title", "author", "year", "edition", "version_note", "original_filename"):
            if info.get(key) is None and source.get(key) is not None:
                info[key] = source[key]
    info["type"] = info["kind"]
    return info


def assert_readable(root: Path) -> None:
    runtime = safe_path(root, ".course-kb")
    if (runtime / "write.lock").exists():
        raise DataError("KB has a write lock. Wait for the writer, or run recover after an interrupted update.")
    for path in sorted((runtime / "transactions").glob("*/journal.json")):
        journal = load_json(safe_path(root, path.relative_to(root).as_posix()))
        if not isinstance(journal, dict) or journal.get("state") not in {"committed", "rolled_back"}:
            raise DataError(f"Unfinished batch {path.parent.name}; run recover before reading the KB.")


def managed_files(root: Path) -> dict[str, str]:
    """Fingerprint actual inputs as well as generated metadata, excluding notes/history."""
    root = root.resolve()
    paths = [root / "manifest.json"]
    if (root / "_directory.md").exists():
        paths.append(root / "_directory.md")
    for _, cdir in course_dirs(root):
        paths += [p for p in (cdir / "course.json", cdir / "sources.json") if p.exists()]
        for sub in ("index/weekly", "index/concepts"):
            paths += sorted((cdir / sub).glob("*.json"))
        paths += sorted(p for p in (cdir / "raw").iterdir() if p.is_file() and not p.name.startswith("."))
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in paths}


def json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


@contextmanager
def read_snapshot(root: Path):
    """读取期间共享写入护锁；未初始化运行状态的库采用前后指纹核对。"""
    root = Path(root).resolve()
    guard = safe_path(root, ".course-kb/write.guard")
    try:
        stream = guard.open("rb")
    except FileNotFoundError:
        # 只读调用不创建运行目录；首次写入若在期间完成，guard 的出现也会使本次读取失效。
        assert_readable(root)
        before = managed_files(root)
        assert_readable(root)
        yield
        assert_readable(root)
        if guard.exists() or managed_files(root) != before:
            raise DataError("KB changed while being read; retry the export before publishing any output.")
        assert_readable(root)
        if guard.exists():
            raise DataError("KB changed while being read; retry the export before publishing any output.")
        return
    with stream:
        try:
            locking.lock_file(stream.fileno(), shared=True)
        except BlockingIOError as error:
            raise DataError("KB has an active writer lock; retry after the writer finishes.") from error
        try:
            assert_readable(root)
            yield
            assert_readable(root)
        finally:
            locking.unlock_file(stream.fileno())

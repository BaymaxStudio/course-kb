"""Prepare compatible metadata migrations and normalize staged index updates."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .model import (DataError, assert_readable, course_dirs, infer_source_metadata,
                    json_text, load_json, managed_files, safe_path, sha256_file,
                    source_id_for)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_text(data), encoding="utf-8")


def _allowed(relative: str) -> bool:
    parts = Path(relative).parts
    if relative in {"manifest.json", "_directory.md"}:
        return True
    if len(parts) == 2 and parts[1] in {"course.json", "sources.json"}:
        return not parts[0].startswith((".", "_"))
    if len(parts) == 3 and parts[1] == "raw":
        return not parts[0].startswith((".", "_")) and not parts[2].startswith(".")
    return (len(parts) == 4 and parts[1] == "index" and parts[2] in {"weekly", "concepts"}
            and parts[3].endswith(".json") and not parts[0].startswith((".", "_")))


def _overlay(root: Path, stage: Path, candidate: Path) -> None:
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise DataError(f"Staged symlinks are not allowed: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix()
        if relative == ".batch.json":
            continue
        if not _allowed(relative):
            raise DataError(f"Not a managed KB path: {relative}")
        original = safe_path(root, relative)
        if "raw" in Path(relative).parts and original.exists() and sha256_file(original) != sha256_file(path):
            raise DataError(f"raw is append-only: {relative}")
        target = safe_path(candidate, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _registry(cdir: Path, code: str, weeks: dict[str, dict]) -> dict[str, dict]:
    registry_path = cdir / "sources.json"
    existing = load_json(registry_path) if registry_path.exists() else {"schema_version": "1.1", "sources": []}
    if not isinstance(existing, dict) or not isinstance(existing.get("sources"), list):
        raise DataError(f"Invalid registry: {registry_path}")
    registry = {}
    for item in existing["sources"]:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise DataError(f"Invalid registry item: {registry_path}")
        if item["filename"] in registry:
            raise DataError(f"Duplicate registry filename: {item['filename']}")
        registry[item["filename"]] = item
    weekly_sources = {}
    for week in weeks.values():
        if not isinstance(week.get("sources"), list):
            raise DataError("weekly.sources must be an array")
        for source in week["sources"]:
            if not isinstance(source, dict) or not isinstance(source.get("filename"), str):
                raise DataError("Every weekly source needs a filename")
            weekly_sources.setdefault(source["filename"], source)
    for path in sorted((cdir / "raw").iterdir()):
        if path.name.startswith("."):
            continue
        if not path.is_file() or path.is_symlink():
            raise DataError(f"raw must contain regular files: {path}")
        digest = sha256_file(path)
        if path.name in registry:
            if registry[path.name].get("sha256") != digest:
                raise DataError(f"Registered raw checksum changed: {path}")
            continue
        source = weekly_sources.get(path.name, {"filename": path.name})
        registry[path.name] = {
            "source_id": source_id_for(code, path.name), "filename": path.name,
            **infer_source_metadata(source, path.name), "sha256": digest,
            "original_filename": source.get("original_filename"),
            "version_note": source.get("version_note"), "derived_from": [],
        }
    for filename in registry:
        if not safe_path(cdir / "raw", filename).is_file():
            raise DataError(f"Registered source file missing: {cdir.name}/{filename}")
    for filename, source in weekly_sources.items():
        if filename not in registry:
            raise DataError(f"Weekly source file missing: {cdir.name}/{filename}")
    _write(registry_path, {**existing, "schema_version": "1.1", "sources": list(registry.values())})
    return registry


def normalize_course(cdir: Path, code: str, legacy: bool) -> dict:
    weeks = {p.stem: load_json(p) for p in sorted((cdir / "index/weekly").glob("*.json"))}
    cards = {p.stem: load_json(p) for p in sorted((cdir / "index/concepts").glob("*.json"))}
    if not all(isinstance(value, dict) for value in [*weeks.values(), *cards.values()]):
        raise DataError(f"Week and concept records must be objects: {code}")
    before_weeks = copy.deepcopy(weeks)
    registry = _registry(cdir, code, weeks)
    by_id = {item["source_id"]: item for item in registry.values()}
    inferred_relations = 0
    unverified_definitions = 0

    for week in weeks.values():
        for source in week["sources"]:
            item = registry[source["filename"]]
            if source.get("source_id") and source["source_id"] != item["source_id"]:
                raise DataError(f"Source ID conflicts with filename: {source['filename']}")
            source["source_id"] = item["source_id"]
            source["type"] = item["kind"]

    relations = set()
    for slug, card in cards.items():
        if not isinstance(card.get("appearances"), list):
            raise DataError(f"appearances must be an array: {code}/{slug}")
        card.setdefault("quotes", [])
        if legacy and card.get("definition_basis") == "verbatim":
            card["legacy_definition_basis"] = "verbatim"
            card["definition_basis"] = "unverified"
            unverified_definitions += 1
        for appearance in card["appearances"]:
            if not isinstance(appearance, dict):
                raise DataError(f"Invalid appearance: {code}/{slug}")
            filename = appearance.get("source_file")
            if filename not in registry:
                raise DataError(f"Appearance source missing: {code}/{slug}: {filename}")
            item = registry[filename]
            if appearance.get("source_id") and appearance["source_id"] != item["source_id"]:
                raise DataError(f"Appearance ID conflicts with source file: {code}/{slug}")
            appearance["source_id"] = item["source_id"]
            appearance.setdefault("locator", None)
            appearance.setdefault("verification_status", "unverified")
            appearance.setdefault("assertion_basis", "legacy_appearance" if legacy else "extracted")
            relations.add((appearance.get("week"), item["source_id"], slug))

    # Legacy source indexes are retained as assertions, not invented explanations.
    if legacy:
        for week in weeks.values():
            for source in week["sources"]:
                for slug in source.get("key_concepts", []):
                    if slug not in cards:
                        raise DataError(f"Source references missing concept card: {code}/{slug}")
                    key = (week["week"], source["source_id"], slug)
                    if key in relations:
                        continue
                    cards[slug]["appearances"].append({
                        "week": week["week"], "source_file": source["filename"],
                        "source_id": source["source_id"], "context": None, "locator": None,
                        "verification_status": "unverified", "assertion_basis": "legacy_source_index",
                    })
                    relations.add(key)
                    inferred_relations += 1

    for number, source_id, slug in relations:
        if not isinstance(number, int) or isinstance(number, bool):
            raise DataError(f"Invalid appearance week: {code}/{slug}")
        week = weeks.get(f"w{number:02d}")
        if not week or source_id not in {s["source_id"] for s in week["sources"]}:
            raise DataError(f"Appearance is not in that week's sources: {code}/{slug}/w{number:02d}")
        if source_id not in by_id:
            raise DataError(f"Unknown source ID: {source_id}")

    referenced_files = {source["filename"] for week in weeks.values() for source in week["sources"]}
    for key, week in weeks.items():
        number = week["week"]
        derived = {slug for wn, _, slug in relations if wn == number}
        unresolved = set(week.get("unresolved_concepts", []))
        if legacy:
            unresolved.update(set(week.get("key_concepts", [])) - derived)
        unresolved -= derived
        week["unresolved_concepts"] = sorted(unresolved)
        week["key_concepts"] = sorted(derived | unresolved)
        for source in week["sources"]:
            source["key_concepts"] = sorted(slug for wn, sid, slug in relations
                                           if wn == number and sid == source["source_id"])
        unindexed = [filename for filename in registry if filename.startswith(key + "-") and filename not in referenced_files]
        week["indexing_status"] = "partial" if unindexed else "complete"
        week.setdefault("material_availability", {"status": "unknown", "missing": []})
        if week != before_weeks[key]:
            week["last_updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
        _write(cdir / "index/weekly" / f"{key}.json", week)
    for slug, card in cards.items():
        _write(cdir / "index/concepts" / f"{slug}.json", card)
    course = load_json(cdir / "course.json")
    course["weeks_indexed"] = len(weeks)
    _write(cdir / "course.json", course)
    return {"course": code, "sources": len(registry), "concepts": len(cards),
            "legacy_relations_preserved": inferred_relations,
            "definitions_awaiting_quote_verification": unverified_definitions}


def prepare_stage(root: Path, stage: Path, migrate: bool = False) -> dict:
    root, stage = root.resolve(), stage.expanduser().resolve()
    assert_readable(root)
    if stage == root or root in stage.parents or stage in root.parents:
        raise DataError("Stage must be outside the KB and must not contain the KB")
    baseline = managed_files(root)
    if migrate and stage.exists() and any(stage.iterdir()):
        raise DataError("Migration stage must be empty")
    stage.mkdir(parents=True, exist_ok=True)
    old_plan = stage / ".batch.json"
    if old_plan.exists():
        planned = load_json(old_plan).get("base_hashes", {})
        if any(baseline.get(path) != digest for path, digest in planned.items()):
            raise DataError("Stage was prepared against a different KB state; preserve it and prepare a new stage")
    with tempfile.TemporaryDirectory(prefix="course-kb-candidate-") as temporary:
        candidate = Path(temporary) / "kb"
        shutil.copytree(root, candidate, ignore=shutil.ignore_patterns("notes", ".course-kb", ".DS_Store"))
        _overlay(root, stage, candidate)
        manifest = load_json(candidate / "manifest.json")
        version = manifest.get("kb_version")
        if version not in {"1.0", "1.1"}:
            raise DataError(f"Unsupported kb_version: {version}")
        if version == "1.0" and not migrate:
            raise DataError("Run migrate first for a 1.0 knowledge base")
        summaries = [normalize_course(cdir, code, legacy=version == "1.0") for code, cdir in course_dirs(candidate)]
        manifest["kb_version"] = "1.1"
        _write(candidate / "manifest.json", manifest)
        changed_before_directory = {path for path, digest in managed_files(candidate).items()
                                    if path != "_directory.md" and baseline.get(path) != digest}
        if changed_before_directory:
            manifest["last_updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
            _write(candidate / "manifest.json", manifest)
            from gen_directory import render
            (candidate / "_directory.md").write_text(render(candidate, truncate=True), encoding="utf-8")
        from .validation import validate
        report = validate(candidate, check_runtime=False)
        if report.errors:
            raise DataError("Candidate validation failed:\n" + "\n".join(f"{rule}: {detail}" for rule, detail in report.errors))
        changed = []
        for relative, digest in managed_files(candidate).items():
            if baseline.get(relative) == digest:
                continue
            changed.append(relative)
            dest = safe_path(stage, relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate / relative, dest)
        base_hashes = dict(baseline)
        for path in changed:
            base_hashes.setdefault(path, None)
        plan = {"format": "course-kb-batch-1", "base_hashes": base_hashes,
                "root_fingerprint": hashlib.sha256(json.dumps(base_hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "summary": {"changed_files": changed, "courses": summaries, "warnings": report.warnings}}
        _write(stage / ".batch.json", plan)
        return plan["summary"]

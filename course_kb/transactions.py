"""校验候选库后批量发布，并保留可恢复的逐文件事务记录。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Iterator
import uuid

from .model import DataError, load_json, resolve_root, safe_path, sha256_file
from . import locking

_PENDING = {"prepared", "applying", "recovery_required"}
_STATES = _PENDING | {"committed", "rolled_back"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise DataError(f"Invalid relative path: {relative!r}")
    parts = Path(relative).parts
    if Path(relative).is_absolute() or any(p in {".", ".."} for p in parts):
        raise DataError(f"Unsafe relative path: {relative!r}")
    # Path normalisation must not silently accept traversal-like spellings.
    if relative != "/".join(parts):
        raise DataError(f"Non-canonical relative path: {relative!r}")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise DataError(f"Symlinks are not managed: {relative}")
    return safe_path(root, relative)


def _runtime(root: Path) -> Path:
    directory = _path(root, ".course-kb")
    directory.mkdir(exist_ok=True)
    if not directory.is_dir():
        raise DataError(".course-kb must be a directory")
    return directory


def _fsync_directory(path: Path) -> None:
    if locking.IS_WINDOWS:
        # Windows 标准库不能打开目录 fd 做 fsync；文件内容在发布前已 fsync。
        # 这里保证进程中断恢复，不宣称 Windows 具有 Unix 目录刷盘语义。
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_new_file(source: str | Path, destination: Path) -> None:
    if locking.IS_WINDOWS:
        # Windows rename 对既存目标必定失败；也适用于不支持硬链接的文件系统。
        os.rename(source, destination)
    else:
        os.link(source, destination)


def _atomic_bytes(path: Path, content: bytes, temporary_directory: Path | None = None,
                  exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = temporary_directory or path.parent
    descriptor, temporary = tempfile.mkstemp(prefix=".batch-", dir=temporary_directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # 原件采用原子“不存在才创建”，避免检查后出现的同名文件被覆盖。
            _publish_new_file(temporary, path)
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)  # Only this operation's temporary file.


def _write_json(path: Path, value: dict) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _install_file(source: Path, destination: Path) -> None:
    """单独保留发布步骤，便于测试实际替换之间的进程中断。"""
    in_transaction = any(parent.parent.name == "transactions" and
                         parent.parent.parent.name == ".course-kb"
                         for parent in source.parents)
    # 发布与恢复的临时副本留在事务目录；进程中断不能把半成品留进 raw/。
    temporary_directory = source.parent if in_transaction else None
    exclusive = in_transaction and destination.parent.name == "raw"
    _atomic_bytes(destination, source.read_bytes(), temporary_directory, exclusive)


def _pid_alive(pid: object) -> bool:
    try:
        return locking.process_alive(pid)
    except ValueError as error:
        raise DataError(str(error)) from error


@contextlib.contextmanager
def _writer_lock(root: Path, token: str, recovery: bool = False) -> Iterator[Path]:
    _runtime(root)
    guard_path = _path(root, ".course-kb/write.guard")
    lock_path = _path(root, ".course-kb/write.lock")
    descriptor = os.open(guard_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        try:
            locking.lock_file(descriptor)
            locked = True
        except BlockingIOError as error:
            raise DataError("Another batch operation holds the writer lock") from error
        if lock_path.exists():
            previous = load_json(lock_path)
            if not isinstance(previous, dict):
                raise DataError("Invalid write.lock; recovery requires a valid journal")
            pid = previous.get("pid")
            alive = _pid_alive(pid)
            if not recovery:
                raise DataError("Writer lock exists; run recover before applying another batch")
            if alive and pid != os.getpid():
                raise DataError(f"Writer process {pid} is still alive; recovery refused")
        _write_json(lock_path, {"pid": os.getpid(), "token": token})
        yield lock_path
    finally:
        # Success paths remove write.lock themselves; failed commits retain it.
        # OS locks also distinguish active same-process calls from failed ones.
        try:
            if locked:
                locking.unlock_file(descriptor)
        finally:
            os.close(descriptor)


def _release_lock(root: Path, token: str) -> None:
    path = _path(root, ".course-kb/write.lock")
    if path.exists() and load_json(path).get("token") == token:
        path.unlink()  # The lock is runtime state, never user material.
        _fsync_directory(path.parent)


def _course_names(manifest: object) -> set[str]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("courses"), list):
        raise DataError("Manifest must contain courses")
    names = set()
    for entry in manifest["courses"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("directory"), str):
            raise DataError("Invalid course directory in manifest")
        name = entry["directory"]
        if not name or len(Path(name).parts) != 1 or name in {".", "..", ".course-kb", "notes"} or "\\" in name:
            raise DataError(f"Invalid course directory: {name!r}")
        names.add(name)
    return names


def _permitted(relative: str, courses: set[str]) -> bool:
    parts = Path(relative).parts
    if relative in {"manifest.json", "_directory.md"}:
        return True
    if len(parts) < 2 or parts[0] not in courses or "notes" in parts:
        return False
    if len(parts) == 2 and parts[1] in {"course.json", "sources.json"}:
        return True
    if len(parts) == 3 and parts[1] == "raw" and not parts[2].startswith("."):
        return True
    return len(parts) == 4 and parts[1] == "index" and parts[2] in {"weekly", "concepts"} and parts[3].endswith(".json") and not parts[3].startswith(".")


def _is_raw(relative: str) -> bool:
    return len(Path(relative).parts) == 3 and Path(relative).parts[1] == "raw"


def _file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise DataError(f"Expected a regular file: {path}")
    return sha256_file(path)


def _stage_files(stage: Path, courses: set[str]) -> list[str]:
    result = []
    for directory, children, files in os.walk(stage, followlinks=False):
        for name in children + files:
            path = Path(directory) / name
            relative = path.relative_to(stage).as_posix()
            _path(stage, relative)
            if "notes" in Path(relative).parts:
                raise DataError("Batch stages may not contain notes")
            if path.is_symlink():
                raise DataError(f"Stage contains a symlink: {relative}")
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(stage).as_posix()
            if relative == ".batch.json":
                continue
            if not _permitted(relative, courses) or not path.is_file():
                raise DataError(f"Unmanaged stage path: {relative}")
            result.append(relative)
    return sorted(result)


def _journals(root: Path) -> list[tuple[Path, dict]]:
    directory = _path(root, ".course-kb/transactions")
    if not directory.exists():
        return []
    result = []
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            raise DataError(f"Invalid transaction directory: {entry}")
        journal_path = _path(root, f".course-kb/transactions/{entry.name}/journal.json")
        if not journal_path.exists():
            continue  # Interrupted backup preparation; no live changes yet.
        journal = load_json(journal_path)
        if not isinstance(journal, dict) or journal.get("state") not in _STATES:
            raise DataError(f"Invalid transaction journal: {journal_path}")
        result.append((journal_path, journal))
    return result


def _assert_baseline(root: Path, baseline: dict[str, str | None]) -> None:
    for relative, expected in baseline.items():
        if _file_hash(_path(root, relative)) != expected:
            raise DataError(f"Stale stage: baseline changed for {relative}")


def _check_baseline(root: Path, stage: Path, changes: list[dict], courses: set[str]) -> dict[str, str | None]:
    path = stage / ".batch.json"
    metadata = load_json(path) if path.exists() else {}
    if not isinstance(metadata, dict):
        raise DataError(".batch.json must be an object")
    baseline = metadata.get("base_hashes", {})
    if not isinstance(baseline, dict):
        raise DataError("base_hashes must be an object")
    for relative, expected in baseline.items():
        _path(root, relative)
        if not _permitted(relative, courses):
            raise DataError(f"Unmanaged baseline path: {relative}")
        if expected is not None and (not isinstance(expected, str) or not _HASH.fullmatch(expected)):
            raise DataError(f"Invalid baseline hash: {relative}")
    fingerprint = metadata.get("root_fingerprint")
    if fingerprint is not None:
        expected = hashlib.sha256(json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != expected:
            raise DataError("Stage root_fingerprint does not match base_hashes")
    for change in changes:
        if change["before_sha256"] is not None and change["path"] not in baseline:
            raise DataError(f"Existing metadata requires a base hash: {change['path']}")
    _assert_baseline(root, baseline)
    return baseline


def apply_batch(root: Path | str, stage: Path | str, validator: Callable | None = None) -> dict:
    """发布外部 stage；只有新文件或完全相同内容可省略 baseline。"""
    root = resolve_root(root)
    stage_path = Path(stage).expanduser()
    if stage_path.is_symlink():
        raise DataError("Stage root cannot be a symlink")
    stage = stage_path.resolve()
    if not stage.is_dir() or stage == root or root in stage.parents or stage in root.parents:
        raise DataError("Stage must be an existing directory outside the live root")
    token = uuid.uuid4().hex
    journal_path = None
    with _writer_lock(root, token):
        try:
            if any(journal["state"] in _PENDING for _, journal in _journals(root)):
                raise DataError("Pending transaction recovery; run recover first")
            old_courses = _course_names(load_json(_path(root, "manifest.json")))
            candidate_manifest = _path(stage, "manifest.json")
            courses = _course_names(load_json(candidate_manifest)) if candidate_manifest.exists() else old_courses
            relatives = _stage_files(stage, courses)
            changes = []
            for relative in relatives:
                target = _path(root, relative)
                before = _file_hash(target)
                after = sha256_file(_path(stage, relative))
                if before == after:
                    continue
                if _is_raw(relative) and before is not None:
                    raise DataError(f"Existing raw files are immutable: {relative}")
                changes.append({"path": relative, "before_sha256": before, "after_sha256": after})
            if not changes:
                _release_lock(root, token)
                return {"status": "no-op", "changed": [], "transaction_id": None}
            baseline = _check_baseline(root, stage, changes, old_courses | courses)
            with tempfile.TemporaryDirectory(prefix="course-kb-candidate-") as temporary:
                candidate = Path(temporary) / "kb"
                shutil.copytree(root, candidate, symlinks=True, ignore=shutil.ignore_patterns("notes", ".course-kb", ".git"))
                for change in changes:
                    destination = _path(candidate, change["path"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(_path(stage, change["path"]), destination)
                    if sha256_file(destination) != change["after_sha256"]:
                        raise DataError(f"Stage changed while preparing candidate: {change['path']}")
                if validator is None:
                    from .validation import validate
                    report = validate(candidate, check_runtime=False)
                else:
                    report = validator(candidate)
                errors = getattr(report, "errors", [])
                if errors:
                    raise DataError(f"Candidate validation failed: {errors}")
                _assert_baseline(root, baseline)
                # Validate before creating journal or replacing any managed live file.
                transaction = _path(root, f".course-kb/transactions/{token}")
                transaction.mkdir(parents=True)
                for change in changes:
                    relative = change["path"]
                    if change["before_sha256"] is not None:
                        backup = _path(transaction, f"backups/{relative}")
                        _install_file(_path(root, relative), backup)
                        if sha256_file(backup) != change["before_sha256"]:
                            raise DataError(f"Live file changed during backup: {relative}")
                    pending = _path(transaction, f"pending/{relative}")
                    _install_file(_path(candidate, relative), pending)
                journal = {"version": 1, "transaction_id": token, "state": "prepared", "entries": changes, "applied": [], "retained_raw": [], "courses": sorted(old_courses | courses)}
                journal_path = transaction / "journal.json"
                _write_json(journal_path, journal)
            _assert_baseline(root, baseline)
            journal["state"] = "applying"
            _write_json(journal_path, journal)
            for change in changes:
                relative = change["path"]
                target = _path(root, relative)
                if _file_hash(target) != change["before_sha256"]:
                    raise DataError(f"Live file changed before replacement: {relative}")
                _install_file(_path(transaction, f"pending/{relative}"), target)
                journal["applied"].append(relative)
                _write_json(journal_path, journal)
            final_hashes = {**baseline, **{entry["path"]: entry["after_sha256"] for entry in changes}}
            _assert_baseline(root, final_hashes)
            journal["state"] = "committed"
            _write_json(journal_path, journal)
            _release_lock(root, token)
            return {"status": "committed", "transaction_id": token, "changed": [entry["path"] for entry in changes]}
        except BaseException:
            if journal_path is not None:
                journal["state"] = "recovery_required"
                _write_json(journal_path, journal)
            else:
                _release_lock(root, token)
            raise


def _rollback(root: Path, journal_path: Path, journal: dict) -> list[str]:
    transaction = journal_path.parent
    courses = set(journal.get("courses", []))
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise DataError(f"Invalid entries: {journal_path}")
    retained = list(journal.get("retained_raw", []))
    plans = []
    for entry in entries:
        if not isinstance(entry, dict) or not _permitted(entry.get("path", ""), courses):
            raise DataError(f"Invalid journal entry: {journal_path}")
        relative = entry["path"]
        target = _path(root, relative)
        before, after = entry.get("before_sha256"), entry.get("after_sha256")
        if (before is not None and (not isinstance(before, str) or not _HASH.fullmatch(before))) or not isinstance(after, str) or not _HASH.fullmatch(after):
            raise DataError(f"Invalid journal checksum: {relative}")
        current = _file_hash(target)
        if before == current:
            continue
        if before is None and _is_raw(relative) and current is not None:
            retained.append(relative)
            continue
        if current != after:
            raise DataError(f"Recovery conflict; preserve changed user file: {relative}")
        if before is not None:
            if _is_raw(relative):
                raise DataError(f"Journal cannot overwrite existing raw: {relative}")
            backup = _path(transaction, f"backups/{relative}")
            if _file_hash(backup) != before:
                raise DataError(f"Recovery backup is missing or changed: {relative}")
            plans.append((relative, "restore", backup, after, before))
        else:
            archive = _path(transaction, f"archived_new/{relative}")
            if archive.exists():
                raise DataError(f"Recovery archive already exists: {relative}")
            plans.append((relative, "archive", archive, after, None))
    journal["state"] = "recovery_required"
    _write_json(journal_path, journal)
    for relative, operation, other, expected, original in plans:
        target = _path(root, relative)
        if _file_hash(target) != expected:
            raise DataError(f"Recovery conflict; preserve changed user file: {relative}")
        if operation == "restore":
            if _file_hash(other) != original:
                raise DataError(f"Recovery backup is missing or changed: {relative}")
            _install_file(other, target)
        else:
            other.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, other)
            _fsync_directory(target.parent)
            _fsync_directory(other.parent)
    journal["retained_raw"] = sorted(set(retained))
    journal["state"] = "rolled_back"
    _write_json(journal_path, journal)
    return journal["retained_raw"]


def recover(root: Path | str) -> dict:
    """回滚未完成的 metadata 发布；新增原件留在 raw/ 原路径。"""
    root = resolve_root(root)
    token = uuid.uuid4().hex
    with _writer_lock(root, token, recovery=True):
        recovered = []
        retained = []
        for journal_path, journal in _journals(root):
            if journal["state"] in _PENDING:
                retained.extend(_rollback(root, journal_path, journal))
                recovered.append(journal.get("transaction_id", journal_path.parent.name))
        _release_lock(root, token)
        return {"status": "recovered" if recovered else "no-op", "transactions": recovered, "retained_raw": sorted(set(retained))}

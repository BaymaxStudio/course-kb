"""事务测试在临时库执行，不读取或改动用户的真实课程库。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from platform_helpers import symlink_or_skip

from course_kb.model import DataError, assert_readable
from course_kb import transactions as tx


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def valid(_root):
    return SimpleNamespace(errors=[], warnings=[])


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-kb-tx-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "live"
        self.root.mkdir()
        self.stage = self.directory / "stage"
        self.stage.mkdir()
        self.manifest = {"kb_version": "1.0", "courses": [{"code": "TEST1000", "name": "Test", "directory": "TEST1000"}]}
        write_json(self.root / "manifest.json", self.manifest)
        write_json(self.root / "TEST1000/course.json", {"code": "TEST1000", "name": "before"})
        (self.root / "TEST1000/raw").mkdir()
        (self.root / "TEST1000/raw/original.pdf").write_bytes(b"original raw bytes")
        (self.root / "TEST1000/notes").mkdir()
        (self.root / "TEST1000/notes/private.md").write_text("private user notes")

    def stage_change(self, relative="TEST1000/course.json", value=None):
        if value is None:
            value = {"code": "TEST1000", "name": "after"}
        write_json(self.stage / relative, value)
        self.baseline()

    def baseline(self):
        hashes = {}
        for p in self.root.rglob("*"):
            relative = p.relative_to(self.root).as_posix()
            if p.is_file() and tx._permitted(relative, {"TEST1000"}):
                hashes[relative] = digest(p)
        for p in self.stage.rglob("*"):
            if p.is_file() and p.name != ".batch.json":
                relative = p.relative_to(self.stage).as_posix()
                hashes.setdefault(relative, digest(self.root / relative))
        fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        write_json(self.stage / ".batch.json", {"base_hashes": hashes, "root_fingerprint": fingerprint})

    def journal(self):
        paths = list((self.root / ".course-kb/transactions").glob("*/journal.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text())

    def test_commit_and_idempotent_repeat_preserve_originals(self):
        self.stage_change()
        result = tx.apply_batch(self.root, self.stage, valid)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "after")
        self.assertEqual(tx.apply_batch(self.root, self.stage, valid)["status"], "no-op")
        self.assertEqual((self.root / "TEST1000/raw/original.pdf").read_bytes(), b"original raw bytes")
        self.assertEqual((self.root / "TEST1000/notes/private.md").read_text(), "private user notes")
        self.assertEqual(self.journal()["state"], "committed")
        self.assertFalse((self.root / ".course-kb/write.lock").exists())
        backup = self.root / ".course-kb/transactions" / result["transaction_id"] / "backups/TEST1000/course.json"
        self.assertEqual(json.loads(backup.read_text())["name"], "before")

    def test_stale_stage_rejects_unrelated_baseline_change(self):
        self.stage_change()
        write_json(self.root / "manifest.json", {**self.manifest, "changed_after_prepare": True})
        with self.assertRaisesRegex(DataError, "Stale stage"):
            tx.apply_batch(self.root, self.stage, valid)
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "before")
        self.assertFalse((self.root / ".course-kb/write.lock").exists())

    def test_existing_metadata_without_baseline_is_rejected(self):
        write_json(self.stage / "TEST1000/course.json", {"code": "TEST1000", "name": "after"})
        with self.assertRaisesRegex(DataError, "requires a base hash"):
            tx.apply_batch(self.root, self.stage, valid)

    def test_windows_publish_uses_nonreplacing_rename_not_hardlink(self):
        source, destination = self.directory / "temporary", self.directory / "published"
        source.write_bytes(b"original")
        with patch.object(tx.locking, "IS_WINDOWS", True), patch.object(tx.os, "rename") as rename, patch.object(tx.os, "link", side_effect=AssertionError("Windows publication must not require hard links")):
            tx._publish_new_file(source, destination)
            rename.assert_called_once_with(source, destination)
            rename.side_effect = FileExistsError("existing original")
            with self.assertRaises(FileExistsError):
                tx._publish_new_file(source, destination)
        self.assertEqual(source.read_bytes(), b"original")

    def test_windows_directory_sync_does_not_open_directory_descriptor(self):
        with patch.object(tx.locking, "IS_WINDOWS", True), patch.object(tx.os, "open", side_effect=AssertionError("Windows does not support directory fd fsync")):
            tx._fsync_directory(self.root)

    def test_failed_kernel_lock_does_not_attempt_unlock(self):
        self.stage_change()
        with patch.object(tx.locking, "lock_file", side_effect=BlockingIOError("busy")), patch.object(tx.locking, "unlock_file") as unlock:
            with self.assertRaisesRegex(DataError, "holds the writer lock"):
                tx.apply_batch(self.root, self.stage, valid)
        unlock.assert_not_called()

    def test_raw_overwrite_rejected_and_identical_raw_is_noop(self):
        destination = self.stage / "TEST1000/raw/original.pdf"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"changed")
        self.baseline()
        with self.assertRaisesRegex(DataError, "raw files are immutable"):
            tx.apply_batch(self.root, self.stage, valid)
        destination.write_bytes(b"original raw bytes")
        self.assertEqual(tx.apply_batch(self.root, self.stage, valid)["status"], "no-op")

    def test_baseline_change_during_validation_is_rejected_before_publication(self):
        self.stage_change()
        def change_dependency(candidate):
            write_json(self.root / "manifest.json", {**self.manifest, "edited_during_validation": True})
            # 重写外部计划不能替换事务开始时读到的 baseline。
            self.baseline()
            return valid(candidate)
        with self.assertRaisesRegex(DataError, "Stale stage"):
            tx.apply_batch(self.root, self.stage, change_dependency)
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "before")
        self.assertTrue(json.loads((self.root / "manifest.json").read_text())["edited_during_validation"])
        self.assertFalse((self.root / ".course-kb/write.lock").exists())

    def test_dependency_change_during_publication_requires_recovery(self):
        self.stage_change()
        original = tx._install_file
        def edit_dependency(source, destination):
            original(source, destination)
            if destination == self.root / "TEST1000/course.json":
                write_json(self.root / "manifest.json", {**self.manifest, "external_edit": True})
        with patch.object(tx, "_install_file", edit_dependency):
            with self.assertRaisesRegex(DataError, "Stale stage"):
                tx.apply_batch(self.root, self.stage, valid)
        with self.assertRaises(DataError):
            assert_readable(self.root)
        self.assertEqual(tx.recover(self.root)["status"], "recovered")
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "before")
        self.assertTrue(json.loads((self.root / "manifest.json").read_text())["external_edit"])
        assert_readable(self.root)

    def test_raw_added_after_absence_check_is_never_overwritten(self):
        destination = self.stage / "TEST1000/raw/new.pdf"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"staged raw")
        self.baseline()
        original = tx._publish_new_file
        def external_raw(source, target):
            if target == self.root / "TEST1000/raw/new.pdf":
                target.write_bytes(b"raw added by another tool")
            original(source, target)
        with patch.object(tx, "_publish_new_file", external_raw):
            with self.assertRaises(FileExistsError):
                tx.apply_batch(self.root, self.stage, valid)
        self.assertEqual((self.root / "TEST1000/raw/new.pdf").read_bytes(), b"raw added by another tool")
        result = tx.recover(self.root)
        self.assertEqual(result["retained_raw"], ["TEST1000/raw/new.pdf"])
        self.assertEqual((self.root / "TEST1000/raw/new.pdf").read_bytes(), b"raw added by another tool")

    def test_notes_paths_are_rejected(self):
        notes = self.stage / "TEST1000/notes"
        notes.mkdir(parents=True)
        with self.assertRaisesRegex(DataError, "notes"):
            tx.apply_batch(self.root, self.stage, valid)

    def test_symlink_paths_are_rejected(self):
        (self.stage / "TEST1000").mkdir()
        symlink_or_skip(self, self.stage / "TEST1000/course.json", self.root / "TEST1000/course.json")
        with self.assertRaisesRegex(DataError, "[Ss]ymlink"):
            tx.apply_batch(self.root, self.stage, valid)

    def test_traversal_baseline_is_rejected(self):
        self.stage_change()
        metadata = json.loads((self.stage / ".batch.json").read_text())
        metadata["base_hashes"]["../outside.json"] = None
        write_json(self.stage / ".batch.json", metadata)
        with self.assertRaises(DataError):
            tx.apply_batch(self.root, self.stage, valid)

    def test_candidate_validation_precedes_live_mutation(self):
        self.stage_change()
        before = (self.root / "TEST1000/course.json").read_bytes()
        def reject(candidate):
            self.assertNotEqual(candidate, self.root)
            self.assertEqual(json.loads((candidate / "TEST1000/course.json").read_text())["name"], "after")
            self.assertEqual((self.root / "TEST1000/course.json").read_bytes(), before)
            self.assertFalse((candidate / "TEST1000/notes").exists())
            return SimpleNamespace(errors=[("fixture", "invalid candidate")])
        with self.assertRaisesRegex(DataError, "Candidate validation failed"):
            tx.apply_batch(self.root, self.stage, reject)
        self.assertEqual((self.root / "TEST1000/course.json").read_bytes(), before)
        self.assertFalse((self.root / ".course-kb/write.lock").exists())
        self.assertFalse((self.root / ".course-kb/transactions").exists())

    def test_same_process_lock_contention(self):
        self.stage_change()
        def competing(candidate):
            with self.assertRaisesRegex(DataError, "holds the writer lock"):
                tx.apply_batch(self.root, self.stage, valid)
            with self.assertRaisesRegex(DataError, "holds the writer lock"):
                tx.recover(self.root)
            return valid(candidate)
        self.assertEqual(tx.apply_batch(self.root, self.stage, competing)["status"], "committed")

    def test_failed_commit_recovers_same_process_and_retains_new_raw(self):
        self.stage_change()
        write_json(self.stage / "manifest.json", {**self.manifest, "updated": True})
        write_json(self.stage / "TEST1000/sources.json", {"sources": []})
        raw = self.stage / "TEST1000/raw/new.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"new append-only original")
        self.baseline()
        original = tx._install_file
        def failing(source, destination):
            if destination == self.root / "manifest.json":
                raise OSError("injected write failure")
            original(source, destination)
        with patch.object(tx, "_install_file", failing):
            with self.assertRaisesRegex(OSError, "injected"):
                tx.apply_batch(self.root, self.stage, valid)
        self.assertEqual(self.journal()["state"], "recovery_required")
        self.assertTrue((self.root / ".course-kb/write.lock").exists())
        with self.assertRaises(DataError):
            tx.apply_batch(self.root, self.stage, valid)
        result = tx.recover(self.root)
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["retained_raw"], ["TEST1000/raw/new.pdf"])
        self.assertEqual((self.root / "TEST1000/raw/new.pdf").read_bytes(), b"new append-only original")
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "before")
        self.assertFalse((self.root / "TEST1000/sources.json").exists())
        archived = list((self.root / ".course-kb/transactions").glob("*/archived_new/TEST1000/sources.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(self.journal()["state"], "rolled_back")
        self.assertEqual(tx.recover(self.root)["status"], "no-op")

    def interrupt_commit(self):
        self.stage_change()
        code = '''import os, sys
from pathlib import Path
from types import SimpleNamespace
from course_kb import transactions as tx
root, stage = map(Path, sys.argv[1:])
original = tx._install_file
def crash(source, destination):
    original(source, destination)
    if destination == root / "TEST1000/course.json":
        os._exit(73)
tx._install_file = crash
tx.apply_batch(root, stage, lambda candidate: SimpleNamespace(errors=[]))
'''
        process = subprocess.run([sys.executable, "-c", code, str(self.root), str(self.stage)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 73, process.stderr)

    def test_process_interruption_after_replace_before_journal_update(self):
        self.interrupt_commit()
        self.assertEqual(self.journal()["state"], "applying")
        self.assertEqual(self.journal()["applied"], [])
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "after")
        self.assertEqual(tx.recover(self.root)["status"], "recovered")
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "before")

    def test_recovery_preserves_later_user_edits_and_reports_conflict(self):
        self.interrupt_commit()
        write_json(self.root / "TEST1000/course.json", {"name": "later user edit"})
        with self.assertRaisesRegex(DataError, "Recovery conflict"):
            tx.recover(self.root)
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "later user edit")
        self.assertTrue((self.root / ".course-kb/write.lock").exists())

    def test_recovery_rechecks_user_edits_after_planning(self):
        self.interrupt_commit()
        original = tx._write_json
        def edit_after_plan(path, value):
            original(path, value)
            if path.name == "journal.json" and value.get("state") == "recovery_required":
                write_json(self.root / "TEST1000/course.json", {"name": "edited during recovery"})
        with patch.object(tx, "_write_json", edit_after_plan):
            with self.assertRaisesRegex(DataError, "Recovery conflict"):
                tx.recover(self.root)
        self.assertEqual(json.loads((self.root / "TEST1000/course.json").read_text())["name"], "edited during recovery")

    def test_recovery_refuses_other_live_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        self.addCleanup(process.stdin.close)
        write_json(self.root / ".course-kb/write.lock", {"pid": process.pid, "token": "another-process"})
        with self.assertRaisesRegex(DataError, "still alive"):
            tx.recover(self.root)
        self.assertEqual(json.loads((self.root / ".course-kb/write.lock").read_text())["pid"], process.pid)

    def test_interruption_before_raw_publication_leaves_no_partial_original(self):
        raw = self.stage / "TEST1000/raw/new.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"complete staged original")
        self.baseline()
        code = '''import os, sys
from pathlib import Path
from types import SimpleNamespace
from course_kb import transactions as tx
root, stage = map(Path, sys.argv[1:])
def crash_before_link(source, target):
    assert Path(source).parent != root / "TEST1000/raw"
    assert Path(source).read_bytes() == b"complete staged original"
    os._exit(74)
tx._publish_new_file = crash_before_link
tx.apply_batch(root, stage, lambda candidate: SimpleNamespace(errors=[]))
'''
        process = subprocess.run([sys.executable, "-c", code, str(self.root), str(self.stage)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(process.returncode, 74, process.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "TEST1000/raw").iterdir()), ["original.pdf"])
        self.assertEqual(tx.recover(self.root)["retained_raw"], [])
        self.assertEqual(sorted(p.name for p in (self.root / "TEST1000/raw").iterdir()), ["original.pdf"])


if __name__ == "__main__":
    unittest.main()

"""Migration preserves evidence while normalizing independently maintained links."""
import json
from pathlib import Path
import tempfile
import unittest

from course_kb.migration import prepare_stage
from course_kb.model import DataError, json_text, load_json, managed_files, source_info
from course_kb.transactions import apply_batch
from course_kb.validation import validate


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="course-kb-migration-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "kb"
        self.course = self.root / "TEST1000"
        for folder in ("raw", "index/weekly", "index/concepts", "notes"):
            (self.course / folder).mkdir(parents=True)
        self.write(self.root / "manifest.json", {
            "kb_version": "1.0", "owner": None, "semester": "Example Fall", "institution": "Example University",
            "program": "Example", "last_updated": "2026-09-26T22:00:00+08:00",
            "courses": [{"code": "TEST1000", "name": "Example", "directory": "TEST1000"}],
            "calendar": {"source": "Example calendar", "term": "Example Fall", "teaching_weeks": 2,
                         "week_ranges": {"w01": "2026-09-01 / 2026-09-05", "w02": "2026-09-06 / 2026-09-12"}},
        })
        self.write(self.course / "course.json", {"code": "TEST1000", "name": "Example", "instructor": "Example",
                   "total_weeks": 2, "weeks_indexed": 2, "language": "en", "created": "2026-09-26T22:00:00+08:00"})
        self.raw_bytes = {}
        for number in (1, 2):
            filename = f"w{number:02d}-transcript.md"
            self.raw_bytes[filename] = f"Original bytes {number}\r\nSpeaker 1 00:00:01.000\r\nExample.\r\n".encode()
            (self.course / "raw" / filename).write_bytes(self.raw_bytes[filename])
            self.write(self.course / f"index/weekly/w{number:02d}.json", {
                "course": "TEST1000", "week": number, "status": "complete", "topic": "Example",
                "date_range": "2026-09-01 / 2026-09-05" if number == 1 else "2026-09-06 / 2026-09-12",
                "sources": [{"type": "transcript", "filename": filename, "key_concepts": ["example"],
                             "version_note": "AI 智能纪要，不是逐字稿" if number == 1 else None}],
                "key_concepts": ["example"], "weekly_summary": "Preserve this summary.", "open_questions": [],
            })
        self.definition = "A quoted phrase followed by an explanation."
        self.write(self.course / "index/concepts/example.json", {
            "name": "Example", "slug": "example", "aliases": [], "definition": self.definition,
            "definition_basis": "verbatim", "originator": None,
            "appearances": [{"week": 2, "source_file": "w02-transcript.md", "context": "Existing context."}],
            "related": [], "tags": [],
        })
        (self.course / "notes/private.txt").write_bytes(b"User note bytes\r\n")

    def write(self, path, data):
        path.write_text(json_text(data), encoding="utf-8")

    def test_migrate_preserves_bytes_and_marks_legacy_assertions(self):
        initial = managed_files(self.root)
        stage = self.base / "stage"
        result = prepare_stage(self.root, stage, migrate=True)
        self.assertEqual(initial, managed_files(self.root))
        self.assertEqual(1, result["courses"][0]["legacy_relations_preserved"])
        self.assertFalse(list(stage.glob("*/raw/*")))
        apply_batch(self.root, stage)
        self.assertEqual([], validate(self.root).errors)
        card = load_json(self.course / "index/concepts/example.json")
        self.assertEqual(self.definition, card["definition"])
        self.assertEqual("unverified", card["definition_basis"])
        self.assertEqual("verbatim", card["legacy_definition_basis"])
        added = next(a for a in card["appearances"] if a["week"] == 1)
        self.assertIsNone(added["context"])
        self.assertIsNone(added["locator"])
        self.assertEqual("legacy_source_index", added["assertion_basis"])
        self.assertEqual("summary", load_json(self.course / "index/weekly/w01.json")["sources"][0]["type"])
        for filename, content in self.raw_bytes.items():
            self.assertEqual(content, (self.course / "raw" / filename).read_bytes())
        self.assertEqual(b"User note bytes\r\n", (self.course / "notes/private.txt").read_bytes())

    def test_repeated_migration_and_apply_are_noops(self):
        stage = self.base / "stage"
        prepare_stage(self.root, stage, migrate=True)
        apply_batch(self.root, stage)
        self.assertEqual("no-op", apply_batch(self.root, stage)["status"])
        before = managed_files(self.root)
        again = self.base / "again"
        self.assertEqual([], prepare_stage(self.root, again, migrate=True)["changed_files"])
        self.assertEqual(before, managed_files(self.root))

    def test_prepare_uses_appearances_as_relation_authority(self):
        stage = self.base / "stage"
        prepare_stage(self.root, stage, migrate=True)
        apply_batch(self.root, stage)
        update = self.base / "update"
        prepare_stage(self.root, update)
        card = load_json(self.course / "index/concepts/example.json")
        card["appearances"] = [a for a in card["appearances"] if a["week"] == 2]
        path = update / "TEST1000/index/concepts/example.json"
        path.parent.mkdir(parents=True)
        self.write(path, card)
        prepare_stage(self.root, update)
        apply_batch(self.root, update)
        self.assertEqual([], load_json(self.course / "index/weekly/w01.json")["key_concepts"])
        self.assertEqual([], validate(self.root).errors)

    def test_stale_stage_is_not_rebased_silently(self):
        stage = self.base / "stage"
        prepare_stage(self.root, stage, migrate=True)
        path = self.course / "course.json"
        data = load_json(path)
        data["name"] = "Changed elsewhere"
        self.write(path, data)
        with self.assertRaisesRegex(DataError, "different KB state"):
            prepare_stage(self.root, stage)

    def test_source_id_filename_conflict_is_not_hidden_by_merge(self):
        stage = self.base / "stage"
        prepare_stage(self.root, stage, migrate=True)
        apply_batch(self.root, stage)
        source = load_json(self.course / "index/weekly/w01.json")["sources"][0]
        source["filename"] = "w02-transcript.md"
        with self.assertRaisesRegex(DataError, "disagree"):
            source_info(self.course, source)

    def test_missing_raw_aborts_without_live_writes(self):
        (self.course / "raw/w01-transcript.md").unlink()
        before = managed_files(self.root)
        with self.assertRaisesRegex(DataError, "missing"):
            prepare_stage(self.root, self.base / "stage", migrate=True)
        self.assertEqual(before, managed_files(self.root))

    def test_public_zero_and_sparse_weeks_migrate_without_renumbering(self):
        manifest = load_json(self.root / "manifest.json")
        ranges = manifest["calendar"]["week_ranges"]
        manifest["calendar"]["week_ranges"] = {"w00": ranges["w01"], "w07": ranges["w02"]}
        self.write(self.root / "manifest.json", manifest)
        retained_raw = {}
        for previous, number in ((1, 0), (2, 7)):
            old_filename = f"w{previous:02d}-transcript.md"
            filename = f"w{number:02d}-transcript.md"
            (self.course / "raw" / old_filename).rename(self.course / "raw" / filename)
            retained_raw[filename] = self.raw_bytes[old_filename]
            path = self.course / f"index/weekly/w{previous:02d}.json"
            week = load_json(path)
            week["week"] = number
            week["sources"][0]["filename"] = filename
            self.write(path, week)
            path.rename(self.course / f"index/weekly/w{number:02d}.json")
        card_path = self.course / "index/concepts/example.json"
        card = load_json(card_path)
        card["appearances"][0].update(week=7, source_file="w07-transcript.md")
        self.write(card_path, card)
        before = managed_files(self.root)
        stage = self.base / "stage"
        prepare_stage(self.root, stage, migrate=True)
        self.assertEqual(before, managed_files(self.root))
        apply_batch(self.root, stage)
        self.assertFalse(validate(self.root).errors)
        self.assertEqual(["w00.json", "w07.json"], sorted(p.name for p in (self.course / "index/weekly").glob("*.json")))
        self.assertEqual({0, 7}, {a["week"] for a in load_json(card_path)["appearances"]})
        self.assertEqual(self.definition, load_json(card_path)["definition"])
        self.assertEqual(2, load_json(self.course / "course.json")["total_weeks"])
        for number in (0, 7):
            self.assertEqual("Preserve this summary.", load_json(self.course / f"index/weekly/w{number:02d}.json")["weekly_summary"])
        for filename, content in retained_raw.items():
            self.assertEqual(content, (self.course / "raw" / filename).read_bytes())

    def test_public_partial_setup_migrates_without_inventing_metadata(self):
        manifest = load_json(self.root / "manifest.json")
        manifest.update(semester=None, institution=None, program=None, setup_complete=True)
        manifest["calendar"].update(source=None, term=None, teaching_weeks=None, week_ranges={})
        manifest["_pending_input"] = ["semester", "institution", "program", "calendar"]
        self.write(self.root / "manifest.json", manifest)
        metadata = load_json(self.course / "course.json")
        metadata.update(instructor=None, total_weeks=None, _pending_input=["instructor", "total_weeks"])
        self.write(self.course / "course.json", metadata)
        for path in (self.course / "index/weekly").glob("*.json"):
            week = load_json(path)
            week.update(date_range=None, _pending_input=["date_range"])
            self.write(path, week)
        before = managed_files(self.root)
        stage = self.base / "stage"
        result = prepare_stage(self.root, stage, migrate=True)
        self.assertEqual(before, managed_files(self.root))
        self.assertTrue({"metadata-pending", "calendar-pending"} <= {rule for rule, _ in result["warnings"]})
        apply_batch(self.root, stage)
        report = validate(self.root)
        self.assertFalse(report.errors, report.errors)
        migrated_manifest = load_json(self.root / "manifest.json")
        for key in ("semester", "institution", "program", "calendar", "setup_complete", "_pending_input"):
            self.assertEqual(manifest[key], migrated_manifest[key])
        self.assertEqual(metadata, load_json(self.course / "course.json"))
        for path in (self.course / "index/weekly").glob("*.json"):
            week = load_json(path)
            self.assertIsNone(week["date_range"])
            self.assertEqual(["date_range"], week["_pending_input"])
            self.assertEqual("Preserve this summary.", week["weekly_summary"])
        for filename, content in self.raw_bytes.items():
            self.assertEqual(content, (self.course / "raw" / filename).read_bytes())


if __name__ == "__main__":
    unittest.main()

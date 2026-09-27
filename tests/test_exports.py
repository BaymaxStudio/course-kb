"""Export regressions exercise consumer-visible claims, bytes, scope and write boundaries."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import gen_directory
import gen_pack
import gen_snapshot
import gen_tier4
from course_kb.model import DataError, read_snapshot
from course_kb.transactions import apply_batch


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="course-kb-export-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "kb"
        self.course = self.root / "TEST101"
        self.out = self.base / "packs"
        for sub in ("raw", "notes", "index/weekly", "index/concepts"):
            (self.course / sub).mkdir(parents=True)
        self.write(self.root / "manifest.json", {
            "kb_version": "1.0", "last_updated": "2026-09-26T00:00:00+08:00",
            "courses": [{"code": "TEST101", "directory": "TEST101", "name": "A course"}]})
        self.write(self.course / "course.json", {"code": "TEST101", "name": "A course", "instructor": "Teacher"})
        self.raw = (b"2026-09-01 10:00|1min\r\n\r\nSpeaker 1 00:00:00.000 \r\n"
                    b"first words\r\n\r\nSpeaker 1 00:00:20.000 \r\nsecond words\r\n\r\n"
                    b"Speaker 1 00:00:40.000 \r\nlast words\r\n")
        (self.course / "raw/w01-transcript.md").write_bytes(self.raw)
        self.source = {"type": "transcript", "filename": "w01-transcript.md", "duration_minutes": 1,
                       "version_note": "Automatic transcription remains unverified.",
                       "segments": [{"approx_start": "00:00", "topic": "Start", "slide_refs": ["p2-3"],
                                     "key_points": ["Source claim."]}]}
        self.week = {"course": "TEST101", "week": 1, "status": "complete", "date_range": "2026-09-01 / 2026-09-07",
                     "topic": "A full topic", "sources": [self.source], "key_concepts": ["concept"],
                     "weekly_summary": "A summary", "open_questions": [], "_pending_input": ["PRIVATE_WORKFLOW"]}
        self.save_week()
        self.card = {"slug": "concept", "name": "Concept", "definition": "A concept.",
                     "definition_basis": "unverified", "legacy_definition_basis": "verbatim",
                     "appearances": [{"week": 1, "source_file": "w01-transcript.md", "context": None,
                                      "source_id": "src-one", "locator": {"start_seconds": 0},
                                      "verification_status": "unverified", "assertion_basis": "legacy_source_index"},
                                     {"week": 2, "source_file": "w02-reading.pdf", "context": "Later context",
                                      "coverage": "reading-only"}],
                     "quotes": [{"text": "Quoted words", "source_id": "src-one", "locator": {"start_seconds": 20},
                                 "verification_status": "unverified"}],
                     "cross_course": [{"course": "OTHER101", "slug": "concept", "note": "Different application"}],
                     "related": [], "tags": []}
        self.write(self.course / "index/concepts/concept.json", self.card)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def save_week(self):
        self.write(self.course / "index/weekly/w01.json", self.week)

    def args(self, **overrides):
        values = dict(root=str(self.root), course="TEST101", week="w01", weeks="w01", tier=3,
                      out=str(self.out), source=None, suffix=None, start=None, end=None)
        values.update(overrides)
        return Namespace(**values)

    def test_legacy_and_registered_summary_are_rejected(self):
        self.source["version_note"] = "AI 智能纪要，不是逐字稿"
        self.save_week()
        with self.assertRaisesRegex(DataError, "摘要或 AI 纪要"):
            gen_tier4.generate(self.args())
        self.source["version_note"] = "Source information"
        self.save_week()
        self.write(self.course / "sources.json", {"schema_version": "1.1", "sources": [
            {"source_id": "src-one", "filename": "w01-transcript.md", "kind": "summary",
             "generation_method": "ai_generated", "verification_status": "unverified"}]})
        with self.assertRaisesRegex(DataError, "摘要或 AI 纪要"):
            gen_tier4.generate(self.args())
        self.assertFalse(self.out.exists())

    def test_ai_generated_transcript_is_rejected_even_when_kind_is_transcript(self):
        self.write(self.course / "sources.json", {"schema_version": "1.1", "sources": [
            {"source_id": "src-one", "filename": "w01-transcript.md", "kind": "transcript",
             "generation_method": "ai_generated", "verification_status": "unverified"}]})
        with self.assertRaisesRegex(DataError, "由 AI 生成"):
            gen_tier4.generate(self.args())
        self.assertFalse(self.out.exists())

    def test_full_transcript_keeps_every_original_byte(self):
        target = gen_tier4.generate(self.args())
        data = target.read_bytes()
        self.assertTrue(data.endswith(self.raw))
        self.assertNotIn(b"Verbatim transcript", data)
        self.assertIn(b"unverified", data)
        self.assertIn(hashlib.sha256(self.raw).hexdigest().encode(), data)

    def test_slices_are_distinct_and_contain_exact_source_span(self):
        first = gen_tier4.generate(self.args(start="00:05", end="00:10"))
        second = gen_tier4.generate(self.args(start="00:25", end="00:30"))
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.out.glob("*.md"))), 2)
        expected = self.raw[self.raw.index(b"Speaker 1 00:00:20"):self.raw.index(b"Speaker 1 00:00:40")]
        self.assertTrue(second.read_bytes().endswith(expected))

    def test_invalid_and_out_of_bounds_slices_fail_without_output(self):
        for start, end in [("00:30", "00:10"), ("00:10", "00:10"), ("01:00", "01:10"),
                           ("00:00", "02:00"), ("-1:00", "00:10"), ("00:70", "01:20")]:
            with self.subTest(start=start, end=end), self.assertRaises(DataError):
                gen_tier4.generate(self.args(start=start, end=end))
        self.assertFalse(self.out.exists())

    def test_multiple_transcripts_need_explicit_selection(self):
        tutorial = dict(self.source, filename="w01-transcript-tutorial.md")
        (self.course / "raw" / tutorial["filename"]).write_bytes(self.raw)
        self.week["sources"].append(tutorial)
        self.save_week()
        with self.assertRaisesRegex(DataError, "多个逐字稿"):
            gen_tier4.generate(self.args())
        target = gen_tier4.generate(self.args(suffix="tutorial"))
        self.assertIn("tutorial", target.name)

    def test_pack_preserves_provenance_full_week_and_card_evidence(self):
        self.week["sources"].append({"type": "reading", "filename": "w01-reading.pdf", "version_note": "Edition uncertain",
                                     "edition": "unknown", "core_argument": "Complete distinctive argument", "outline": ["Outline entry"]})
        self.save_week()
        for tier in (1, 2, 3):
            target = gen_pack.generate(self.args(tier=tier))
            text = target.read_text()
            self.assertIn("Edition uncertain", text)
            self.assertIn("Automatic transcription remains unverified.", text)
            self.assertNotIn("PRIVATE_WORKFLOW", text)
            if tier >= 2:
                for expected in ("Different application", "Quoted words", "start_seconds", "unverified", "legacy_source_index", "Later context", "reading-only"):
                    self.assertIn(expected, text)
            if tier == 3:
                for expected in ("Complete distinctive argument", "Outline entry", "p2-3"):
                    self.assertIn(expected, text)

    def test_exact_week_scope_has_distinct_output_and_missing_week_fails(self):
        for number in (2, 3):
            self.write(self.course / f"index/weekly/w{number:02d}.json", dict(self.week, week=number))
        sparse = gen_pack.generate(self.args(weeks="w01,w03"))
        continuous = gen_pack.generate(self.args(weeks="w01-w03"))
        self.assertNotEqual(sparse, continuous)
        with self.assertRaisesRegex(DataError, "缺少索引"):
            gen_pack.generate(self.args(weeks="w01,w04"))

    def test_readers_refuse_active_or_unfinished_transaction(self):
        runtime = self.root / ".course-kb"
        runtime.mkdir()
        (runtime / "write.lock").write_text("{}")
        for reader in (lambda: gen_pack.generate(self.args()), lambda: gen_tier4.generate(self.args()),
                       lambda: gen_directory.render(self.root),
                       lambda: gen_snapshot.generate(self.args(weeks=0, out=str(self.out / "snapshot.md")))):
            with self.assertRaisesRegex(DataError, "lock"):
                reader()
        (runtime / "write.lock").unlink()
        journal = runtime / "transactions/batch/journal.json"
        journal.parent.mkdir(parents=True)
        self.write(journal, {"state": "applying"})
        with self.assertRaisesRegex(DataError, "Unfinished"):
            gen_pack.generate(self.args())
        self.assertFalse(self.out.exists())

    def test_directory_is_pure_deterministic_and_hard_limited(self):
        for number in range(1, 14):
            week = dict(self.week, week=number, key_concepts=[f"lengthy-concept-{i:02d}-label" for i in range(30)])
            self.write(self.course / f"index/weekly/w{number:02d}.json", week)
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        rendered = gen_directory.render(self.root)
        self.assertLessEqual(len(rendered), 4000)
        self.assertEqual(rendered, gen_directory.render(self.root))
        self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
        self.assertIn("A full topic", rendered)
        self.week["topic"] = "x" * 4001
        self.save_week()
        with self.assertRaisesRegex(DataError, "4000"):
            gen_directory.render(self.root)

    def test_outputs_cannot_overwrite_raw_notes_or_indexes(self):
        for directory in (self.course / "raw", self.course / "notes", self.course / "index/weekly"):
            with self.subTest(directory=directory):
                with self.assertRaises(DataError):
                    gen_pack.generate(self.args(out=str(directory)))
                with self.assertRaises(DataError):
                    gen_tier4.generate(self.args(out=str(directory)))
                with self.assertRaises(DataError):
                    gen_snapshot.generate(self.args(weeks=0, out=str(directory / "snapshot.md")))
        self.assertEqual((self.course / "raw/w01-transcript.md").read_bytes(), self.raw)

    def test_completed_writer_during_read_is_rejected_before_output(self):
        stage = self.base / "stage"
        (stage / "TEST101").mkdir(parents=True)
        original = self.course / "course.json"
        self.write(stage / "TEST101/course.json", {"code": "TEST101", "name": "New course name", "instructor": "Teacher"})
        self.write(stage / ".batch.json", {"base_hashes": {
            "TEST101/course.json": hashlib.sha256(original.read_bytes()).hexdigest()}})
        actual_load = gen_pack.load_json
        commits = []

        def read_after_writer(path):
            if Path(path).name == "w01.json":
                commits.append(apply_batch(self.root, stage, validator=lambda _: Namespace(errors=[])))
            return actual_load(path)

        with patch.object(gen_pack, "load_json", side_effect=read_after_writer):
            with self.assertRaisesRegex(DataError, "changed while being read"):
                gen_pack.generate(self.args())
        self.assertEqual(commits[0]["status"], "committed")
        self.assertFalse((self.root / ".course-kb/write.lock").exists())
        self.assertEqual(json.loads(original.read_text())["name"], "New course name")
        self.assertFalse(self.out.exists())

    def test_existing_guard_holds_shared_lock_without_creating_runtime_files(self):
        runtime = self.root / ".course-kb"
        runtime.mkdir()
        (runtime / "write.guard").touch()
        stage = self.base / "empty-stage"
        stage.mkdir()
        with read_snapshot(self.root):
            with self.assertRaisesRegex(DataError, "writer lock"):
                apply_batch(self.root, stage, validator=lambda _: Namespace(errors=[]))
        self.assertEqual([path.name for path in runtime.iterdir()], ["write.guard"])
        gen_pack.generate(self.args())
        self.assertEqual([path.name for path in runtime.iterdir()], ["write.guard"])

    def test_snapshot_fingerprint_detects_edits_without_runtime(self):
        with self.assertRaisesRegex(DataError, "changed while being read"):
            with read_snapshot(self.root):
                self.card["definition"] = "Changed during read"
                self.write(self.course / "index/concepts/concept.json", self.card)
        self.assertFalse((self.root / ".course-kb").exists())

    def test_directory_prefers_indexing_status_and_preserves_preclass_availability(self):
        self.week["indexing_status"] = "partial"
        self.week["material_availability"] = {"status": "incomplete", "missing": ["lecture slides"]}
        self.save_week()
        rendered = gen_directory.render(self.root)
        self.assertIn("[partial; 缺材料]", rendered)
        self.assertNotIn("[complete]", rendered)
        self.week["status"] = "pre-class"
        self.save_week()
        self.assertIn("[partial; pre-class; 缺材料]", gen_directory.render(self.root))
        self.assertFalse((self.root / ".course-kb").exists())

    def test_snapshot_reports_validation_failure_and_preserves_source_kind(self):
        self.source["version_note"] = "AI 智能纪要，不是逐字稿"
        self.save_week()
        target = self.out / "snapshot.md"
        with patch.object(gen_snapshot, "run_validate", return_value="exit_code=2\nR001 invalid"), contextlib.redirect_stdout(io.StringIO()):
            gen_snapshot.generate(self.args(weeks=1, out=str(target)))
        text = target.read_text()
        self.assertIn("exit_code=2", text)
        self.assertIn('"kind": "summary"', text)
        self.assertIn("AI 智能纪要", text)


if __name__ == "__main__":
    unittest.main()

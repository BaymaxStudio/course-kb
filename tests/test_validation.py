"""用最小知识库检查结构损坏、来源追溯和版本边界。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from platform_helpers import symlink_or_skip

from course_kb.validation import validate


SID = "src-00000000-0000-0000-0000-000000000001"
RAW = "w01-reading-example.pdf"


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="course-kb-validation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.course = self.root / "TEST1000"
        for folder in ("raw", "index/weekly", "index/concepts", "notes"):
            (self.course / folder).mkdir(parents=True)
        (self.course / "raw" / RAW).write_bytes(b"Fixture source bytes.\n")
        self.manifest = {
            "kb_version": "1.1", "owner": "Fixture", "semester": "2026 Fall",
            "institution": "Fixture", "program": "Fixture",
            "last_updated": "2026-09-26T22:00:00+08:00",
            "courses": [{"code": "TEST1000", "name": "Fixture", "directory": "TEST1000"}],
            "calendar": {"source": "Fixture", "term": "2026 Fall", "teaching_weeks": 13,
                         "week_ranges": {"w01": "2026-09-01 / 2026-09-05"}},
        }
        self.metadata = {"code": "TEST1000", "name": "Fixture", "instructor": "Fixture",
                         "total_weeks": 13, "weeks_indexed": 1, "language": "en",
                         "created": "2026-09-26T22:00:00+08:00"}
        self.week = {
            "course": "TEST1000", "week": 1, "status": "complete",
            "date_range": "2026-09-01 / 2026-09-05", "topic": "Fixture",
            "sources": [{"type": "reading", "filename": RAW, "source_id": SID,
                         "title": "Fixture", "key_concepts": ["example"]}],
            "key_concepts": ["example"], "weekly_summary": "Fixture", "open_questions": [],
            "unresolved_concepts": [], "indexing_status": "complete",
            "material_availability": {"status": "unknown", "missing": []},
        }
        self.card = {
            "name": "Example", "slug": "example", "aliases": [],
            "definition": "Fixture", "definition_basis": "synthesized", "originator": None,
            "appearances": [{"week": 1, "source_file": RAW, "source_id": SID,
                             "context": None, "locator": None,
                             "verification_status": "unverified", "assertion_basis": "legacy_appearance"}],
            "related": [], "tags": [], "quotes": [],
        }
        self.registry = {"schema_version": "1.1", "sources": [{
            "source_id": SID, "filename": RAW, "kind": "reading",
            "generation_method": "unknown", "verification_status": "unverified",
            "sha256": hashlib.sha256((self.course / "raw" / RAW).read_bytes()).hexdigest(),
            "original_filename": None, "version_note": None, "derived_from": [],
        }]}
        self.flush()

    def flush(self) -> None:
        for path, value in [(self.root / "manifest.json", self.manifest),
                            (self.course / "course.json", self.metadata),
                            (self.course / "sources.json", self.registry),
                            (self.course / "index/weekly/w01.json", self.week),
                            (self.course / "index/concepts/example.json", self.card)]:
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def rules(self) -> set[str]:
        return {rule for rule, _ in validate(self.root).errors}

    def test_valid_legacy_and_current(self) -> None:
        for version in ("1.0", "1.1"):
            with self.subTest(version=version):
                self.manifest["kb_version"] = version
                self.flush()
                self.assertEqual([], validate(self.root).errors)

    def test_missing_source_and_hash_change_fail(self) -> None:
        (self.course / "raw" / RAW).write_bytes(b"Changed bytes")
        self.assertIn("source-hash", self.rules())
        (self.course / "raw" / RAW).unlink()
        self.assertIn("source-missing", self.rules())

    def test_registry_identifiers_and_derived_from(self) -> None:
        self.registry["sources"][0]["derived_from"] = [SID]
        self.registry["sources"].append(dict(self.registry["sources"][0]))
        self.flush()
        self.assertTrue({"source-id-duplicate", "source-filename-duplicate", "derived-from"} <= self.rules())

    def test_unregistered_raw_is_recovery_warning(self) -> None:
        (self.course / "raw/new-file.pdf").write_bytes(b"Retained after recovery")
        result = validate(self.root)
        self.assertFalse(result.errors)
        self.assertIn("raw-unregistered", {rule for rule, _ in result.warnings})

    def test_relation_mismatch_warns_legacy_errors_current(self) -> None:
        self.week["sources"][0]["key_concepts"] = []
        for version in ("1.0", "1.1"):
            with self.subTest(version=version):
                self.manifest["kb_version"] = version
                self.flush()
                result = validate(self.root)
                target = result.warnings if version == "1.0" else result.errors
                self.assertIn("source-concepts-derived", {rule for rule, _ in target})
                if version == "1.0":
                    self.assertFalse(result.errors)

    def test_unknown_source_id_and_mismatched_week(self) -> None:
        self.card["appearances"][0]["source_id"] = "src-00000000-0000-0000-0000-000000000099"
        self.week["week"] = 99
        self.flush()
        self.assertTrue({"source-id-dangling", "weekly-number", "appearance-week"} <= self.rules())

    def test_source_id_must_agree_with_weekly_and_appearance_filename(self) -> None:
        second_filename = "w01-reading-second.pdf"
        second_id = "src-00000000-0000-0000-0000-000000000002"
        second_raw = self.course / "raw" / second_filename
        second_raw.write_bytes(b"Second source bytes.\n")
        self.registry["sources"].append({
            **self.registry["sources"][0], "source_id": second_id, "filename": second_filename,
            "sha256": hashlib.sha256(second_raw.read_bytes()).hexdigest(),
        })
        for label, reference in (("sources", self.week["sources"][0]),
                                 ("appearances", self.card["appearances"][0])):
            with self.subTest(reference=label):
                reference["source_id"] = second_id
                self.flush()
                errors = validate(self.root).errors
                self.assertTrue(any(rule == "source-id-filename" and label in detail
                                    for rule, detail in errors), errors)
                reference["source_id"] = SID

    def test_ai_generated_transcript_metadata_does_not_certify_tier4_eligibility(self) -> None:
        # 来源类型和生成方式分别保留；Tier4 必须自行拒绝 AI 生成来源。
        self.registry["sources"][0]["kind"] = "transcript"
        self.registry["sources"][0]["generation_method"] = "ai_generated"
        self.week["sources"][0]["type"] = "transcript"
        self.flush()
        self.assertFalse(validate(self.root).errors)

    def test_missing_required_and_nonobject_documents(self) -> None:
        self.metadata = {"code": "TEST1000", "weeks_indexed": 1}
        del self.manifest["calendar"]
        self.flush()
        self.assertTrue({"required", "calendar"} <= self.rules())
        (self.course / "index/weekly/w01.json").write_text("[]")
        self.assertIn("object-type", self.rules())

    def test_nested_bad_types_report_multiple_errors(self) -> None:
        self.week["sources"] = [None, 4, [], {"filename": [RAW], "type": []}]
        self.card["appearances"] = [None, {"week": True, "source_file": {}, "context": []}]
        self.card["related"] = {"not": "a list"}
        self.flush()
        rules = self.rules()
        self.assertTrue({"object-type", "array-type", "source-filename", "integer-type"} <= rules)
        self.assertNotIn("validation-failure", rules)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        (self.course / "index/concepts/example.json").write_text('{"slug":"example","slug":"other"}')
        self.assertIn("json-read", self.rules())

    def test_unsupported_version_is_rejected(self) -> None:
        self.manifest["kb_version"] = "9.0"
        self.flush()
        self.assertIn("schema-version", self.rules())

    def test_path_traversal_is_rejected(self) -> None:
        self.week["sources"][0]["filename"] = "../course.json"
        self.flush()
        self.assertIn("source-filename", self.rules())

    def test_escaping_symlink_is_rejected(self) -> None:
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink)
        raw_path = self.course / "raw" / RAW
        raw_path.unlink()
        symlink_or_skip(self, raw_path, outside)
        self.assertIn("managed-path", self.rules())

    def test_bad_locator_and_coverage(self) -> None:
        appearance = self.card["appearances"][0]
        appearance["locator"] = {"slide": True, "pdf_page": 0, "start_seconds": 50, "end_seconds": 20}
        appearance["coverage"] = "guessed"
        self.flush()
        self.assertTrue({"integer-type", "locator-range", "enum"} <= self.rules())
        appearance["locator"] = {}
        self.flush()
        self.assertIn("locator-empty", self.rules())

    def test_verbatim_requires_independent_verified_quote(self) -> None:
        self.card["definition_basis"] = "verbatim"
        self.flush()
        self.assertIn("verbatim-evidence", self.rules())
        self.card["quotes"] = [{"text": "A literal quotation.", "source_id": SID,
                                "locator": {"pdf_page": 1}, "verification_status": "verified"}]
        self.flush()
        self.assertFalse(validate(self.root).errors)
        self.card["quotes"][0]["locator"] = None
        self.flush()
        self.assertIn("quote-evidence", self.rules())

    def test_definition_quote_mismatch_warns_without_claiming_semantic_validation(self) -> None:
        self.card["definition"] = "A definition written in different words."
        self.card["definition_basis"] = "verbatim"
        self.card["quotes"] = [{"text": "A separately recorded quotation.", "source_id": SID,
                                "locator": {"pdf_page": 1}, "verification_status": "verified"}]
        self.flush()
        report = validate(self.root)
        self.assertFalse(report.errors)
        self.assertIn("definition-quote-review", {rule for rule, _ in report.warnings})
        # 相同文字只能通过结构核对，不能证明这些文字实际存在于源文件。
        self.card["definition"] = self.card["quotes"][0]["text"]
        self.flush()
        report = validate(self.root)
        self.assertFalse(report.errors)
        self.assertNotIn("definition-quote-review", {rule for rule, _ in report.warnings})

    def test_qualified_slide_refs_resolve_filename_and_bounds(self) -> None:
        self.registry["sources"][0]["kind"] = "slides"
        self.week["sources"][0]["type"] = "slides"
        self.week["sources"][0]["page_count"] = 5
        self.week["sources"][0]["segments"] = [{"slide_refs": [RAW + "#p2-4"]}]
        self.flush()
        self.assertFalse(validate(self.root).warnings)
        self.week["sources"][0]["segments"][0]["slide_refs"] = [RAW + "#p9"]
        self.flush()
        self.assertIn("slide-ref-range", {rule for rule, _ in validate(self.root).warnings})

    def test_runtime_lock_checked_and_candidate_opt_out(self) -> None:
        runtime = self.root / ".course-kb"
        runtime.mkdir()
        (runtime / "write.lock").write_text('{"pid":123,"token":"fixture"}')
        self.assertTrue(validate(self.root).errors)
        self.assertFalse(validate(self.root, check_runtime=False).errors)

    def test_pending_metadata_and_optional_coverage_are_preserved(self) -> None:
        self.metadata["language"] = None
        self.metadata["_pending_input"] = ["language"]
        self.card["appearances"][0]["coverage"] = "unknown"
        self.flush()
        self.assertFalse(validate(self.root).errors)

    def test_public_zero_and_sparse_week_numbers_follow_calendar_keys(self) -> None:
        self.metadata["total_weeks"] = 1
        self.manifest["calendar"]["teaching_weeks"] = 1
        for version in ("1.0", "1.1"):
            for number in (0, 3, 14):
                with self.subTest(version=version, number=number):
                    self.manifest["kb_version"] = version
                    name = f"w{number:02d}"
                    self.manifest["calendar"]["week_ranges"] = {name: self.week["date_range"]}
                    self.week["week"] = number
                    self.card["appearances"][0]["week"] = number
                    self.flush()
                    original = self.course / "index/weekly/w01.json"
                    renamed = self.course / f"index/weekly/{name}.json"
                    original.rename(renamed)
                    report = validate(self.root)
                    self.assertFalse(report.errors, report.errors)
                    renamed.rename(original)

    def test_explicit_unknown_setup_metadata_warns_without_blocking(self) -> None:
        for key in ("semester", "institution", "program"):
            self.manifest[key] = None
        for key in ("source", "term", "teaching_weeks"):
            self.manifest["calendar"][key] = None
        self.manifest["calendar"]["week_ranges"] = {}
        self.manifest["setup_complete"] = True
        self.metadata["instructor"] = None
        self.metadata["total_weeks"] = None
        self.week["date_range"] = None
        for version in ("1.0", "1.1"):
            with self.subTest(version=version):
                self.manifest["kb_version"] = version
                self.flush()
                report = validate(self.root)
                self.assertFalse(report.errors, report.errors)
                self.assertTrue({"metadata-pending", "calendar-pending"} <= {rule for rule, _ in report.warnings})

    def test_known_week_membership_and_date_conflicts_remain_errors(self) -> None:
        self.manifest["calendar"]["week_ranges"] = {"w07": self.week["date_range"]}
        self.flush()
        self.assertIn("weekly-calendar", self.rules())
        self.manifest["calendar"]["week_ranges"] = {"w01": None}
        self.flush()
        report = validate(self.root)
        self.assertFalse(report.errors)
        self.assertIn("calendar-pending", {rule for rule, _ in report.warnings})
        self.manifest["calendar"]["week_ranges"]["w01"] = "2026-09-06 / 2026-09-12"
        self.flush()
        self.assertIn("date-range", self.rules())

    def test_unknown_allowance_does_not_accept_missing_or_wrong_types(self) -> None:
        del self.manifest["calendar"]["source"]
        self.manifest["calendar"]["term"] = []
        self.manifest["calendar"]["teaching_weeks"] = "13"
        self.metadata["instructor"] = 5
        self.metadata["total_weeks"] = False
        self.flush()
        self.assertTrue({"required", "string-type", "integer-type"} <= self.rules())

    def test_dates_are_validated_even_when_calendar_is_unknown(self) -> None:
        self.manifest["calendar"]["week_ranges"] = {}
        self.week["date_range"] = "2026-02-30 / 2026-03-01"
        self.flush()
        self.assertIn("date-range-format", self.rules())
        self.manifest["calendar"]["week_ranges"] = {"w01": "2026-09-07 / 2026-09-01"}
        self.week["date_range"] = None
        self.flush()
        self.assertIn("date-range-format", self.rules())
        self.manifest["calendar"]["week_ranges"] = {"w01": 1}
        self.flush()
        self.assertIn("calendar-range", self.rules())

    def test_unresolved_concept_can_keep_week_only_relation(self) -> None:
        self.card["appearances"] = []
        self.week["sources"][0]["key_concepts"] = []
        self.week["unresolved_concepts"] = ["example"]
        self.flush()
        rules = self.rules()
        self.assertNotIn("weekly-concepts-derived", rules)
        self.assertNotIn("source-concepts-derived", rules)

    def test_cli_invalid_nested_types_always_emit_json(self) -> None:
        self.week["sources"] = [None]
        self.flush()
        script = Path(__file__).resolve().parents[1] / "scripts/validate.py"
        run = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--json"],
                             text=True, capture_output=True, check=False)
        self.assertEqual(1, run.returncode)
        self.assertTrue(json.loads(run.stdout)["errors"])
        self.assertNotIn("Traceback", run.stderr)
        again = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--json"],
                               text=True, capture_output=True, check=False)
        self.assertEqual(run.stdout, again.stdout)


if __name__ == "__main__":
    unittest.main()

"""验证首次建库、既有资料保护及随分发包附带模板的完整使用路径。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from course_kb import initialization
from course_kb.initialization import initialize
from course_kb.migration import prepare_stage
from course_kb.model import DataError, load_json
from course_kb.transactions import apply_batch
from course_kb.validation import validate

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-kb-init-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "学校 fall term"
        self.manifest = self.base / "manifest-input.json"
        self.manifest.write_bytes((EXAMPLES / "manifest.json").read_bytes())

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_partial_calendar_initializes_without_inventing_dates(self):
        pdf = self.base / "学校校历.pdf"
        pdf.write_bytes(b"%PDF-1.7\r\nfixture bytes\x00\r\n")
        original = self.manifest.read_bytes()
        result = initialize(self.root, self.manifest, pdf)
        self.assertEqual(result["status"], "initialized")
        self.assertTrue(result["warnings"])
        self.assertEqual((self.root / "_calendar.pdf").read_bytes(), pdf.read_bytes())
        manifest = load_json(self.root / "manifest.json")
        self.assertTrue(manifest["setup_complete"])
        self.assertEqual(manifest["calendar"]["week_ranges"], {})
        self.assertIsNone(manifest["calendar"]["teaching_weeks"])
        self.assertEqual(manifest["calendar"]["source"], "_calendar.pdf")
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertEqual(validate(self.root).errors, [])

    def test_bad_known_dates_leave_no_live_directory(self):
        manifest = load_json(self.manifest)
        manifest["calendar"]["week_ranges"] = {"w00": "2025-99-01 / 2025-09-07"}
        self.write(self.manifest, manifest)
        with self.assertRaisesRegex(DataError, "validation failed"):
            initialize(self.root, self.manifest)
        self.assertFalse(self.root.exists())

    def test_existing_root_is_never_overwritten(self):
        self.root.mkdir()
        original = self.root / "user-notes.txt"
        original.write_bytes(b"user content")
        with self.assertRaisesRegex(DataError, "new directory"):
            initialize(self.root, self.manifest)
        self.assertEqual(list(self.root.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"user content")

    def test_racing_file_is_preserved_and_manifest_is_not_published(self):
        original = initialization._atomic_bytes
        def race(path, content, *args, **kwargs):
            path.write_bytes(b"other writer")
            original(path, content, *args, **kwargs)
        with patch.object(initialization, "_atomic_bytes", race):
            with self.assertRaisesRegex(DataError, "interrupted"):
                initialize(self.root, self.manifest)
        self.assertEqual((self.root / "_directory.md").read_bytes(), b"other writer")
        self.assertFalse((self.root / "manifest.json").exists())

    def test_bootstrap_requires_empty_courses_and_current_schema(self):
        for changes in ({"courses": [{"code": "PS101"}]}, {"kb_version": "1.0"}):
            manifest = load_json(EXAMPLES / "manifest.json")
            manifest.update(changes)
            self.write(self.manifest, manifest)
            with self.assertRaises(DataError):
                initialize(self.root, self.manifest)
            self.assertFalse(self.root.exists())

    def test_packaged_templates_support_first_course_and_week_zero(self):
        initialize(self.root, self.manifest)
        stage = self.base / "候选 first import"
        prepare_stage(self.root, stage)
        manifest = load_json(self.root / "manifest.json")
        manifest["courses"] = [{"code": "PS101", "name": "Example Course", "directory": "PS101"}]
        manifest["calendar"]["week_ranges"] = {"w00": None}
        self.write(stage / "manifest.json", manifest)
        self.write(stage / "PS101/course.json", load_json(EXAMPLES / "course.json"))
        self.write(stage / "PS101/index/weekly/w00.json", load_json(EXAMPLES / "weekly.json"))
        self.write(stage / "PS101/index/concepts/example-concept.json", load_json(EXAMPLES / "concept.json"))
        raw = stage / "PS101/raw/w00-reading-example.md"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"Fictional example material for a test.\r\n")
        prepare_stage(self.root, stage)
        apply_batch(self.root, stage)
        self.assertEqual(validate(self.root).errors, [])
        self.assertEqual((self.root / "PS101/raw" / raw.name).read_bytes(), raw.read_bytes())
        week = load_json(self.root / "PS101/index/weekly/w00.json")
        self.assertEqual(week["key_concepts"], ["example-concept"])
        self.assertTrue(week["sources"][0]["source_id"].startswith("src-"))
        self.assertEqual(load_json(self.root / "PS101/course.json")["weeks_indexed"], 1)
        self.assertEqual(apply_batch(self.root, stage)["status"], "no-op")


if __name__ == "__main__":
    unittest.main()

"""Public release isolation, privacy, archive integrity and reproducibility checks."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from platform_helpers import symlink_or_skip

BUILDER_PATH = Path(__file__).resolve().parents[1] / "scripts/build_public.py"
SPEC = importlib.util.spec_from_file_location("course_kb_public_builder", BUILDER_PATH)
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class PublicDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-kb-public-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "release"
        for relative in builder.PUBLIC_FILES:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            text = "{}\n" if path.suffix == ".json" else "# Portable fixture source\n"
            path.write_text(text, encoding="utf-8")

    def test_only_explicit_public_files_are_packaged(self):
        for relative in ("tests/private_test.py", "scripts/install.py", "scripts/build_public.py",
                         "course_kb/unknown_module.py", "__pycache__/cached.pyc", "docs/rollout-report.json",
                         "real-kb/private.json", "outputs/export.md", "examples/unlisted.json"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE_UNLISTED_CONTENT", encoding="utf-8")
        result = builder.build(self.source, self.output)
        with ZipFile(result["artifacts"]["skill"]) as archive:
            expected = sorted("course-kb/" + name for name in builder.PUBLIC_FILES)
            self.assertEqual(archive.namelist(), expected)
            self.assertIsNone(archive.testzip())
            self.assertFalse(any(b"PRIVATE_UNLISTED_CONTENT" in archive.read(name) for name in archive.namelist()))
            self.assertIn("course-kb/course_kb/initialization.py", archive.namelist())
            self.assertIn("course-kb/course_kb/locking.py", archive.namelist())
        self.assertEqual(result["release_version"], "public-1.1.0")
        self.assertFalse((self.source / "__pycache__/build_public.pyc").exists())

    def test_archives_and_manifests_reproduce_despite_source_timestamps_and_modes(self):
        first = builder.build(self.source, self.output)
        before = {kind: Path(path).read_bytes() for kind, path in first["artifacts"].items()}
        self.assertEqual(before["skill"], before["zip"])
        for relative in builder.PUBLIC_FILES:
            path = self.source / relative
            os.utime(path, (1_800_000_000, 1_800_000_000))
            path.chmod(0o600)
        second = builder.build(self.source, self.base / "second-release")
        after = {kind: Path(path).read_bytes() for kind, path in second["artifacts"].items()}
        self.assertEqual(before, after)
        self.assertEqual(first["archive_sha256"], second["archive_sha256"])
        with ZipFile(io.BytesIO(before["zip"])) as archive:
            for member in archive.infolist():
                self.assertEqual(member.date_time, builder.ARCHIVE_TIME)
                self.assertEqual(member.external_attr >> 16, 0o100644)

    def test_external_manifest_and_checksum_list_verify_every_output_and_member(self):
        result = builder.build(self.source, self.output)
        manifest = json.loads(Path(result["artifacts"]["manifest"]).read_text())
        with ZipFile(result["artifacts"]["zip"]) as archive:
            for member in manifest["files"]:
                data = archive.read(member["path"])
                self.assertEqual(len(data), member["size_bytes"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), member["sha256"])
        checksums = Path(result["artifacts"]["sha256"]).read_text().splitlines()
        self.assertEqual(len(checksums), 3)
        for line in checksums:
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertNotIn(str(self.source), Path(result["artifacts"]["manifest"]).read_text())
        self.assertEqual(manifest["kb_schema_version"], "1.1")

    def test_privacy_preflight_blocks_known_private_markers_before_writing(self):
        for text, expected in (("ghp_" + "A" * 36, "credential"),
                               ("-----BEGIN PRIVATE KEY-----", "private-key"),
                               ("/Users/student/course-kb", "personal-absolute-path"),
                               ("/home/student/course-kb", "personal-absolute-path"),
                               (r"C:\Users\student\course-kb", "personal-absolute-path")):
            with self.subTest(text=text):
                (self.source / "README.md").write_text("Header\n" + text + "\n", encoding="utf-8")
                with self.assertRaisesRegex(builder.BuildError, expected) as raised:
                    builder.build(self.source, self.output)
                self.assertIn("README.md:2:", str(raised.exception))
                self.assertFalse(self.output.exists())

    def test_custom_private_markers_are_blocked_without_recording_their_text(self):
        path = self.source / "README.md"
        path.write_text("Example Private Academy\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.BuildError, "private-marker") as failure:
            builder.build(self.source, self.output, ("Example Private Academy",))
        self.assertNotIn("Example Private Academy", str(failure.exception))
        self.assertFalse(self.output.exists())
        path.write_text("Public source\n", encoding="utf-8")
        result = builder.build(self.source, self.output, ("Example Private Academy",))
        text = Path(result["artifacts"]["manifest"]).read_text()
        self.assertNotIn("Example Private Academy", text)
        self.assertEqual(json.loads(text)["privacy_preflight"]["custom_marker_count"], 1)

    def test_generic_home_relative_path_is_allowed(self):
        (self.source / "README.md").write_text("Use ~/course-kb with your own courses.\n", encoding="utf-8")
        self.assertEqual(builder.build(self.source, self.output)["file_count"], len(builder.PUBLIC_FILES))

    def test_missing_new_module_is_an_error_not_a_partial_release(self):
        (self.source / "course_kb/locking.py").unlink()
        with self.assertRaisesRegex(builder.BuildError, "course_kb/locking.py"):
            builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_source_file_and_directory_symlinks_are_rejected(self):
        path = self.source / "README.md"
        path.unlink()
        target = self.base / "original-readme.md"
        target.write_text("Public text", encoding="utf-8")
        symlink_or_skip(self, path, target)
        with self.assertRaisesRegex(builder.BuildError, "Symlink source"):
            builder.build(self.source, self.output)
        path.unlink()
        path.write_text("Public text", encoding="utf-8")
        original = self.source / "references"
        moved = self.base / "reference-files"
        original.rename(moved)
        symlink_or_skip(self, original, moved, directory=True)
        with self.assertRaisesRegex(builder.BuildError, "Symlink source"):
            builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_source_root_symlink_is_rejected(self):
        alias = self.base / "source-alias"
        symlink_or_skip(self, alias, self.source, directory=True)
        with self.assertRaisesRegex(builder.BuildError, "Source root must not be a symlink"):
            builder.build(alias, self.output)

    def test_output_inside_source_is_rejected(self):
        with self.assertRaisesRegex(builder.BuildError, "outside the source tree"):
            builder.build(self.source, self.source / "release")
        self.assertFalse((self.source / "release").exists())

    def test_invalid_python_and_json_are_rejected_before_writing(self):
        path = self.source / "course_kb/locking.py"
        path.write_text("def invalid(\n", encoding="utf-8")
        with self.assertRaisesRegex(builder.BuildError, "Invalid source syntax"):
            builder.build(self.source, self.output)
        path.write_text("# valid\n", encoding="utf-8")
        (self.source / "examples/manifest.json").write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(builder.BuildError, "Invalid source syntax"):
            builder.build(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_reports_failure_without_traceback_or_output(self):
        (self.source / "README.md").write_text("Example Private Academy\n", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = builder.main(["--source", str(self.source), "--out", str(self.output),
                                 "--deny-text", "Example Private Academy"])
        self.assertEqual(code, 2)
        self.assertIn("privacy preflight failed", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

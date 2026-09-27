#!/usr/bin/env python3
"""Build reproducible public archives from an explicit, privacy-checked file list."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from zipfile import ZIP_STORED, ZipFile, ZipInfo

RELEASE_VERSION = "public-1.1.0"
SCHEMA_VERSION = "1.1"
PACKAGE_ROOT = "course-kb"
ARCHIVE_TIME = (1980, 1, 1, 0, 0, 0)
PUBLIC_FILES = (
    "SKILL.md",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "references/schemas.md",
    "gen_pack.py",
    "gen_tier4.py",
    "gen_directory.py",
    "gen_snapshot.py",
    "course_kb/__init__.py",
    "course_kb/__main__.py",
    "course_kb/cli.py",
    "course_kb/initialization.py",
    "course_kb/locking.py",
    "course_kb/migration.py",
    "course_kb/model.py",
    "course_kb/transactions.py",
    "course_kb/validation.py",
    "scripts/kb.py",
    "scripts/validate.py",
    "examples/README.md",
    "examples/manifest.json",
    "examples/course.json",
    "examples/weekly.json",
    "examples/concept.json",
)

PRIVACY_RULES = (
    ("credential", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,})")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("personal-absolute-path", re.compile(
        r"(?:/Users/[^/\s\"']+|/home/[^/\s\"']+|[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\s\"']+)",
        re.IGNORECASE)),
)


class BuildError(ValueError):
    """A release input failed a required build check."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_path(root: Path, relative: str) -> Path:
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise BuildError(f"Symlink source is not distributable: {relative}")
    if not path.is_file():
        raise BuildError(f"Required public file is missing or not a regular file: {relative}")
    return path


def _privacy_issues(relative: str, text: str, deny_texts: tuple[str, ...] = ()) -> list[str]:
    issues = []
    rules = (*PRIVACY_RULES, *(("private-marker", re.compile(re.escape(value), re.IGNORECASE))
                             for value in deny_texts))
    for rule, pattern in rules:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            # 报告规则和位置即可，不将命中的个人内容复制进日志。
            issues.append(f"{relative}:{line}: {rule}")
    return issues


def collect_public_files(source: Path, deny_texts: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Read only named public files; unrelated source-tree contents are never traversed."""
    if source.is_symlink():
        raise BuildError("Source root must not be a symlink")
    root = source.resolve()
    if not root.is_dir():
        raise BuildError("Source root must be an existing directory")
    contents = {}
    privacy_issues = []
    for relative in sorted(PUBLIC_FILES):
        path = _source_path(root, relative)
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise BuildError(f"Source changed while being read: {relative}")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise BuildError(f"Public source is not UTF-8 text: {relative}") from error
        privacy_issues.extend(_privacy_issues(relative, text, deny_texts))
        try:
            if relative.endswith(".py"):
                compile(text, relative, "exec", dont_inherit=True)
            elif relative.endswith(".json"):
                json.loads(text)
        except (SyntaxError, ValueError) as error:
            raise BuildError(f"Invalid source syntax in {relative}: {error}") from error
        contents[f"{PACKAGE_ROOT}/{relative}"] = data
    if privacy_issues:
        raise BuildError("Public privacy preflight failed:\n" + "\n".join(privacy_issues))
    # 捕捉读取其他文件期间发生的编辑，避免把同时变更中的文件组合成一个发行版。
    for relative in sorted(PUBLIC_FILES):
        if _source_path(root, relative).read_bytes() != contents[f"{PACKAGE_ROOT}/{relative}"]:
            raise BuildError(f"Source changed during build: {relative}")
    return contents


def archive_bytes(contents: dict[str, bytes]) -> bytes:
    """Use fixed metadata and stored members so compression-library versions cannot vary bytes."""
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_STORED) as archive:
        for name in sorted(contents):
            entry = ZipInfo(name, date_time=ARCHIVE_TIME)
            entry.create_system = 3
            entry.compress_type = ZIP_STORED
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.extra = b""
            entry.comment = b""
            archive.writestr(entry, contents[name])
    data = stream.getvalue()
    with ZipFile(io.BytesIO(data)) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise BuildError(f"Archive integrity verification failed: {bad_member}")
        if archive.namelist() != sorted(contents):
            raise BuildError("Archive file list differs from the public whitelist")
        for name, original in contents.items():
            if archive.read(name) != original:
                raise BuildError(f"Archive member differs from its source: {name}")
    return data


def build(source: str | Path, out: str | Path, deny_texts: tuple[str, ...] = ()) -> dict:
    """Build .skill, .zip, checksum list, and per-file manifest outside the source tree."""
    source = Path(source).expanduser().absolute()
    if source.is_symlink():
        raise BuildError("Source root must not be a symlink")
    output = Path(out).expanduser().resolve()
    resolved_source = source.resolve()
    if output == resolved_source or resolved_source in output.parents:
        raise BuildError("--out must be a directory outside the source tree")
    if any(not isinstance(value, str) or not value.strip() for value in deny_texts):
        raise BuildError("Private markers must be nonempty strings")
    contents = collect_public_files(source, deny_texts)
    data = archive_bytes(contents)
    stem = f"{PACKAGE_ROOT}-{RELEASE_VERSION}"
    skill_name, zip_name = stem + ".skill", stem + ".zip"
    manifest_name, checksums_name = stem + ".manifest.json", stem + ".sha256"
    archive_hash = sha256(data)
    manifest = {
        "manifest_version": 1,
        "package": PACKAGE_ROOT,
        "release_version": RELEASE_VERSION,
        "kb_schema_version": SCHEMA_VERSION,
        "archive_root": PACKAGE_ROOT + "/",
        "archive_format": "zip-stored",
        "privacy_preflight": {"passed": True, "rules": [name for name, _ in PRIVACY_RULES],
                              "custom_marker_count": len(deny_texts)},
        "archives": [{"filename": name, "size_bytes": len(data), "sha256": archive_hash}
                     for name in (skill_name, zip_name)],
        "files": [{"path": name, "size_bytes": len(contents[name]), "sha256": sha256(contents[name])}
                  for name in sorted(contents)],
    }
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    artifacts = {skill_name: data, zip_name: data, manifest_name: manifest_data}
    artifacts[checksums_name] = "".join(
        f"{sha256(artifacts[name])}  {name}\n" for name in sorted(artifacts)
    ).encode("utf-8")
    for name in artifacts:
        if (output / name).is_symlink():
            raise BuildError(f"Refusing to replace a symlink output: {name}")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".course-kb-build-", dir=output) as staging:
        stage = Path(staging)
        for name, payload in artifacts.items():
            (stage / name).write_bytes(payload)
        for name in sorted(artifacts):
            os.replace(stage / name, output / name)
    return {"release_version": RELEASE_VERSION, "file_count": len(contents), "archive_sha256": archive_hash,
            "artifacts": {kind: str(output / name) for kind, name in
                          (("skill", skill_name), ("zip", zip_name), ("manifest", manifest_name), ("sha256", checksums_name))}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1],
                        help="source project directory; defaults to this script's project")
    parser.add_argument("--out", type=Path, required=True, help="output directory outside the source project")
    parser.add_argument("--deny-text", action="append", default=[],
                        help="optional private marker to reject; repeat for additional names or institutions")
    args = parser.parse_args(argv)
    try:
        result = build(args.source, args.out, tuple(args.deny_text))
    except (BuildError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

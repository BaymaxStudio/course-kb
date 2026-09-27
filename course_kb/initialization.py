"""从 Agent 已读过的校历元数据创建新库，不猜日期、不覆盖已有目录。"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile

from .model import DataError, json_text, load_json
from .transactions import _atomic_bytes
from .validation import validate


def initialize(root: Path, manifest_path: Path, calendar_file: Path | None = None) -> dict:
    """先在外部验证空库；成功后独占创建目标目录，最后发布 manifest。"""
    # 先检查原始路径，避免把指向不存在目标的链接当成新库。
    root = Path(os.path.abspath(root.expanduser()))
    if root.exists() or root.is_symlink():
        raise DataError("Initialization requires a new directory; inspect or migrate the existing KB")
    if not root.parent.is_dir():
        raise DataError("The parent directory must already exist; choose an existing location")
    manifest = deepcopy(load_json(manifest_path.expanduser().resolve()))
    if not isinstance(manifest, dict) or manifest.get("kb_version") != "1.1":
        raise DataError("Initialization requires a schema 1.1 manifest object")
    if manifest.get("courses") != []:
        raise DataError("Initialize with courses: []; add courses through prepare/apply afterwards")
    if not isinstance(manifest.get("calendar"), dict):
        raise DataError("manifest.calendar must be an object")
    if calendar_file is not None:
        calendar_file = calendar_file.expanduser().resolve()
        if not calendar_file.is_file():
            raise DataError("Calendar file does not exist")
        manifest["calendar"]["source"] = "_calendar.pdf"
    # 标记首启已经执行；校历是否完整仍由原字段与校验警告表达。
    manifest["setup_complete"] = True
    with tempfile.TemporaryDirectory(prefix=".course-kb-init-", dir=root.parent) as temporary:
        candidate = Path(temporary)
        (candidate / "manifest.json").write_text(json_text(manifest), encoding="utf-8")
        if calendar_file is not None:
            shutil.copyfile(calendar_file, candidate / "_calendar.pdf")
        report = validate(candidate)
        if report.errors:
            raise DataError("Initialization validation failed:\n" + "\n".join(
                f"{rule}: {detail}" for rule, detail in report.errors))
        from gen_directory import render
        (candidate / "_directory.md").write_text(render(candidate), encoding="utf-8")
        # mkdir 的排他性防止同时初始化同一路径；每份文件也只能在目标不存在时发布。
        root.mkdir()
        files = ["_calendar.pdf"] if calendar_file is not None else []
        files += ["_directory.md", "manifest.json"]
        try:
            for name in files:
                _atomic_bytes(root / name, (candidate / name).read_bytes(), candidate, exclusive=True)
        except OSError as error:
            raise DataError(f"Initialization interrupted at {root}; retain existing files and inspect before retrying: {error}") from error
    return {"status": "initialized", "root": str(root), "schema_version": "1.1",
            "files": files, "warnings": report.warnings}

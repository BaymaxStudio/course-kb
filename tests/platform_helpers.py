"""按实际平台能力运行文件系统测试，其他失败仍然报错。"""
import os
from pathlib import Path
import unittest


def symlink_or_skip(case: unittest.TestCase, link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) in {50, 1314}:
            case.skipTest("Windows does not permit creating symlinks on this account or filesystem")
        raise

#!/usr/bin/env python3
"""兼容已有技能调用路径的只读校验入口。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from course_kb.validation import main


if __name__ == "__main__":
    raise SystemExit(main())

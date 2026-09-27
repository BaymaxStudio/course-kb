#!/usr/bin/env python3
"""Run the canonical backend from any working directory or skill symlink."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from course_kb.cli import main

raise SystemExit(main())

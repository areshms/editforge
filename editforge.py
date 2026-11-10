#!/usr/bin/env python3
"""
EditForge CLI — AI Video Editing Agent Service

Usage:
    editforge.py <input> --instruction <instruction> [--out <output>]

Example:
    editforge.py in.mp4 --instruction 'cut silence + captions on' --out out.mp4
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from editforge_core import main as core_main

if __name__ == "__main__":
    core_main()

#!/usr/bin/env python3
"""Final five-stage planning workflow using only local project modules."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "core"))
from v4_workflow import run


if __name__ == "__main__":
    raise SystemExit(run("v4.5"))

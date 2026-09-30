#!/usr/bin/env python3
"""Compatibility entry point for the original port and state directory."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "dsh-gpt-supervisor/scripts"))
from bridge import main

if __name__ == "__main__":
    if "--port" not in sys.argv:
        sys.argv.extend(["--port", "13081"])
    if "--state-dir" not in sys.argv:
        sys.argv.extend(
            [
                "--state-dir",
                str(Path(__file__).resolve().parent.parent / "work/dsh-bridge"),
            ]
        )
    main()

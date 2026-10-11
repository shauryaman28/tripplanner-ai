"""Write constraints.txt again from the environment this is run in — after the suites have passed on it.

    python scripts/freeze_constraints.py

constraints.txt holds the exact versions that are deployed (its own header says why). This keeps
the header and replaces the pins with what `pip freeze` says is installed now.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "constraints.txt"


def main() -> int:
    header = [line for line in FILE.read_text().splitlines() if line.startswith("#")]
    header = [
        f"# Made on {date.today()} from Python {platform.python_version()}." if line.startswith("# Made on ") else line
        for line in header
    ]
    frozen = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True).stdout
    pins = sorted((line for line in frozen.splitlines() if re.match(r"^[A-Za-z0-9._-]+==", line)), key=str.lower)
    FILE.write_text("\n".join(header + pins) + "\n")
    print(f"constraints.txt: {len(pins)} pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())

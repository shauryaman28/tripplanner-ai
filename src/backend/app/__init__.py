"""Backend package.

The agent layer (`src.ai.*`) lives beside `src/backend`, so the repo root has
to be importable no matter which directory uvicorn is started from.
"""

import sys
from pathlib import Path

__version__ = "0.27.0"  # 0.<phase>.0 — tests/unit/test_version.py holds it to the README's status line

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

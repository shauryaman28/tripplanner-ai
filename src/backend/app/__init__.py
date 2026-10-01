"""Backend package.

The agent layer (`src.ai.*`) lives beside `src/backend`, so the repo root has
to be importable no matter which directory uvicorn is started from.
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

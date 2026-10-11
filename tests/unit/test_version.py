"""The app's version is its phase: 0.<phase>.0, the phase being the one the README says the project is at.

Each phase used to pin the version in a test of its own, and the next phase's
bump then broke the last phase's test. One check, which follows the README.
"""

import re
from pathlib import Path

from app.main import app

README = Path(__file__).resolve().parents[2] / "README.md"


def test_the_app_version_is_the_phase_the_readme_says():
    status = re.search(r"^\*\*Status: Phase (\d+) / 50", README.read_text(), flags=re.MULTILINE)

    assert status, "README.md has no '**Status: Phase N / 50' line"
    assert app.version == f"0.{status.group(1)}.0"

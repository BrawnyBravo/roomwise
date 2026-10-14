"""CI must run on a Python and Home Assistant pair that work together, with one ruff version everywhere."""

from __future__ import annotations

import re
from importlib.metadata import metadata, version
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^([A-Za-z0-9_.-]+)==([^\s#]+)", line.strip()):
            pins[m.group(1).lower()] = m.group(2)
    return pins


def test_ruff_version_is_the_same_in_pre_commit_and_requirements():
    rev = re.search(r"ruff-pre-commit\s+rev:\s*v?(\S+)", (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    assert rev is not None
    assert _pins(ROOT / "requirements.txt")["ruff"] == rev.group(1)


def test_test_requirements_pin_the_ha_pair_this_suite_runs_on():
    pins = _pins(ROOT / "requirements_test.txt")
    assert pins["pytest-homeassistant-custom-component"] == version("pytest-homeassistant-custom-component")
    assert pins["homeassistant"] == version("homeassistant")


def test_ci_python_satisfies_home_assistant():
    workflow = (ROOT / ".github" / "workflows" / "tests.yaml").read_text(encoding="utf-8")
    m = re.search(r"DEFAULT_PYTHON:\s*\"?(\d+)\.(\d+)", workflow)
    assert m is not None
    ci = (int(m.group(1)), int(m.group(2)))
    needs = re.match(r">=\s*(\d+)\.(\d+)", metadata("homeassistant")["Requires-Python"])
    assert ci >= (int(needs.group(1)), int(needs.group(2)))

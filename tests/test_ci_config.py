"""CI must run on a Python and Home Assistant pair that work together, with one ruff version everywhere."""

from __future__ import annotations

import json
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


MIN_HA = "2026.9.0"  # oldest Home Assistant this is tested on (the suite's pinned pair is 2026.9.x)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def test_minimum_home_assistant_is_the_tested_one_everywhere():
    """HACS refuses older installs, pip agrees, and the README says the same (BaseScannerEntity needs 2026.6+)."""
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["homeassistant"] == MIN_HA
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^homeassistant>=" + re.escape(MIN_HA) + r"\s*$", reqs, re.MULTILINE)
    assert f"Home Assistant {MIN_HA} or newer" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert _version_tuple(MIN_HA) <= _version_tuple(version("homeassistant"))
    pinned = _pins(ROOT / "requirements_test.txt")["homeassistant"]
    assert _version_tuple(MIN_HA)[:2] == _version_tuple(pinned)[:2]  # the release line CI tests


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _installed_closure(roots: set[str]) -> set[str]:
    """Every distribution pip would install for these roots (no extras)."""
    from importlib.metadata import PackageNotFoundError, requires  # noqa: PLC0415

    from packaging.requirements import Requirement  # noqa: PLC0415

    seen: set[str] = set()
    todo = [_norm(r) for r in roots]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = requires(name) or []
        except PackageNotFoundError:
            continue
        for text in reqs:
            req = Requirement(text)
            if req.marker is None or req.marker.evaluate({"extra": ""}):
                todo.append(_norm(req.name))
    return seen


def test_test_requirements_cover_the_ha_components_roomwise_loads():
    """
    A clean CI install (requirements_test.txt only) must import everything.

    Home Assistant installs a component's own requirements at runtime, not as
    package dependencies, so the test environment has to list them (HA 2026.9's
    usb component, pulled in by bluetooth, needs serialx).
    """
    import homeassistant  # noqa: PLC0415

    components = Path(homeassistant.__file__).parent / "components"
    manifest = json.loads((ROOT / "custom_components" / "roomwise" / "manifest.json").read_text(encoding="utf-8"))
    todo = list(manifest.get("dependencies", [])) + list(manifest.get("after_dependencies", []))
    seen: set[str] = set()
    needed: dict[str, str] = {}
    while todo:
        domain = todo.pop()
        if domain in seen:
            continue
        seen.add(domain)
        comp = json.loads((components / domain / "manifest.json").read_text(encoding="utf-8"))
        for req in comp.get("requirements", []):
            needed[_norm(re.split(r"[=<>!~\[ ]", req, maxsplit=1)[0])] = domain
        todo += comp.get("dependencies", [])
    roots = {"homeassistant", "pytest-homeassistant-custom-component"}
    for name in ("requirements.txt", "requirements_test.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            if m := re.match(r"^([A-Za-z0-9_.-]+)", line.strip()):
                roots.add(m.group(1))
    missing = {req: domain for req, domain in needed.items() if req not in _installed_closure(roots)}
    assert not missing, f"add to requirements_test.txt: {missing}"

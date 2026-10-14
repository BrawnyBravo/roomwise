"""Publishing checks on the user documentation."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("pr", "author"),
    [
        ("833", "@davidcoulson"),
        ("848", "@barneyonline"),
        ("863", "@davidcoulson"),
        ("865", "@davidcoulson"),
        ("867", "@davidcoulson"),
        ("836", "@BrawnyBravo"),
    ],
)
def test_readme_and_changelog_credit_each_ported_pull_request(pr, author):
    for name in ("README.md", "CHANGELOG.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        line = next((ln for ln in text.splitlines() if f"#{pr}" in ln), "")
        block = text[text.find(f"#{pr}") : text.find(f"#{pr}") + 200]
        assert author in line or author in block, (name, pr)


def test_no_promises_of_unreleased_docs():
    for name in ("README.md", "info.md"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert "release notes" not in text, name
        assert "coming in 0.1" not in text, name


def test_readme_documents_autotune_undo_and_export_privacy():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for words in ("## Auto-tune rooms", "What Apply saves", "Reset auto-tune settings", "not redacted"):
        assert words in text, words


def test_readme_says_some_fixed_devices_appear_after_20_minutes():
    """Shape-rotating addresses are only offered once heard steadily (autotune_ha.STEADY_MIN_AGE)."""
    from custom_components.roomwise.autotune_ha import STEADY_MIN_AGE  # noqa: PLC0415

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    section = text[text.find("### How to run it") : text.find("### What Apply saves")]
    assert f"about {STEADY_MIN_AGE // 60} minutes" in section
    assert "restart" in section

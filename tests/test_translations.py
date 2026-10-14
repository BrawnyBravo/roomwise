"""
Translation files pass Home Assistant's hassfest string checks.

hassfest (script/hassfest/translations.py in home-assistant/core, checked
against tag 2026.9.3) validates translations/en.json of a custom integration
and rejects, among other things, any URL inside a translation string: links
must be passed in as description placeholders. The checks below are copied
from validate_translation_value there and applied to every language, so a
failure shows up here before CI.
"""

from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv

from custom_components.roomwise.const import DOCS_URL, DOMAIN

TRANSLATIONS = sorted(
    (Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN / "translations").glob("*.json")
)

# Copied from script/hassfest/translations.py (home-assistant/core 2026.9.3).
RE_COMBINED_REFERENCE = re.compile(r"(.+\[%)|(%\].+)")
RE_PLACEHOLDER_IN_SINGLE_QUOTES = re.compile(r"'{\w+}'")
RE_URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)


def _hassfest_value_errors(value: str) -> list[str]:
    """The checks of hassfest's validate_translation_value (placeholders allowed, URLs not)."""
    errors = []
    try:
        cv.string_with_no_html(value)
    except vol.Invalid as err:
        errors.append(str(err))
    if RE_PLACEHOLDER_IN_SINGLE_QUOTES.search(value):
        errors.append("placeholder inside single quotes")
    for _, field_name, _, _ in string.Formatter().parse(value):
        if field_name and not field_name.isidentifier():
            errors.append(f"placeholder {field_name!r} is not a valid identifier")
    if RE_COMBINED_REFERENCE.search(value):
        errors.append("combined translation")
    if value != value.strip():
        errors.append("leading or trailing spaces")
    if RE_URL.search(value):
        errors.append("the string should not contain URLs, please use description placeholders instead")
    return errors


def _strings(node, path=()):
    if isinstance(node, dict):
        for key, val in node.items():
            yield from _strings(val, (*path, key))
    elif isinstance(node, str):
        yield ".".join(path), node


def test_translation_files_found():
    assert {p.name for p in TRANSLATIONS} >= {
        "en.json",
        "el.json",
        "fr.json",
        "ko.json",
        "nb.json",
        "nl.json",
        "pt.json",
    }


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.name)
def test_translation_strings_pass_hassfest_checks(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))  # also: the file is valid JSON
    problems = [(key, err) for key, value in _strings(data) for err in _hassfest_value_errors(value)]
    assert not problems, problems


@pytest.mark.parametrize("path", TRANSLATIONS, ids=lambda p: p.name)
def test_user_step_link_comes_from_a_placeholder(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    description = data["config"]["step"]["user"]["description"]
    fields = {f for _, f, _, _ in string.Formatter().parse(description) if f}
    assert fields == {"url"}, fields


def test_hassfest_url_check_catches_the_old_string():
    """The copied check really does reject the string hassfest rejected upstream."""
    old = "If you need help with the configuration have a look at our [GitHub page](https://github.com/example/x)"
    assert _hassfest_value_errors(old)
    assert not _hassfest_value_errors("If you need help have a look at our [GitHub page]({url})")


async def test_user_step_supplies_the_url_placeholder(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["description_placeholders"]["url"] == DOCS_URL

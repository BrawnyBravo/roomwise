"""Translation gaps: every translation key and abort reason has an English string."""

from __future__ import annotations

import json
import re
from pathlib import Path

import custom_components.roomwise.number as number
import custom_components.roomwise.sensor as sensor
from custom_components.roomwise.const import DOMAIN

TRANSLATIONS = Path(__file__).resolve().parent.parent / "custom_components" / DOMAIN / "translations"


def _own_attr(cls, name):
    # Home Assistant's entity metaclass stores _attr_x class values as __attr_x.
    return cls.__dict__.get(f"_{name}", cls.__dict__.get(name))


def test_every_entity_translation_key_has_an_english_name():
    en = json.loads((TRANSLATIONS / "en.json").read_text("utf-8"))
    keys = set()
    for module, platform in ((sensor, "sensor"), (number, "number")):
        for obj in vars(module).values():
            if isinstance(obj, type) and isinstance(key := _own_attr(obj, "_attr_translation_key"), str):
                keys.add((platform, key))
                # A hard-coded name would win over the translated one.
                assert _own_attr(obj, "_attr_name") is None, obj
    assert ("number", "ref_power") in keys
    for platform, key in keys:
        assert en["entity"][platform][key]["name"], (platform, key)


def test_config_flow_abort_reasons_are_translated():
    en = json.loads((TRANSLATIONS / "en.json").read_text("utf-8"))
    source = (TRANSLATIONS.parent / "config_flow.py").read_text("utf-8")
    reasons = set(re.findall(r'async_abort\(reason="(\w+)"\)', source))
    if "_abort_if_unique_id_configured" in source:
        reasons.add("already_configured")
    assert reasons <= set(en["config"]["abort"])

"""Auto-tune's saved settings are visible, resettable, and never exceed the global max radius."""

from __future__ import annotations

import json
from pathlib import Path

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import autotune as at
from custom_components.roomwise import autotune_ha
from custom_components.roomwise.const import CONF_MAX_RADIUS, CONF_RSSI_OFFSETS, CONF_SCANNER_MAX_RADIUS

from .test_area_radius import _advert, _select

SCN_A = "aa:aa:aa:aa:aa:01"
SCN_B = "aa:aa:aa:aa:aa:02"


def test_saved_receiver_radius_never_exceeds_global_in_the_room_rule():
    """Auto-tune saved 15 m; the user later lowered the global max radius to 5 m: 5 m applies."""
    now = monotonic_time_coarse()
    adverts = {SCN_A: _advert(SCN_A, "Kitchen", 8.0, now)}
    assert _select(adverts, scanner_radius={SCN_A: 15.0}, max_radius=5.0) is None
    assert _select(adverts, incumbent_scanner=SCN_A, scanner_radius={SCN_A: 15.0}, max_radius=5.0) is None
    # still a real limit when it is the smaller one
    assert _select(adverts, scanner_radius={SCN_A: 6.0}, max_radius=20.0) is None
    assert _select(adverts, scanner_radius={SCN_A: 10.0}, max_radius=20.0) == SCN_A


def test_tuner_uses_the_same_clamp():
    settings = at.Settings(-55.0, 3.0, 5.0, {}, {SCN_A: 15.0, SCN_B: 3.0})
    assert settings.radius(SCN_A) == 5.0
    assert settings.radius(SCN_B) == 3.0


def test_result_shows_saved_range_limits_even_when_nothing_changes():
    current = at.Settings(-55.0, 3.0, 20.0, {SCN_A: 0}, {SCN_A: 4.0})
    report = at.TuneReport(
        current=current,
        proposed=current.copy(),
        results=[],
        scored=3,
        right_before=3,
        right_after=3,
        recommend=False,
        summary="already as good",
    )
    table = at.format_table(report, {SCN_A: "Hall proxy"})
    assert "Range limits saved earlier" in table
    assert "| Hall proxy | 4 |" in table


def test_apply_help_says_exactly_what_is_saved():
    strings = json.loads((Path(autotune_ha.__file__).parent / "translations" / "en.json").read_text(encoding="utf-8"))
    text = strings["options"]["step"]["autotune_result"]["data_description"]["autotune_apply"]
    for words in ("offsets", "range limits", "devices you picked", "Reset auto-tune settings"):
        assert words in text, words
    assert "autotune_reset" in strings["options"]["step"]


async def _open_reset(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert "autotune_reset" in result["menu_options"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "autotune_reset"}
    )


async def _with_options(hass: HomeAssistant, entry: MockConfigEntry):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_MAX_RADIUS: 6.0,
            CONF_SCANNER_MAX_RADIUS: {SCN_A: 4.0, SCN_B: 15.0},
            CONF_RSSI_OFFSETS: {SCN_A: 3, SCN_B: -5},
            autotune_ha.CONF_AUTOTUNE_LANDMARKS: {"11:22:33:44:55:66": "kitchen"},
        },
    )
    await hass.async_block_till_done()


async def test_reset_shows_and_removes_range_limits(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    await _with_options(hass, setup_bermuda_entry)
    result = await _open_reset(hass, setup_bermuda_entry)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "autotune_reset"
    placeholders = result["description_placeholders"]
    assert "4 m" in placeholders["radius_lines"]
    assert "6 m" in placeholders["radius_lines"]  # 15 m saved, shown as the 6 m that applies
    assert placeholders["landmark_count"] == "1"
    assert "+3 dB" in placeholders["offset_lines"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"reset_scanner_max_radius": True, "reset_landmarks": True, "reset_rssi_offsets": False},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    options = setup_bermuda_entry.options
    assert CONF_SCANNER_MAX_RADIUS not in options
    assert autotune_ha.CONF_AUTOTUNE_LANDMARKS not in options
    assert options[CONF_RSSI_OFFSETS] == {SCN_A: 3, SCN_B: -5}  # untouched unless asked


async def test_reset_can_clear_offsets_only(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    await _with_options(hass, setup_bermuda_entry)
    result = await _open_reset(hass, setup_bermuda_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"reset_scanner_max_radius": False, "reset_landmarks": False, "reset_rssi_offsets": True},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    options = setup_bermuda_entry.options
    assert options[CONF_RSSI_OFFSETS] == {}
    assert options[CONF_SCANNER_MAX_RADIUS] == {SCN_A: 4.0, SCN_B: 15.0}

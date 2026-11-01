"""Coverage for the room auto-tuner: pure maths, report text, the options-flow steps and the HA glue."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import autotune as at
from custom_components.roomwise import autotune_ha
from custom_components.roomwise.autotune_ha import Discovery
from custom_components.roomwise.const import (
    BDADDR_TYPE_OTHER,
    BDADDR_TYPE_UNKNOWN,
    CONF_DEVICES,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SCANNER_MAX_RADIUS,
    DOMAIN_PRIVATE_BLE_DEVICE,
)

from .test_autotune import DEFAULT, SCANNERS, make_house

SETTINGS = at.Settings(ref_power=-55.0, attenuation=3.0, max_radius=10.0, offsets={})
KITCHEN = at.Scanner("scn_kitchen", "Kitchen", "Kitchen proxy")
NO_ROOM = at.Scanner("scn_noroom", None, "Spare proxy")


# --------------------------------------------------------------------------
# autotune.py
# --------------------------------------------------------------------------


def _result(area="Kitchen", *, scored=True, before_ok=False, after_ok=False, outsider=False, heard=None):
    return at.LandmarkResult(
        at.Landmark("lm", area, "Thing"),
        scored,
        "" if scored else "why not",
        before_ok=before_ok,
        after_ok=after_ok,
        heard_by=heard or {},
        outsider=outsider,
    )


def _report(results, current=SETTINGS, proposed=None, **kw):
    return at.TuneReport(
        current=current,
        proposed=proposed or current,
        results=results,
        scored=len([r for r in results if r.scored]),
        right_before=0,
        right_after=0,
        recommend=kw.pop("recommend", False),
        summary="",
        **kw,
    )


def test_regressions_are_the_devices_that_were_right_and_are_now_wrong():
    broke = _result(before_ok=True, after_ok=False)
    fixed = _result(before_ok=False, after_ok=True)
    same = _result(before_ok=True, after_ok=True)
    report = _report([broke, fixed, same])
    assert report.regressions == [broke]
    assert report.fixes == [fixed]


def test_prediction_and_holding_ignore_receivers_that_are_unknown_or_have_no_room():
    scanners = {KITCHEN.id: KITCHEN, NO_ROOM.id: NO_ROOM}
    heard = {"ghost": -40.0, NO_ROOM.id: -40.0, KITCHEN.id: -60.0}
    assert at.predict_area(heard, scanners, SETTINGS) == ("Kitchen", KITCHEN.id)
    assert at.holding_scanner(heard, scanners, SETTINGS) == KITCHEN.id
    assert at.predict_area({"ghost": -40.0, NO_ROOM.id: -40.0}, scanners, SETTINGS) == (None, None)


def test_readings_with_no_signal_value_are_left_out_of_the_medians():
    samples = [at.Sample("lm", "scn", None, float(i)) for i in range(5)]
    samples += [at.Sample("lm", "scn", -60.0, float(i)) for i in range(5)]
    assert at.aggregate(samples) == {"lm": {"scn": -60.0}}


def test_training_cost_ignores_receivers_that_are_unknown_or_have_no_room():
    scanners = {KITCHEN.id: KITCHEN, NO_ROOM.id: NO_ROOM}
    heard = {"ghost": -30.0, NO_ROOM.id: -30.0, KITCHEN.id: -60.0}
    cost = at._landmark_cost(heard, "Kitchen", scanners, SETTINGS, margin=4.0, radius_margin=2.0)
    # only the kitchen receiver counts: right room, nothing to be beaten by, well inside range
    assert cost == 0.0


def test_a_fractional_current_offset_stays_in_the_search_range():
    values = at._offset_range(2.5, 3)
    assert 2.5 in values
    assert values == sorted(values)
    assert values[0] == -3
    assert values[-1] == 3


def test_fit_with_nothing_usable_keeps_the_current_settings():
    landmarks = [at.Landmark("no_room", None, "Unplaced"), at.Landmark("not_heard", "Kitchen", "Silent")]
    current = at.Settings(-55.0, 3.0, 10.0, {KITCHEN.id: 2})
    best = at.fit({}, landmarks, [KITCHEN, NO_ROOM], current)
    assert best.offsets == {KITCHEN.id: 2, NO_ROOM.id: 0}
    assert best.ref_power == current.ref_power


def test_many_devices_are_checked_in_folds_and_empty_folds_are_skipped():
    landmarks, samples = make_house({}, per_room=2)
    options = at.TuneOptions(loo_max_landmarks=1, folds=10, max_passes=2)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT, options)
    assert len(landmarks) == 8  # fewer devices than folds, so some folds are empty
    assert all(r.after_area is not None for r in report.results if r.scored)
    assert "Because many devices were chosen" in report.summary


def test_a_room_without_a_receiver_that_is_left_alone_needs_no_note():
    outsider = _result("Attic", scored=False, after_ok=True, outsider=True, heard={KITCHEN.id: -90.0})
    report = _report([outsider])
    assert at.explain(report, {KITCHEN.id: KITCHEN}, SETTINGS, {}) == []


def test_unchecked_devices_are_explained_with_every_reason_that_applies():
    outsider = _result("Attic", scored=False, outsider=True)
    no_room = _result(None, scored=False)
    silent = _result("Kitchen", scored=False)
    text = at._why_not_checked([outsider, no_room, silent])
    assert text == "no receiver in the room, no room set, or not heard"
    assert at._why_not_checked([silent]) == "not heard"
    assert at._why_not_checked([no_room, silent]) == "no room set or not heard"


def test_a_changed_reference_power_or_attenuation_counts_as_a_change():
    base = at.Settings(-55.0, 3.0, 10.0, {"a": 1})
    assert at._settings_changed(base, base.copy()) is False
    assert at._settings_changed(base, at.Settings(-60.0, 3.0, 10.0, {"a": 1})) is True
    assert at._settings_changed(base, at.Settings(-55.0, 2.5, 10.0, {"a": 1})) is True


def test_the_table_lists_a_changed_reference_power():
    current = at.Settings(-55.0, 3.0, 10.0, {"a": 0})
    proposed = at.Settings(-60.0, 3.0, 10.0, {"a": 2})
    report = _report([_result(before_ok=False, after_ok=True)], current, proposed, recommend=True, tuned_ref_power=True)
    table = at.format_table(report, {"a": "Hall proxy"})
    assert "| Hall proxy | 0 | 2 |" in table
    assert "| (reference power) | -55 | -60 | | |" in table


# --------------------------------------------------------------------------
# autotune_flow.py
# --------------------------------------------------------------------------


def _flow(hass, entry):
    from custom_components.roomwise.config_flow import BermudaOptionsFlowHandler

    flow = BermudaOptionsFlowHandler()
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "test_flow"
    flow.coordinator = entry.runtime_data.coordinator
    flow._autotune_found = None
    flow._autotune_landmarks = []
    flow._autotune_minutes = 1
    flow._autotune_export = False
    flow._autotune_task = None
    return flow


async def test_room_names_are_looked_up_for_rooms_the_scan_did_not_name(hass, setup_bermuda_entry):
    kitchen = ar.async_get(hass).async_create("Kitchen")
    flow = _flow(hass, setup_bermuda_entry)
    flow._autotune_found = Discovery()
    flow._autotune_landmarks = [at.Landmark("11:22:33:44:55:66", kitchen.id, "Kettle")]

    async def forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    with patch("custom_components.roomwise.autotune_flow.async_run_autotune", side_effect=forever) as run:
        result = await flow.async_step_autotune_collect()

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert flow._autotune_found.area_names == {kitchen.id: "Kitchen"}
    flow._autotune_task.cancel()
    assert run.called


async def test_a_result_step_without_a_report_aborts(hass, setup_bermuda_entry):
    flow = _flow(hass, setup_bermuda_entry)
    flow._autotune_report = None
    result = await flow.async_step_autotune_result()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "autotune_failed"


async def test_reset_with_nothing_saved_says_so(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    hass.config_entries.async_update_entry(setup_bermuda_entry, options={})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(setup_bermuda_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "autotune_reset"}
    )
    assert result["step_id"] == "autotune_reset"
    assert result["description_placeholders"]["radius_lines"] == "- none"
    assert result["description_placeholders"]["offset_lines"] == "- none (all 0)"


# --------------------------------------------------------------------------
# autotune_ha.py
# --------------------------------------------------------------------------


def test_an_address_of_unknown_type_is_never_a_fixed_one():
    device = SimpleNamespace(is_scanner=False, address_type=BDADDR_TYPE_UNKNOWN, metadevice_type=set())
    assert autotune_ha._fixed_address(device, 0.0) is False


def _fake_device(address, address_type=BDADDR_TYPE_OTHER, is_scanner=False, area_id=None, wifi=None):
    return SimpleNamespace(
        address=address,
        address_type=address_type,
        is_scanner=is_scanner,
        area_id=area_id,
        name=address,
        address_ble_mac=address,
        address_wifi_mac=wifi,
        adverts={},
    )


def _coordinator(devices, scanners=()):
    return SimpleNamespace(
        devices=devices,
        scanner_list=set(scanners),
        options={CONF_DEVICES: []},
        config_entry=None,
    )


async def test_discovery_skips_private_ble_devices_scanner_twins_and_repeats(hass: HomeAssistant):
    kitchen = ar.async_get(hass).async_create("Kitchen")
    entry = MockConfigEntry(domain="test_other")
    entry.add_to_hass(hass)
    devreg = dr.async_get(hass)

    def add(name, bt, identifiers=None, area=kitchen, kind=dr.CONNECTION_BLUETOOTH):
        dev = devreg.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(kind, bt)},
            identifiers=identifiers or set(),
            name=name,
        )
        devreg.async_update_device(dev.id, area_id=area.id)

    add("Phone", "11:11:11:11:11:01", identifiers={(DOMAIN_PRIVATE_BLE_DEVICE, "phone")})
    add("Proxy twin", "11:11:11:11:11:51")  # the Bluetooth side of a proxy the scan lists by its other address
    add("Plug", "11:11:11:11:11:61")
    # a second registry entry (known by its network address) that resolves to the same radio address
    add("Plug again", "11:11:11:11:11:60", kind=dr.CONNECTION_NETWORK_MAC)

    proxy = "22:22:22:22:22:01"
    coordinator = _coordinator(
        {
            proxy: _fake_device(proxy, is_scanner=True, area_id=kitchen.id, wifi="11:11:11:11:11:51"),
            "11:11:11:11:11:01": _fake_device("11:11:11:11:11:01"),
            "11:11:11:11:11:51": _fake_device("11:11:11:11:11:51"),
            "11:11:11:11:11:61": _fake_device("11:11:11:11:11:61"),
        },
        scanners=[proxy],
    )

    found = autotune_ha.discover(hass, coordinator)

    assert ("Phone", "private Bluetooth device (rotating address)") in found.skipped
    assert [lm.id for lm in found.landmarks] == ["11:11:11:11:11:61"]  # one plug, once
    assert not any(name == "Proxy twin" for name, _why in found.skipped)  # a scanner, not a landmark
    assert found.area_names[kitchen.id] == "Kitchen"


def test_a_tuned_reference_power_is_saved_with_the_offsets():
    current = at.Settings(-55.0, 3.0, 10.0, {"a": 0})
    proposed = at.Settings(-62.0, 3.0, 10.0, {"a": 1})
    report = _report([], current, proposed, recommend=True, tuned_ref_power=True)
    out = autotune_ha.settings_to_options(report, {})
    assert out[CONF_REF_POWER] == -62.0
    assert out[CONF_RSSI_OFFSETS] == {"a": 1}
    assert CONF_SCANNER_MAX_RADIUS not in out
    # an untuned ref_power is never written
    report.tuned_ref_power = False
    assert CONF_REF_POWER not in autotune_ha.settings_to_options(report, {})


def test_the_collector_skips_addresses_the_coordinator_has_not_heard():
    heard = SimpleNamespace(
        adverts={"k": SimpleNamespace(rssi=-60, stamp=5.0, scanner_address="scn")},
    )
    coordinator = SimpleNamespace(devices={"aa:aa:aa:aa:aa:01": heard}, async_add_listener=lambda cb: lambda: None)
    collector = autotune_ha.SampleCollector(coordinator, ["AA:AA:AA:AA:AA:01", "aa:aa:aa:aa:aa:02"])
    collector.poll()
    assert [(s.landmark, s.scanner, s.rssi) for s in collector.samples] == [("aa:aa:aa:aa:aa:01", "scn", -60.0)]

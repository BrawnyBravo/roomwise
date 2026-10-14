"""Tests for the Auto-tune rooms options-flow step and its Home Assistant glue."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import autotune as at
from custom_components.roomwise import autotune_ha
from custom_components.roomwise.autotune_ha import Discovery
from custom_components.roomwise.const import (
    ADDR_TYPE_IBEACON,
    BDADDR_TYPE_OTHER,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SCANNER_MAX_RADIUS,
    NAME,
)

SCN_A = "aa:aa:aa:aa:aa:01"
SCN_B = "aa:aa:aa:aa:aa:02"


def _report(recommend: bool) -> at.TuneReport:
    current = at.Settings(-55.0, 3.0, 20.0, {SCN_A: 0, SCN_B: 0})
    proposed = at.Settings(-55.0, 3.0, 20.0, {SCN_A: 3, SCN_B: -6}) if recommend else current.copy()
    lm = at.Landmark("11:22:33:44:55:66", "kitchen", "Kettle")
    res = at.LandmarkResult(lm, True, "", "office", "kitchen" if recommend else "office", False, recommend)
    return at.TuneReport(
        current=current,
        proposed=proposed,
        results=[res],
        scored=1,
        right_before=0,
        right_after=1 if recommend else 0,
        recommend=recommend,
        summary="1 of 1 known devices land in the right room (was 0 of 1)." if recommend else "already as good",
    )


def _discovery() -> Discovery:
    return Discovery(
        landmarks=[at.Landmark("11:22:33:44:55:66", "kitchen", "Kettle")],
        scanners=[at.Scanner(SCN_A, "kitchen", "Kitchen proxy"), at.Scanner(SCN_B, "office", "Office proxy")],
        area_names={"kitchen": "Kitchen", "office": "Office"},
    )


async def _start_autotune(hass: HomeAssistant, entry: MockConfigEntry, report_or_exc, export=False):
    seen = {}

    async def fake_run(hass_, coordinator, options, seconds, landmarks, scanners, area_names=None, export_path=None):
        seen.update(seconds=seconds, landmarks=landmarks, export_path=export_path)
        if isinstance(report_or_exc, Exception):
            raise report_or_exc
        return report_or_exc

    cands = [
        autotune_ha.Candidate("11:22:33:44:55:66", "Kettle", "kitchen", True),
        autotune_ha.Candidate("11:22:33:44:55:77", "Lamp [11:22:33:44:55:77]", None, False),
    ]
    with (
        patch("custom_components.roomwise.autotune_flow.discover", return_value=_discovery()),
        patch("custom_components.roomwise.autotune_flow.candidates", return_value=cands),
        patch("custom_components.roomwise.autotune_flow.async_run_autotune", side_effect=fake_run),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.MENU
        assert "autotune" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "autotune"}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "autotune"
        assert result["description_placeholders"]["candidate_count"] == "2"
        assert result["description_placeholders"]["suggested_count"] == "1"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "autotune_selected": ["11:22:33:44:55:66", "11:22:33:44:55:77"],
                "autotune_minutes": 2,
                "autotune_export": export,
            },
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "autotune_rooms"
        keys = [str(k) for k in result["data_schema"].schema]
        assert keys == ["Kettle", "Lamp [11:22:33:44:55:77]"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"Kettle": "kitchen", "Lamp [11:22:33:44:55:77]": "office"}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS, result
        assert result["step_id"] == "autotune_collect"
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert seen["seconds"] == 120
    assert {lm.id: lm.area for lm in seen["landmarks"]} == {
        "11:22:33:44:55:66": "kitchen",
        "11:22:33:44:55:77": "office",
    }
    assert (seen["export_path"] is not None) == export
    return result


async def test_autotune_apply_writes_offsets(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _start_autotune(hass, setup_bermuda_entry, _report(True))
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "autotune_result"
    assert "was 0 of 1" in result["description_placeholders"]["summary"]
    table = result["description_placeholders"]["table"]
    assert "| Kitchen |" in table  # room names, not area ids
    assert "Office proxy" in table
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"autotune_apply": True})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert setup_bermuda_entry.options[CONF_RSSI_OFFSETS] == {SCN_A: 3, SCN_B: -6}
    # the confirmed landmarks are remembered for next time
    assert setup_bermuda_entry.options[autotune_ha.CONF_AUTOTUNE_LANDMARKS] == {
        "11:22:33:44:55:66": "kitchen",
        "11:22:33:44:55:77": "office",
    }


async def test_autotune_export_option_passes_a_path(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _start_autotune(hass, setup_bermuda_entry, _report(True), export=True)
    assert result["step_id"] == "autotune_result"


async def test_autotune_cancel_changes_nothing(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    before = dict(setup_bermuda_entry.options)
    result = await _start_autotune(hass, setup_bermuda_entry, _report(True))
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={"autotune_apply": False})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "autotune_not_applied"
    assert dict(setup_bermuda_entry.options) == before


async def test_autotune_no_improvement_offers_no_apply(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    before = dict(setup_bermuda_entry.options)
    result = await _start_autotune(hass, setup_bermuda_entry, _report(False))
    assert result["step_id"] == "autotune_result"
    assert "autotune_apply" not in [str(k) for k in result["data_schema"].schema]
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={})
    assert result["type"] == FlowResultType.ABORT
    assert dict(setup_bermuda_entry.options) == before


async def test_autotune_failure_aborts(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _start_autotune(hass, setup_bermuda_entry, RuntimeError("boom"))
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "autotune_failed"


def test_settings_to_options_matches_manual_calibration_shape():
    report = _report(True)
    out = autotune_ha.settings_to_options(report, {CONF_RSSI_OFFSETS: {"cc:cc:cc:cc:cc:cc": 4}})
    # Untouched scanners keep their offsets; tuned ones are ints, clipped like calibration2.
    assert out == {CONF_RSSI_OFFSETS: {"cc:cc:cc:cc:cc:cc": 4, SCN_A: 3, SCN_B: -6}}
    assert CONF_REF_POWER not in out
    report.proposed.radii = {SCN_A: 3.0}
    out = autotune_ha.settings_to_options(report, {})
    assert out[CONF_SCANNER_MAX_RADIUS] == {SCN_A: 3.0}


def test_current_settings_reads_options():
    s = autotune_ha.current_settings(
        {CONF_MAX_RADIUS: 6, CONF_RSSI_OFFSETS: {SCN_A: -4}, CONF_SCANNER_MAX_RADIUS: {SCN_B: 3}}, [SCN_A, SCN_B]
    )
    assert s.radius(SCN_B) == 3.0 and s.radius(SCN_A) == 6.0
    assert (s.ref_power, s.attenuation, s.max_radius) == (-55.0, 3.0, 6.0)
    assert s.offsets == {SCN_A: -4, SCN_B: 0}


# --------------------------------------------------------------------------
# Discovery and collection against a real device registry and a fake coordinator
# --------------------------------------------------------------------------


def _fake_device(address, address_type=BDADDR_TYPE_OTHER, is_scanner=False, area_id=None, name=None):
    return SimpleNamespace(
        address=address,
        address_type=address_type,
        is_scanner=is_scanner,
        area_id=area_id,
        name=name or address,
        address_ble_mac=address,
        address_wifi_mac=None,
        adverts={},
    )


@pytest.fixture
async def house(hass: HomeAssistant):
    """A registry with a few kinds of devices, and a coordinator-like object that has heard some of them."""
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    office = areas.async_create("Office")
    other_entry = MockConfigEntry(domain="test_other")
    other_entry.add_to_hass(hass)
    our_entry = MockConfigEntry(domain="roomwise", entry_id="ours")
    our_entry.add_to_hass(hass)
    devreg = dr.async_get(hass)

    def add(name, conns, area=None, entry=other_entry, model=None):
        dev = devreg.async_get_or_create(config_entry_id=entry.entry_id, connections=conns, name=name, model=model)
        if area is not None:
            devreg.async_update_device(dev.id, area_id=area.id)
        return dev

    add("Kettle", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:01")}, kitchen)
    add("Desk lamp", {(dr.CONNECTION_NETWORK_MAC, "22:22:22:22:22:00")}, office)  # BLE = WiFi + 1
    add("Pocket phone", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:02")}, kitchen, model="Phone 9")
    add("Unplaced plug", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:03")})
    add("Rotating tag", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:04")}, kitchen)
    add("Beacon", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:05")}, office)
    walker = add("Walker", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:06")}, office)
    add("Tracked by us", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:07")}, office)
    add("Silent plug", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:08")}, office)
    add("Roomwise own", {(dr.CONNECTION_BLUETOOTH, "11:11:11:11:11:09")}, office, entry=our_entry)
    er.async_get(hass).async_get_or_create(
        "device_tracker", "test_other", "walker_tracker", config_entry=other_entry, device_id=walker.id
    )

    devices = {
        SCN_A: _fake_device(SCN_A, is_scanner=True, area_id=kitchen.id, name="Kitchen proxy"),
        SCN_B: _fake_device(SCN_B, is_scanner=True, area_id=office.id, name="Office proxy"),
        "11:11:11:11:11:01": _fake_device("11:11:11:11:11:01"),
        "22:22:22:22:22:01": _fake_device("22:22:22:22:22:01"),
        "11:11:11:11:11:02": _fake_device("11:11:11:11:11:02"),
        "11:11:11:11:11:03": _fake_device("11:11:11:11:11:03"),
        "11:11:11:11:11:04": _fake_device("11:11:11:11:11:04", BDADDR_TYPE_RANDOM_RESOLVABLE),
        "11:11:11:11:11:05": _fake_device("11:11:11:11:11:05", ADDR_TYPE_IBEACON),
        "11:11:11:11:11:06": _fake_device("11:11:11:11:11:06"),
        "11:11:11:11:11:07": _fake_device("11:11:11:11:11:07"),
        "11:11:11:11:11:09": _fake_device("11:11:11:11:11:09"),
    }
    listeners = []

    def add_listener(cb):
        listeners.append(cb)
        return lambda: listeners.remove(cb)

    coordinator = SimpleNamespace(
        devices=devices,
        scanner_list={SCN_A, SCN_B},
        options={CONF_DEVICES: ["11:11:11:11:11:07"]},
        config_entry=our_entry,
        async_add_listener=add_listener,
        listeners=listeners,
    )
    return SimpleNamespace(coordinator=coordinator, kitchen=kitchen, office=office)


async def test_discover_picks_stationary_devices_with_rooms(hass: HomeAssistant, house):
    found = autotune_ha.discover(hass, house.coordinator)
    assert {lm.name: lm.id for lm in found.landmarks} == {
        "Kettle": "11:11:11:11:11:01",
        "Desk lamp": "22:22:22:22:22:01",
    }
    assert {lm.id: lm.area for lm in found.landmarks}["11:11:11:11:11:01"] == house.kitchen.id
    skipped = dict(found.skipped)
    assert skipped["Pocket phone"] == "looks like something that moves"
    assert skipped["Unplaced plug"] == "no room set"
    assert skipped["Walker"] == "has a device tracker, so it moves"
    assert skipped["Tracked by us"] == "tracked by Roomwise as a moving device"
    assert skipped["Silent plug"] == "not heard by any scanner"
    # rotating addresses and iBeacons are never usable
    assert skipped["Rotating tag"] == "rotating or beacon address"
    assert skipped["Beacon"] == "rotating or beacon address"
    assert "Roomwise own" not in skipped
    assert {s.id: s.area for s in found.scanners} == {SCN_A: house.kitchen.id, SCN_B: house.office.id}
    assert found.area_names[house.kitchen.id] == "Kitchen"


async def test_collector_records_each_new_advert_once(hass: HomeAssistant, house):
    coord = house.coordinator
    kettle = coord.devices["11:11:11:11:11:01"]
    adv = SimpleNamespace(scanner_address=SCN_A, rssi=-60, stamp=100.0)
    kettle.adverts = {SCN_A: adv}
    collector = autotune_ha.SampleCollector(coord, ["11:11:11:11:11:01"])
    collector.start()
    assert len(coord.listeners) == 1
    for listener in list(coord.listeners):
        listener()  # same stamp again: not a new reading
    adv.stamp, adv.rssi = 101.0, -62
    for listener in list(coord.listeners):
        listener()
    collector.stop()
    assert coord.listeners == []
    assert [(s.scanner, s.rssi, s.t) for s in collector.samples] == [(SCN_A, -60.0, 100.0), (SCN_A, -62.0, 101.0)]


async def test_run_autotune_end_to_end_with_fake_coordinator(hass: HomeAssistant, house, tmp_path):
    coord = house.coordinator
    kettle = coord.devices["11:11:11:11:11:01"]
    kettle.adverts = {
        SCN_A: SimpleNamespace(scanner_address=SCN_A, rssi=-60, stamp=1.0),
        SCN_B: SimpleNamespace(scanner_address=SCN_B, rssi=-70, stamp=1.0),
    }
    found = autotune_ha.discover(hass, coord)
    export = tmp_path / "samples.json"
    report = await autotune_ha.async_run_autotune(
        hass, coord, {}, 0, found.landmarks, found.scanners, found.area_names, str(export)
    )
    # one reading per pair is below the minimum, so nothing is scored and nothing recommended
    assert report.scored == 0
    assert not report.recommend
    # the export can be loaded back for an offline replay
    landmarks, scanners, settings, samples = at.load_export(json.loads(export.read_text()))
    assert {lm.id for lm in landmarks} == {"11:11:11:11:11:01", "22:22:22:22:22:01"}
    assert {(s.landmark, s.scanner, s.rssi) for s in samples} == {
        ("11:11:11:11:11:01", SCN_A, -60.0),
        ("11:11:11:11:11:01", SCN_B, -70.0),
    }
    assert settings.max_radius == 20.0


async def test_candidates_list_fixed_steady_addresses(hass: HomeAssistant, house):
    coord = house.coordinator
    now = monotonic_time_coarse()
    for dev in coord.devices.values():
        dev.last_seen = now
        dev.adverts = {SCN_A: SimpleNamespace(hist_rssi=[-60, -61, -60])}
    coord.devices["11:11:11:11:11:03"].adverts = {SCN_A: SimpleNamespace(hist_rssi=[-60])}  # too few readings
    coord.devices["11:11:11:11:11:06"].last_seen = now - 3600  # not heard lately
    coord.devices["33:33:33:33:33:33"] = _fake_device("33:33:33:33:33:33", name="Fridge")
    coord.devices["33:33:33:33:33:33"].last_seen = now
    coord.devices["33:33:33:33:33:33"].adverts = {SCN_B: SimpleNamespace(hist_rssi=[-70] * 5)}
    found = autotune_ha.discover(hass, coord)
    cands = autotune_ha.candidates(coord, found, saved={"44:44:44:44:44:44": house.office.id})
    by_addr = {c.address: c for c in cands}
    # registry pre-fill is ticked, with its room; the guessed one says so
    assert by_addr["11:11:11:11:11:01"].suggested and by_addr["11:11:11:11:11:01"].area_id == house.kitchen.id
    assert "please check" in by_addr["22:22:22:22:22:01"].label
    # heard steadily but unknown to the registry: offered, not ticked
    assert not by_addr["33:33:33:33:33:33"].suggested
    assert "Fridge" in by_addr["33:33:33:33:33:33"].label
    # saved earlier, not heard now: still offered and ticked
    assert by_addr["44:44:44:44:44:44"].suggested
    assert by_addr["44:44:44:44:44:44"].area_id == house.office.id
    # never offered: scanners, rotating/beacon addresses, tracked devices, too few readings, stale
    for addr in (
        SCN_A,
        SCN_B,
        "11:11:11:11:11:04",
        "11:11:11:11:11:05",
        "11:11:11:11:11:07",
        "11:11:11:11:11:03",
        "11:11:11:11:11:06",
    ):
        assert addr not in by_addr, addr
    # ticked ones come first
    assert [c.suggested for c in cands] == sorted([c.suggested for c in cands], reverse=True)

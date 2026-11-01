"""Config flow and options flow paths: discovery, menu status, device picker, calibration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise.config_flow import BermudaOptionsFlowHandler
from custom_components.roomwise.const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_REF_POWER,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_INFO,
    CONF_SCANNERS,
    DOMAIN,
    NAME,
)

from .const import MOCK_CONFIG


def _flow(hass: HomeAssistant, coordinator) -> BermudaOptionsFlowHandler:
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME)
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    flow = BermudaOptionsFlowHandler(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.coordinator = coordinator
    return flow


def _coordinator(scanners=(), devices=None, active_devices=0, summary=()):
    scanners = list(scanners)
    all_devices = {s.address: s for s in scanners}
    all_devices.update(devices or {})
    return SimpleNamespace(
        devices=all_devices,
        scanner_list=[s.address for s in scanners],
        count_active_devices=lambda: active_devices,
        count_active_scanners=lambda: len(scanners),
        get_active_scanner_summary=lambda: list(summary),
    )


def _scanner(n: int, name: str, hist) -> SimpleNamespace:
    return SimpleNamespace(address=f"aa:bb:cc:dd:ee:0{n}", name=name, last_seen=monotonic_time_coarse(), hist_rssi=hist)


# --- initial set-up flow ---------------------------------------------------


async def test_bluetooth_discovery_asks_for_confirmation_once(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=SimpleNamespace()
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # A second discovery of the same thing does not start a second flow.
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=SimpleNamespace()
    )
    assert again["type"] == FlowResultType.ABORT
    assert again["reason"] == "already_in_progress"


async def test_bluetooth_discovery_is_ignored_when_already_set_up(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_BLUETOOTH}, data=SimpleNamespace()
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_manual_setup_is_refused_when_already_set_up(hass: HomeAssistant):
    MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


# --- options menu status ----------------------------------------------------


async def test_menu_warns_when_scanners_hear_no_devices(hass: HomeAssistant):
    scanner = _scanner(1, "Hall Proxy", [])
    flow = _flow(hass, _coordinator([scanner], active_devices=0))
    result = await flow.async_step_init()
    assert "No bluetooth devices are actively being reported" in result["description_placeholders"]["status"]


async def test_menu_scanner_table_marks_fresh_slow_and_dead_scanners(hass: HomeAssistant):
    summary = [
        {"name": "Fresh", "address": "aa:bb:cc:dd:ee:01", "last_stamp_age": 0.5},
        {"name": "Slow", "address": "aa:bb:cc:dd:ee:02", "last_stamp_age": 5.0},
        {"name": "Dead", "address": "aa:bb:cc:dd:ee:03", "last_stamp_age": 60.0},
    ]
    flow = _flow(hass, _coordinator([_scanner(1, "Fresh", [])], active_devices=3, summary=summary))
    result = await flow.async_step_init()
    status = result["description_placeholders"]["status"]
    assert "at least some active devices" in status
    rows = {line.split("|")[1].strip(): line for line in status.splitlines() if line.startswith("| ")}
    assert "mdi:check-circle-outline" in rows["Fresh"]
    assert "mdi:alert-outline" in rows["Slow"]
    assert "mdi:skull-crossbones" in rows["Dead"]
    assert "aa::" in rows["Fresh"]  # only the outer octets of the address are shown
    assert "bb:cc" not in rows["Fresh"]


# --- device picker ------------------------------------------------------------


def _device(address, name, address_type=None, *, is_scanner=False, sources=(), age=0):
    return SimpleNamespace(
        address=address,
        name=name,
        address_type=address_type,
        is_scanner=is_scanner,
        metadevice_sources=list(sources),
        last_seen=monotonic_time_coarse() - age,
    )


async def test_device_picker_lists_preferred_devices_first_and_skips_the_rest(hass: HomeAssistant):
    beacon1 = "e2c56db5dffb48d2b060d0f5a71096e0_1_2"
    beacon2 = "e2c56db5dffb48d2b060d0f5a71096e0_3_4"
    devices = {
        "s": _device("aa:bb:cc:00:00:01", "A Scanner", is_scanner=True),
        "p": _device("5eedc0de0123456789abcdef01234567", "Phone IRK", ADDR_TYPE_PRIVATE_BLE_DEVICE),
        "b1": _device(beacon1, "Beacon One", ADDR_TYPE_IBEACON, sources=["aa:bb:cc:00:00:09"]),
        "b2": _device(beacon2, beacon2, ADDR_TYPE_IBEACON),
        "r1": _device("aa:bb:cc:00:00:20", "Recent Random", BDADDR_TYPE_RANDOM_RESOLVABLE, age=60),
        "r2": _device("aa:bb:cc:00:00:21", "Old Random", BDADDR_TYPE_RANDOM_RESOLVABLE, age=3 * 60 * 60),
        "n": _device("aa:bb:cc:00:00:30", "Plain Tag"),
    }
    flow = _flow(hass, _coordinator(devices=devices))
    flow.options[CONF_DEVICES] = ["AA:BB:CC:00:00:30", "AA:BB:CC:00:00:99"]

    result = await flow.async_step_selectdevices()

    assert result["type"] == FlowResultType.FORM
    selector = next(iter(result["data_schema"].schema.values()))
    options = selector.config["options"]
    assert [o["value"] for o in options] == [
        beacon1.upper(),
        beacon2.upper(),
        "AA:BB:CC:00:00:30",
        "AA:BB:CC:00:00:20",
        "AA:BB:CC:00:00:99",  # configured but not currently seen
    ]
    labels = {o["value"]: o["label"] for o in options}
    assert "[AA:BB:CC:00:00:09]" in labels[beacon1.upper()]
    assert "Beacon One" in labels[beacon1.upper()]
    assert labels["AA:BB:CC:00:00:20"].endswith("(Random MAC)")
    assert labels["AA:BB:CC:00:00:99"].endswith("(saved)")


async def test_device_picker_saves_the_chosen_devices(hass: HomeAssistant):
    flow = _flow(hass, _coordinator())
    result = await flow.async_step_selectdevices(user_input={CONF_DEVICES: ["AA:BB:CC:00:00:30"]})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICES] == ["AA:BB:CC:00:00:30"]


# --- calibration 1 --------------------------------------------------------------


def _calibration1_input(**overrides):
    base = {
        CONF_DEVICES: "registry_id",
        CONF_SCANNERS: "aa:bb:cc:dd:ee:01",
        CONF_REF_POWER: -55.0,
        CONF_ATTENUATION: 3.0,
        CONF_SAVE_AND_CLOSE: False,
    }
    return base | overrides


async def test_calibration1_shows_the_form_first(hass: HomeAssistant):
    flow = _flow(hass, _coordinator([_scanner(1, "Scanner A", [-60])]))
    result = await flow.async_step_calibration1_global()
    assert result["type"] == FlowResultType.FORM
    assert "After you click Submit" in result["description_placeholders"]["suffix"]


async def test_calibration1_submit_shows_distances_for_the_chosen_values(hass: HomeAssistant):
    flow = _flow(hass, _coordinator([_scanner(1, "Scanner A", [-55, -65, -75])]))
    device = SimpleNamespace(name="Tag One", get_scanner=lambda address: flow.coordinator.devices[address])
    with patch.object(BermudaOptionsFlowHandler, "_get_bermuda_device_from_registry", return_value=device):
        result = await flow.async_step_calibration1_global(user_input=_calibration1_input())

    assert result["type"] == FlowResultType.FORM
    suffix = result["description_placeholders"]["suffix"]
    assert "ref_power = -55.0" in suffix
    assert "attenuation = 3.0" in suffix
    # 10 ** ((ref - rssi) / (10 * attenuation)): 1.00 m at the reference power, 2.15 m 10 dB below it.
    assert "| Tag One | 0 | 1 | 2 |" in suffix
    assert "` 1.00`" in suffix
    assert "` 2.15`" in suffix
    assert "`  -55`" in suffix
    # The values are remembered so the form re-opens with them.
    assert flow._last_ref_power == -55.0
    assert flow._last_scanner == "aa:bb:cc:dd:ee:01"


async def test_calibration1_save_and_close_stores_the_chosen_values(hass: HomeAssistant):
    flow = _flow(hass, _coordinator([_scanner(1, "Scanner A", [])]))
    result = await flow.async_step_calibration1_global(
        user_input=_calibration1_input(**{CONF_SAVE_AND_CLOSE: True, CONF_REF_POWER: -60.0, CONF_ATTENUATION: 2.5})
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_REF_POWER] == -60.0
    assert result["data"][CONF_ATTENUATION] == 2.5


async def test_calibration1_without_a_known_device_shows_the_form_again(hass: HomeAssistant):
    flow = _flow(hass, _coordinator([_scanner(1, "Scanner A", [])]))
    with patch.object(BermudaOptionsFlowHandler, "_get_bermuda_device_from_registry", return_value=None):
        result = await flow.async_step_calibration1_global(user_input=_calibration1_input())
    assert result["type"] == FlowResultType.FORM
    assert not result.get("errors")


# --- calibration 2 --------------------------------------------------------------


async def test_calibration2_results_ignore_an_offset_that_is_not_a_number(hass: HomeAssistant):
    scanners = [_scanner(1, "Scanner A", [-55]), _scanner(2, "Scanner B", [-55])]
    flow = _flow(hass, _coordinator(scanners))
    device = SimpleNamespace(get_scanner=lambda address: flow.coordinator.devices[address])
    with patch.object(BermudaOptionsFlowHandler, "_get_bermuda_device_from_registry", return_value=device):
        result = await flow.async_step_calibration2_scanners(
            user_input={
                CONF_DEVICES: "registry_id",
                CONF_SCANNER_INFO: {"Scanner A": "loud", "Scanner B": 10},
                CONF_SAVE_AND_CLOSE: False,
            }
        )
    assert result["type"] == FlowResultType.FORM
    rows = {
        line.split("|")[1]: line
        for line in result["description_placeholders"]["suffix"].splitlines()
        if line.startswith("|Scanner")
    }
    # -55 dBm at the default reference power is 1 m; the bad offset counts as 0, the good one moves it.
    assert "  1.00" in rows["Scanner A"]
    assert "  1.00" not in rows["Scanner B"]


# --- device lookup ----------------------------------------------------------------


async def test_registry_device_without_a_bluetooth_address_is_not_a_roomwise_device(hass: HomeAssistant):
    flow = _flow(hass, _coordinator())
    other = MockConfigEntry(domain="test_other")
    other.add_to_hass(hass)
    reg_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:99")}
    )
    assert flow._get_bermuda_device_from_registry(reg_device.id) is None
    assert flow._get_bermuda_device_from_registry("no-such-device-id") is None

"""Global options are range-checked, and a bad saved value cannot crash the maths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise.autotune import estimated_distance
from custom_components.roomwise.autotune_ha import current_settings
from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.config_flow import BermudaOptionsFlowHandler
from custom_components.roomwise.const import (
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_REF_POWER,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_MAX_RADIUS,
    CONF_SCANNERS,
    CONF_SMOOTHING_SAMPLES,
    DEFAULT_ATTENUATION,
    DEFAULT_MAX_RADIUS,
    DOMAIN,
    NAME,
)
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

from .const import MOCK_CONFIG, MOCK_OPTIONS_GLOBALS


async def _globalopts(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "globalopts"}
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (CONF_ATTENUATION, 0),
        (CONF_ATTENUATION, -2),
        (CONF_MAX_RADIUS, 0),
        (CONF_MAX_RADIUS, -1),
        (CONF_SMOOTHING_SAMPLES, -3),
        (CONF_SMOOTHING_SAMPLES, 0),
        (CONF_REF_POWER, 10),
    ],
)
async def test_global_options_reject_values_that_break_the_maths(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry: MockConfigEntry, key, value
):
    result = await _globalopts(hass, setup_bermuda_entry)
    assert result["type"] == FlowResultType.FORM
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input=MOCK_OPTIONS_GLOBALS | {key: value}
        )
    assert setup_bermuda_entry.options.get(key) != value


async def test_global_options_accept_sensible_values(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry: MockConfigEntry
):
    result = await _globalopts(hass, setup_bermuda_entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=MOCK_OPTIONS_GLOBALS | {CONF_ATTENUATION: 0.5, CONF_SMOOTHING_SAMPLES: 1}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert setup_bermuda_entry.options[CONF_ATTENUATION] == 0.5


async def test_calibration_step_1_rejects_zero_attenuation(hass: HomeAssistant):
    coordinator = SimpleNamespace(devices={}, scanner_list=["y"])
    config_entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME)
    config_entry.add_to_hass(hass)
    config_entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    flow = BermudaOptionsFlowHandler(config_entry)
    flow.hass = hass
    flow.handler = config_entry.entry_id
    flow.coordinator = coordinator
    result = await flow.async_step_calibration1_global()
    schema = result["data_schema"]
    good = {CONF_DEVICES: "x", CONF_SCANNERS: "y", CONF_REF_POWER: -55, CONF_ATTENUATION: 3, CONF_SAVE_AND_CLOSE: False}
    schema(good)
    with pytest.raises(Exception, match="value must be at least"):
        schema(good | {CONF_ATTENUATION: 0})


async def test_saved_out_of_range_options_fall_back_to_defaults(hass: HomeAssistant, bypass_get_data, caplog):
    """A zero attenuation saved before validation existed must not crash every advert."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_ATTENUATION: 0, CONF_MAX_RADIUS: -5, CONF_REF_POWER: -60.0},
        entry_id="test",
        title=NAME,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    coordinator: BermudaDataUpdateCoordinator = entry.runtime_data.coordinator
    assert coordinator.options[CONF_ATTENUATION] == DEFAULT_ATTENUATION
    assert coordinator.options[CONF_MAX_RADIUS] == DEFAULT_MAX_RADIUS
    assert coordinator.options[CONF_REF_POWER] == -60.0  # valid values are kept
    assert "out-of-range" in caplog.text

    # The per-advert distance maths runs without a ZeroDivisionError.
    scanner = MagicMock(address="aa:bb:cc:dd:ee:10", is_remote_scanner=False, last_seen=0.0)
    advert = BermudaAdvert(
        MagicMock(ref_power=0, address="11:22:33:44:55:66", name_bt_local_name=None),
        MagicMock(rssi=-60, local_name=None, manufacturer_data={}, service_data={}, service_uuids=[]),
        coordinator.options,
        scanner,
    )
    assert advert.rssi_distance_raw > 0


def test_autotune_settings_survive_bad_saved_values():
    """Zero attenuation / max radius would divide by zero or take log10(0) in the tuner."""
    settings = current_settings(
        {CONF_ATTENUATION: 0, CONF_MAX_RADIUS: 0, CONF_SCANNER_MAX_RADIUS: {"s1": 0, "s2": 4.0}}, ["s1", "s2"]
    )
    assert settings.attenuation == DEFAULT_ATTENUATION
    assert settings.max_radius == DEFAULT_MAX_RADIUS
    assert settings.radius("s1") == DEFAULT_MAX_RADIUS  # a zero per-receiver limit is ignored
    assert settings.radius("s2") == 4.0
    assert settings.rssi_cutoff("s1") < 0
    assert estimated_distance(settings, "s1", -60) > 0

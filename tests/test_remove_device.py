"""Deleting a tracked device in the Home Assistant UI sticks."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import async_remove_config_entry_device
from custom_components.roomwise.const import CONF_DEVICES, DOMAIN, DOMAIN_PRIVATE_BLE_DEVICE, NAME

from .const import MOCK_CONFIG

MAC = "AA:BB:CC:DD:EE:FF"
BEACON = "E2C56DB5DFFB48D2B060D0F5A71096E0_1_2"
OTHER = "11:22:33:44:55:66"


@pytest.fixture
async def entry(hass: HomeAssistant, bypass_get_data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        options={CONF_DEVICES: [MAC, BEACON, OTHER]},
        entry_id="test",
        title=NAME,
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    return entry


def _device(hass, entry, identifier):
    return dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={identifier})


@pytest.mark.parametrize("address", [MAC, BEACON])
async def test_removed_device_is_no_longer_configured(hass: HomeAssistant, entry, address):
    coordinator = entry.runtime_data.coordinator
    tracked = coordinator._get_or_create_device(address)
    tracked.create_sensor = True

    device_entry = _device(hass, entry, (DOMAIN, address.lower()))
    assert await async_remove_config_entry_device(hass, entry, device_entry)

    assert tracked.create_sensor is False
    assert address not in entry.options[CONF_DEVICES]
    assert OTHER in entry.options[CONF_DEVICES]  # the rest stay
    await hass.async_block_till_done()  # the options change reloads the integration

    # After the reload the device is not tracked (and so not re-created) any more.
    coordinator = entry.runtime_data.coordinator
    coordinator.devices.setdefault(address.lower(), coordinator._get_or_create_device(address)).calculate_data()
    assert coordinator.devices[address.lower()].create_sensor is False


async def test_private_ble_devices_are_removed_in_their_own_integration(hass: HomeAssistant, entry):
    device_entry = _device(hass, entry, (DOMAIN_PRIVATE_BLE_DEVICE, "5eedc0de0123456789abcdef01234567"))
    assert not await async_remove_config_entry_device(hass, entry, device_entry)
    assert entry.options[CONF_DEVICES] == [MAC, BEACON, OTHER]

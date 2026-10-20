"""The Bluetooth manufacturer name tables load wherever the integration is installed."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

# Captured at import, before the autouse fixture that skips this (slow) load in other tests.
REAL_LOAD = BermudaDataUpdateCoordinator.async_load_manufacturer_ids


async def test_manufacturer_tables_load_outside_the_config_folder(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    # The test config folder has no custom_components/roomwise in it.
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    await REAL_LOAD(coordinator)
    assert coordinator.member_uuids
    assert coordinator.company_uuids
    assert coordinator.get_manufacturer_from_id(0x004C)[0] == "Apple Inc."
    assert coordinator._waitingfor_load_manufacturer_ids is False

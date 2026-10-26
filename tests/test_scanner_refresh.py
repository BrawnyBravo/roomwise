"""A proxy's room and name changes in Home Assistant reach Roomwise without a restart."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.components.bluetooth import BaseHaRemoteScanner
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.const import DOMAIN, REPAIR_SCANNER_WITHOUT_AREA

PROXY = "aa:bb:cc:dd:ee:10"


def _ha_scanner():
    scanner = MagicMock(spec=BaseHaRemoteScanner)
    scanner.source = PROXY.upper()
    scanner.name = "Test proxy"
    scanner.time_since_last_detection.return_value = 1.0
    scanner.discovered_device_timestamps = {}
    scanner.discovered_devices_and_advertisement_data = {}
    return scanner


async def _proxy(hass: HomeAssistant):
    """A proxy registered by the Bluetooth integration, with no room yet."""
    entry = MockConfigEntry(domain="bluetooth")
    entry.add_to_hass(hass)
    devreg = dr.async_get(hass)
    return devreg, devreg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, PROXY.upper())},
        name="Test proxy",
    )


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_SCANNER_WITHOUT_AREA)


async def test_proxy_room_and_name_changes_are_picked_up(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    devreg, proxy_entry = await _proxy(hass)
    ha_scanner = _ha_scanner()

    with patch.object(coordinator, "_manager", MagicMock(**{"async_current_scanners.return_value": [ha_scanner]})):
        coordinator._refresh_scanners(force=True)
        scanner = coordinator.devices[PROXY]
        assert scanner.area_id is None
        assert _issue(hass) is not None  # "proxy without a room" repair raised

        # A tracked device already heard by this proxy.
        advert = BermudaAdvert(MagicMock(ref_power=0, address="11:22:33:44:55:66"), MagicMock(rssi=-60), {}, scanner)

        # The user follows the repair and puts the proxy in a room, and renames it.
        living = ar.async_get(hass).async_create("Living Room")
        devreg.async_update_device(proxy_entry.id, area_id=living.id, name_by_user="Lounge proxy")
        await hass.async_block_till_done()

        # The device registry listener asks for a scanner refresh; the next cycle does it.
        coordinator._async_gather_advert_data()

        assert scanner.area_id == living.id
        assert scanner.name == "Lounge proxy"
        assert _issue(hass) is None  # repair cleared
        # Adverts follow their proxy's current room and name.
        assert advert.area_id == living.id
        assert advert.area_name == "Living Room"
        assert advert.name == "Lounge proxy"
        # The one-off refresh request has been dealt with.
        assert coordinator._scanner_init_pending is False

        # Moving the proxy to another room is picked up as well.
        office = ar.async_get(hass).async_create("Office")
        devreg.async_update_device(proxy_entry.id, area_id=office.id)
        await hass.async_block_till_done()
        coordinator._async_gather_advert_data()
        assert scanner.area_id == office.id
        assert advert.area_id == office.id


async def test_unforced_refresh_with_unchanged_scanners_does_no_work(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    await _proxy(hass)
    ha_scanner = _ha_scanner()
    with patch.object(coordinator, "_manager", MagicMock(**{"async_current_scanners.return_value": [ha_scanner]})):
        coordinator._refresh_scanners(force=True)
        scanner = coordinator.devices[PROXY]
        with patch.object(scanner, "async_as_scanner_resolve_device_entries") as resolve:
            coordinator._refresh_scanners()
            resolve.assert_not_called()
            coordinator._refresh_scanners(force=True)
            resolve.assert_called_once()

"""
Auto-tune landmark candidates with the real address classification.

A public address whose first hex digit is 0-7, with no "public" hint from the
Bluetooth stack, is classed as a random (rotating) address by its shape. If it
has been heard unchanged for longer than a private address lives, it must still
be offered as a landmark. Rotating addresses, known private BLE devices, tracked
devices and things that look like they move must not be.
"""

from __future__ import annotations

from types import SimpleNamespace

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import autotune_ha
from custom_components.roomwise.const import (
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    BDADDR_TYPE_RANDOM_UNRESOLVABLE,
    CONF_DEVICES,
    METADEVICE_TYPE_PRIVATE_BLE_SOURCE,
)

PUBLIC_4X = "4a:10:20:30:40:01"  # public, but shaped like a resolvable private address
PUBLIC_2X = "2c:10:20:30:40:02"  # public, but shaped like a non-resolvable private address
FRESH_RPA = "5b:10:20:30:40:03"  # a rotating address heard for a few minutes only
KNOWN_IRK = "6c:10:20:30:40:04"  # current address of a known private BLE device (phone)
PHONE = "4d:10:20:30:40:05"  # steady, but its name says it moves
TRACKED = "4e:10:20:30:40:06"  # steady, but Roomwise tracks it as a moving device
SCANNER = "aa:aa:aa:aa:aa:01"


def _heard(coordinator, address, minutes_known, name=None):
    """A device created through the real coordinator (real address classification)."""
    device = coordinator._get_or_create_device(address)  # noqa: SLF001
    now = monotonic_time_coarse()
    device.first_seen = now - minutes_known * 60
    device.last_seen = now - 5
    device.adverts = {SCANNER: SimpleNamespace(hist_rssi=[-60, -61, -60, -62])}
    if name:
        device.name = name
    return device


async def test_candidates_keep_steady_public_addresses_misclassified_as_random(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator.options[CONF_DEVICES] = [TRACKED.upper()]
    devs = {
        PUBLIC_4X: _heard(coordinator, PUBLIC_4X, 25),
        PUBLIC_2X: _heard(coordinator, PUBLIC_2X, 25),
        FRESH_RPA: _heard(coordinator, FRESH_RPA, 4),
        KNOWN_IRK: _heard(coordinator, KNOWN_IRK, 25),
        PHONE: _heard(coordinator, PHONE, 25, name="Pixel 9"),
        TRACKED: _heard(coordinator, TRACKED, 25),
    }
    devs[KNOWN_IRK].metadevice_type.add(METADEVICE_TYPE_PRIVATE_BLE_SOURCE)
    # The real classifier calls these random, as the critic found:
    assert devs[PUBLIC_4X].address_type == BDADDR_TYPE_RANDOM_RESOLVABLE
    assert devs[PUBLIC_2X].address_type == BDADDR_TYPE_RANDOM_UNRESOLVABLE
    assert devs[FRESH_RPA].address_type == BDADDR_TYPE_RANDOM_RESOLVABLE

    cands = {c.address for c in autotune_ha.candidates(coordinator, autotune_ha.Discovery())}

    assert PUBLIC_4X in cands
    assert PUBLIC_2X in cands
    for address in (FRESH_RPA, KNOWN_IRK, PHONE, TRACKED):
        assert address not in cands, address


async def test_registry_prefill_accepts_a_steady_public_address_shaped_like_a_random_one(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    kitchen = ar.async_get(hass).async_create("Kitchen")
    other = MockConfigEntry(domain="test_other")
    other.add_to_hass(hass)
    devreg = dr.async_get(hass)
    for address, name in ((PUBLIC_4X, "Fridge"), (FRESH_RPA, "Tag")):
        entry = devreg.async_get_or_create(
            config_entry_id=other.entry_id, connections={(dr.CONNECTION_BLUETOOTH, address)}, name=name
        )
        devreg.async_update_device(entry.id, area_id=kitchen.id)
    _heard(coordinator, PUBLIC_4X, 25)
    _heard(coordinator, FRESH_RPA, 4)

    found = autotune_ha.discover(hass, coordinator)

    assert {lm.id: lm.area for lm in found.landmarks} == {PUBLIC_4X: kitchen.id}
    assert dict(found.skipped)["Tag"] == "rotating or beacon address"
    cands = {c.address: c for c in autotune_ha.candidates(coordinator, found)}
    assert cands[PUBLIC_4X].suggested

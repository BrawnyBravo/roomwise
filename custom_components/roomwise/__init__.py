"""
Roomwise: Bluetooth room presence for Home Assistant.

Roomwise is built on Bermuda BLE Trilateration by Ashley Gittins
(https://github.com/agittins/bermuda), MIT licence.

For more details about this integration, please refer to
https://github.com/BrawnyBravo/roomwise
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_registry import async_migrate_entries

from .const import _LOGGER, CONF_DEVICES, DOMAIN, DOMAIN_PRIVATE_BLE_DEVICE, PLATFORMS, STARTUP_MESSAGE
from .coordinator import BermudaDataUpdateCoordinator
from .util import mac_math_offset, mac_norm

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

type BermudaConfigEntry = ConfigEntry[BermudaData]


@dataclass
class BermudaData:
    """Holds global data for Bermuda."""

    coordinator: BermudaDataUpdateCoordinator


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> bool:
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        _LOGGER.info(STARTUP_MESSAGE)
    coordinator = BermudaDataUpdateCoordinator(hass, entry)
    entry.runtime_data = BermudaData(coordinator)

    async def on_failure():
        _LOGGER.debug("Coordinator last update failed, rasing ConfigEntryNotReady")
        raise ConfigEntryNotReady

    try:
        await coordinator.async_refresh()
    except Exception as ex:  # noqa: BLE001
        _LOGGER.exception(ex)
        await on_failure()
    if not coordinator.last_update_success:
        await on_failure()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: BermudaConfigEntry) -> bool:
    """Migrate previous config entries."""
    _LOGGER.debug("Migrating config from version %s.%s", config_entry.version, config_entry.minor_version)
    _oldversion = f"{config_entry.version}.{config_entry.minor_version}"

    if config_entry.version == 3:  # it won't be.
        # Bogus version for now, wanted to placeholder the migrate_entries / unique_id thing.
        # If we need to manage unique_id of sensors, we probably just need
        # to manage the callback, but not worry about the hass update.
        #
        # This is lifted from the discussion at https://community.home-assistant.io/t/migrating-unique-ids/348512
        #
        # Also worth looking at https://github.com/home-assistant/core/pull/115265/files for an example
        # of migrating unique_ids from one form to another.
        #
        old_unique_id = config_entry.unique_id
        new_unique_id = mac_math_offset(old_unique_id, 3)

        @callback
        def update_unique_id(entity_entry):
            """Update unique_id of an entity."""
            return {"new_unique_id": entity_entry.unique_id.replace(old_unique_id, new_unique_id)}

        if old_unique_id != new_unique_id:
            await async_migrate_entries(hass, config_entry.entry_id, update_unique_id)
            hass.config_entries.async_update_entry(config_entry, unique_id=new_unique_id)

        return False

    if f"{config_entry.version}.{config_entry.minor_version}" != _oldversion:
        _LOGGER.info("Migrated config entry to version %s.%s", config_entry.version, config_entry.minor_version)

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: BermudaConfigEntry, device_entry: DeviceEntry
) -> bool:
    """
    Implements user-deletion of devices from device registry.

    The device is also dropped from the configured-devices option, otherwise the
    next update cycle (or the next restart) would create it and its entities again.
    """
    if any(domain == DOMAIN_PRIVATE_BLE_DEVICE for domain, *_ in device_entry.identifiers):
        # Roomwise tracks every Private BLE device automatically; removing it here
        # would only last until the next restart. It is managed (and removed) in
        # the Private BLE Device integration.
        _LOGGER.warning(
            "%s is a Private BLE device: remove it from the Private BLE Device integration instead",
            device_entry.name_by_user or device_entry.name,
        )
        return False

    coordinator: BermudaDataUpdateCoordinator = config_entry.runtime_data.coordinator
    address = None
    for identifier in device_entry.identifiers:
        if len(identifier) == 2 and identifier[0] == DOMAIN:
            address = _address_from_identifier(identifier[1])
    if address is None:
        # Even if we don't know this address it probably just means it's stale or from
        # a previous version that used weirder names. Allow it.
        _LOGGER.warning(
            "Didn't find address for %s but allowing deletion to proceed.",
            device_entry.name,
        )
        return True

    if device := coordinator.devices.get(address):
        device.create_sensor = False
    configured = config_entry.options.get(CONF_DEVICES, [])
    remaining = [item for item in configured if mac_norm(item) != address]
    if len(remaining) != len(configured):
        # Saving the options reloads the integration, which is what we want here.
        hass.config_entries.async_update_entry(config_entry, options={**config_entry.options, CONF_DEVICES: remaining})
    return True


def _address_from_identifier(ident: str) -> str:
    """
    The device address in one of our device registry identifiers.

    It is a MAC address, an IRK or an iBeacon uuid_major_minor, possibly with a
    per-sensor suffix like "_range" from older versions. Only that suffix is
    removed: splitting at the first "_" cut an iBeacon id down to its uuid.
    """
    if _IBEACON_ID.fullmatch(ident):
        return ident.lower()
    return mac_norm(ident.split("_", maxsplit=1)[0])


_IBEACON_ID = re.compile(r"[0-9a-f]{32}_[0-9]+_[0-9]+", re.IGNORECASE)


async def async_unload_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> bool:
    """Handle removal of an entry."""
    if unload_result := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        _LOGGER.debug("Unloaded platforms.")
        # The coordinator registers dump_devices bound to itself. Remove it once no
        # other entry is still loaded, so the service doesn't keep a dead
        # coordinator (and all its device data) alive. A reload registers it again.
        if not any(
            other.entry_id != entry.entry_id and other.state is ConfigEntryState.LOADED
            for other in hass.config_entries.async_entries(DOMAIN)
        ):
            hass.services.async_remove(DOMAIN, "dump_devices")
    return unload_result


async def async_reload_entry(hass: HomeAssistant, entry: BermudaConfigEntry) -> None:
    """Reload config entry."""
    hass.config_entries.async_schedule_reload(entry.entry_id)

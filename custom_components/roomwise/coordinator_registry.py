"""Device, area and floor registry events for the coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import (
    Event,
    callback,
)

from .const import (
    _LOGGER,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import (
        EventDeviceRegistryUpdatedData,
    )


class RegistryMixin:
    """Coordinator methods that follow device, area and floor registry changes."""

    def init_floors(self) -> bool:
        """Check if the system has floors configured, and enable sensors."""
        _have_floors: bool = False
        for area in self.ar.async_list_areas():
            if area.floor_id is not None:
                _have_floors = True
                break
        _LOGGER.debug("Have_floors is %s", _have_floors)
        return _have_floors

    @callback
    def handle_area_floor_changes(self, _ev: Event) -> None:
        """Re-check for floors and re-read the scanners' rooms after an area or floor change."""
        self.have_floors = self.init_floors()
        self._scanner_init_pending = True

    @callback
    def handle_devreg_changes(self, ev: Event[EventDeviceRegistryUpdatedData]):
        """
        Update our scanner list if the device registry is changed.

        This catches area changes (on scanners) and any new/changed
        Private BLE Devices.
        """
        if ev.data["action"] == "update":
            _LOGGER.debug("Device registry UPDATE. ev: %s changes: %s", ev, ev.data["changes"])
        else:
            _LOGGER.debug("Device registry has changed. ev: %s", ev)

        device_id = ev.data.get("device_id")

        if ev.data["action"] in {"create", "update"}:
            if device_id is None:
                _LOGGER.error("Received Device Registry create/update without a device_id. ev.data: %s", ev.data)
                return

            # First look for any of our devices that have a stored id on them, it'll be quicker.
            for device in self.devices.values():
                if device.entry_id == device_id:
                    # We matched, most likely a scanner.
                    if device.is_scanner:
                        self._refresh_scanners(force=True)
                        return
            # Didn't match an existing, work through the connections etc.

            # Pull up the device registry entry for the device_id
            if device_entry := self.dr.async_get(ev.data["device_id"]):
                # Work out if it's a device that interests us and respond appropriately.
                for conn_type, _conn_id in device_entry.connections:
                    if conn_type == "private_ble_device":
                        _LOGGER.debug("Trigger updating of Private BLE Devices")
                        self._do_private_device_init = True
                    elif conn_type == "ibeacon":
                        # this was probably us, nothing else to do
                        pass
                    else:
                        for identifier in device_entry.identifiers:
                            # HA device identifiers are expected to be
                            # (domain, id) tuples, but a buggy integration can
                            # register a malformed one (e.g. a bare string that
                            # unpacks into many values). Skip those rather than
                            # letting a ValueError crash the whole handler.
                            if len(identifier) != 2:
                                _LOGGER.debug(
                                    "Ignoring malformed device identifier on %s: %s",
                                    ev.data["device_id"],
                                    identifier,
                                )
                                continue
                            ident_type, ident_id = identifier
                            if ident_type == DOMAIN:
                                # One of our sensor devices!
                                try:
                                    if _device := self.devices[ident_id.lower()]:
                                        _device.name_by_user = device_entry.name_by_user
                                        _device.make_name()
                                except KeyError:
                                    pass
                        # might be a scanner, so let's refresh those
                        _LOGGER.debug("Trigger updating of Scanner Listings")
                        self._scanner_init_pending = True
            else:
                _LOGGER.error(
                    "Received DR update/create but device id does not exist: %s",
                    ev.data["device_id"],
                )

        elif ev.data["action"] == "remove":
            device_found = False
            for address, departed in list(self._scanners_departed.items()):
                if departed["entry_id"] == device_id:
                    _LOGGER.debug("Departed scanner %s removed, no longer watching it", departed["name"])
                    del self._scanners_departed[address]
            for scanner in self.get_scanners:
                if scanner.entry_id == device_id:
                    _LOGGER.debug(
                        "Scanner %s removed, trigger update of scanners",
                        scanner.name,
                    )
                    self._scanner_init_pending = True
                    device_found = True
            if not device_found:
                # If we save the private ble device's device_id into devices[].entry_id
                # we could check ev.data["device_id"] against it to decide if we should
                # rescan PBLE devices. But right now we don't, so scan 'em anyway.
                _LOGGER.debug("Opportunistic trigger of update for Private BLE Devices")
                self._do_private_device_init = True
        # The co-ordinator will only get updates if we have created entities already.
        # Since this might not always be the case (say, private_ble_device loads after
        # we do), then we trigger an update here with the expectation that we got a
        # device registry update after the private ble device was created. There might
        # be other corner cases where we need to trigger our own update here, so test
        # carefully and completely if you are tempted to remove / alter this. Bermuda
        # will skip an update cycle if it detects one already in progress.
        # FIXME: self._async_update_data_internal()

"""Scanner list, sync, advert harvest and scanner repairs for the coordinator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    DOMAIN,
    PRUNE_TIME_DEFAULT,
    PRUNE_TIME_UNKNOWN_IRK,
    REPAIR_SCANNER_SILENT,
    REPAIR_SCANNER_WITHOUT_AREA,
    SCANNER_SILENT_AFTER,
    SCANNER_SILENT_CHECK_INTERVAL,
    SIGNAL_SCANNERS_CHANGED,
)
from .coordinator_clock import monotonic_time_coarse
from .util import mac_norm

if TYPE_CHECKING:
    from habluetooth import BaseHaScanner

    from .bermuda_device import BermudaDevice


class ScannerMixin:
    """Coordinator methods that track scanners, gather adverts and raise scanner repairs."""

    @property
    def scanner_list(self):
        return self._scanner_list

    @property
    def get_scanners(self) -> set[BermudaDevice]:
        return self._scanners

    def scanner_list_add(self, scanner_device: BermudaDevice):
        self._scanner_list.add(scanner_device.address)
        self._scanners.add(scanner_device)
        async_dispatcher_send(self.hass, SIGNAL_SCANNERS_CHANGED)

    def scanner_list_del(self, scanner_device: BermudaDevice):
        self._scanner_list.remove(scanner_device.address)
        self._scanners.remove(scanner_device)
        async_dispatcher_send(self.hass, SIGNAL_SCANNERS_CHANGED)

    def count_active_scanners(self, max_age=10) -> int:
        """Returns count of scanners that have recently sent updates."""
        stamp = monotonic_time_coarse() - max_age  # seconds
        fresh_count = 0
        for scanner in self.get_active_scanner_summary():
            if scanner.get("last_stamp", 0) > stamp:
                fresh_count += 1
        return fresh_count

    def get_active_scanner_summary(self) -> list[dict]:
        """
        Returns a list of dicts suitable for seeing which scanners
        are configured in the system and how long it has been since
        each has returned an advertisement.

        Sorted by scanner name, so the list is stable and easy to scan by eye.
        """
        stamp = monotonic_time_coarse()
        return [
            {
                "name": scannerdev.name,
                "address": scannerdev.address,
                "last_stamp": scannerdev.last_seen,
                "last_stamp_age": stamp - scannerdev.last_seen,
            }
            for scannerdev in sorted(self.get_scanners, key=lambda scanner: str(scanner.name).casefold())
        ]

    def _async_gather_advert_data(self):
        """Perform the gathering of backend Bluetooth Data and updating scanners and devices."""
        nowstamp = monotonic_time_coarse()
        _timestamp_cutoff = nowstamp - min(PRUNE_TIME_DEFAULT, PRUNE_TIME_UNKNOWN_IRK)

        # Initialise ha_scanners if we haven't already
        if self._scanner_init_pending:
            self._refresh_scanners(force=True)
            self._scanner_init_pending = False

        for ha_scanner in self._hascanners:
            # Create / Get the BermudaDevice for this scanner
            scanner_device = self._get_device(ha_scanner.source)

            if scanner_device is None:
                # Looks like a scanner we haven't met, refresh the list.
                self._refresh_scanners(force=True)
                scanner_device = self._get_device(ha_scanner.source)

            if scanner_device is None:
                # Highly unusual. If we can't find an entry for the scanner
                # maybe it's from an integration that's not yet loaded, or
                # perhaps it's an unexpected type that we don't know how to
                # find.
                _LOGGER_SPAM_LESS.error(
                    f"missing_scanner_entry_{ha_scanner.source}",
                    "Failed to find config for scanner %s, this is probably a bug.",
                    ha_scanner.source,
                )
                continue

            scanner_device.async_as_scanner_update(ha_scanner)

            # Now go through the scanner's adverts and send them to our device objects.
            for bledevice, advertisementdata in ha_scanner.discovered_devices_and_advertisement_data.values():
                if adstamp := scanner_device.async_as_scanner_get_stamp(bledevice.address):
                    if adstamp < self.stamp_last_update_started - 3:
                        # skip older adverts that should already have been processed
                        continue
                if advertisementdata.rssi == -127:
                    # BlueZ is pushing bogus adverts for paired but absent devices.
                    continue

                device = self._get_or_create_device(bledevice.address)
                device.async_apply_address_details(bledevice.details)
                device.process_advertisement(scanner_device, advertisementdata)

        # end of for ha_scanner loop
        return True

    def _refresh_scanners(self, force=False):
        """
        Refresh data on existing scanner objects, and rebuild if scannerlist has changed.

        Called on every update cycle, this handles the *fast* updates (such as updating
        timestamps). If it detects that the list of scanners has changed (or is called
        with force=True) then the full list of scanners will be rebuild by calling
        _rebuild_scanners.
        """
        self._rebuild_scanner_list(force=force)

    def _rebuild_scanner_list(self, force=False):
        """
        Rebuild Bermuda's internal list of scanners.

        Called on every update (via _refresh_scanners) but exits *quickly*
        *unless*:
          - the scanner set has changed or
          - force=True or
          - self._force_full_scanner_init=True
        """
        # Using new API in 2025.2
        _new_ha_scanners: set[BaseHaScanner] = set(self._manager.async_current_scanners())

        _changed = not (_new_ha_scanners is self._hascanners or _new_ha_scanners == self._hascanners)
        if not _changed and not force:
            # No changes, and nobody asked for a re-read.
            return

        if _changed:
            _LOGGER.debug("HA Base Scanner Set has changed, rebuilding.")
            self._hascanners = _new_ha_scanners
            self._async_purge_removed_scanners()
        else:
            # Same scanners, but something about one of them (its room, its name)
            # may have changed in the device registry: re-read them all.
            _LOGGER.debug("Re-reading scanner details from the device registry.")

        # So we can raise a single repair listing all area-less scanners:
        _scanners_without_areas: list[str] = []

        # Find active HaBaseScanners in the backend and treat that as our
        # authoritative source of truth.
        #
        for hascanner in self._hascanners:
            scanner_address = mac_norm(hascanner.source)
            self._scanners_departed.pop(scanner_address, None)
            bermuda_scanner = self._get_or_create_device(scanner_address)
            bermuda_scanner.async_as_scanner_init(hascanner, force=force)

            if bermuda_scanner.area_id is None:
                _scanners_without_areas.append(f"{bermuda_scanner.name} [{bermuda_scanner.address}]")
        self._async_manage_repair_scanners_without_areas(_scanners_without_areas)

    def _async_purge_removed_scanners(self):
        """Demotes any devices that are no longer scanners based on new self.hascanners."""
        _scanners = [device.address for device in self.devices.values() if device.is_scanner]
        for ha_scanner in self._hascanners:
            scanner_address = mac_norm(ha_scanner.source)
            if scanner_address in _scanners:
                # This is still an extant HA Scanner, so we'll keep it.
                _scanners.remove(scanner_address)
        # Whatever's left are presumably no longer scanners.
        for address in _scanners:
            ex_scanner = self.devices[address]
            _LOGGER.info("Demoting ex-scanner %s", ex_scanner.name)
            # Keep watching it: a proxy that has dropped off the network is
            # exactly the one the silent-proxy repair should report.
            self._scanners_departed[address] = {
                "name": ex_scanner.name,
                "address": address,
                "last_stamp": ex_scanner.last_seen,
                "entry_id": ex_scanner.entry_id,
            }
            ex_scanner.async_as_scanner_nolonger()

    def _async_manage_repair_scanners_without_areas(self, scannerlist: list[str]):
        """
        Raise a repair for any scanners that lack an area assignment.

        This function will take care of ensuring a repair is (re)raised
        or cleared (if the list is empty) when given a list of area-less scanner names.

        scannerlist should contain a friendly string to name each scanner missing an area.
        """
        if self._scanners_without_areas != scannerlist:
            self._scanners_without_areas = scannerlist
            # Clear any existing repair, because it's either resolved now (empty list) or we need to re-issue
            # the repair in order to update the scanner list (re-calling doesn't update it).
            ir.async_delete_issue(self.hass, DOMAIN, REPAIR_SCANNER_WITHOUT_AREA)

            if self._scanners_without_areas and len(self._scanners_without_areas) != 0:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    REPAIR_SCANNER_WITHOUT_AREA,
                    translation_key=REPAIR_SCANNER_WITHOUT_AREA,
                    translation_placeholders={
                        "scannerlist": "".join(f"- {name}\n" for name in self._scanners_without_areas),
                    },
                    severity=ir.IssueSeverity.ERROR,
                    is_fixable=False,
                )

    def _async_check_silent_scanners(self):
        """
        Raise a repair for any scanners that have sent nothing for SCANNER_SILENT_AFTER.

        Runs at most once every SCANNER_SILENT_CHECK_INTERVAL. Scanners that have
        unregistered from HA Bluetooth are included, since a proxy that drops off
        the network does exactly that.

        Until Roomwise itself has been running for SCANNER_SILENT_AFTER nothing is
        changed, so a restart neither raises a false alarm for a scanner not heard
        yet nor clears (and un-ignores) a repair that is still valid.
        """
        nowstamp = monotonic_time_coarse()
        if nowstamp - self.stamp_started <= SCANNER_SILENT_AFTER:
            return
        if self.stamp_last_silent_check > nowstamp - SCANNER_SILENT_CHECK_INTERVAL:
            return
        self.stamp_last_silent_check = nowstamp

        stamp = nowstamp - SCANNER_SILENT_AFTER
        _scanners = self.get_active_scanner_summary() + [
            departed for address, departed in self._scanners_departed.items() if address not in self.scanner_list
        ]
        _silent_scanners = sorted(
            (str(scanner["name"]) for scanner in _scanners if scanner["last_stamp"] < stamp), key=str.casefold
        )
        self._async_manage_repair_scanners_silent(_silent_scanners)

    def _async_manage_repair_scanners_silent(self, scannerlist: list[str]):
        """
        Raise, update or clear the repair for scanners that have gone quiet.

        scannerlist should contain a friendly name for each silent scanner. The
        repair is only touched when that list changes; an empty list clears it.
        """
        if self._scanners_silent != scannerlist:
            self._scanners_silent = scannerlist
            if self._scanners_silent:
                # Re-calling create updates the existing repair in place, so the
                # list stays current and a user's "ignore" is kept.
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    REPAIR_SCANNER_SILENT,
                    translation_key=REPAIR_SCANNER_SILENT,
                    translation_placeholders={
                        "minutes": str(SCANNER_SILENT_AFTER // 60),
                        "scannerlist": "".join(f"- {name}\n" for name in self._scanners_silent),
                    },
                    severity=ir.IssueSeverity.WARNING,
                    is_fixable=False,
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, REPAIR_SCANNER_SILENT)

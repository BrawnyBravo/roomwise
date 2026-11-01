"""DataUpdateCoordinator for Roomwise bluetooth data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import voluptuous as vol
import yaml
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.api import _get_manager
from homeassistant.const import MAJOR_VERSION as HA_VERSION_MAJ
from homeassistant.const import MINOR_VERSION as HA_VERSION_MIN
from homeassistant.core import (
    HomeAssistant,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers import (
    floor_registry as fr,
)
from homeassistant.helpers import (
    issue_registry as ir,
)
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import get_age, now

from .bermuda_device import BermudaDevice
from .bermuda_irk import BermudaIrkManager
from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SCANNER_MAX_RADIUS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PRUNE_TIME_DEFAULT,
    PRUNE_TIME_UNKNOWN_IRK,
    REPAIR_SCANNER_SILENT,
    REPAIR_SCANNER_WITHOUT_AREA,
    SAVEOUT_COOLDOWN,
    SCANNER_SILENT_AFTER,
    SCANNER_SILENT_CHECK_INTERVAL,
    SIGNAL_DEVICE_NEW,
    SIGNAL_SCANNERS_CHANGED,
    UPDATE_INTERVAL,
)
from .coordinator_area import AreaMixin
from .coordinator_dump import DumpMixin
from .coordinator_metadevices import MetadeviceMixin
from .coordinator_prune import PruneMixin
from .coordinator_registry import RegistryMixin
from .util import mac_norm, sanitize_options

MANUFACTURER_ID_DIR = Path(__file__).parent / "manufacturer_identification"

if TYPE_CHECKING:
    from habluetooth import BaseHaScanner, BluetoothServiceInfoBleak
    from homeassistant.components.bluetooth import (
        BluetoothChange,
    )
    from homeassistant.components.bluetooth.manager import HomeAssistantBluetoothManager

    from . import BermudaConfigEntry

Cancellable = Callable[[], None]


# The C loader parses the manufacturer tables several times faster.
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_manufacturer_ids(member_path: str | Path, company_path: str | Path) -> tuple[dict[int, str], dict[int, str]]:
    """Read the Bluetooth SIG name tables. Blocking: run in the executor."""
    with Path(member_path).open(encoding="utf-8") as f:
        mi_yaml = yaml.load(f, Loader=YAML_LOADER)["uuids"]  # noqa: S506 - safe loader
    with Path(company_path).open(encoding="utf-8") as f:
        ci_yaml = yaml.load(f, Loader=YAML_LOADER)["company_identifiers"]  # noqa: S506 - safe loader
    return (
        {member["uuid"]: member["name"] for member in mi_yaml},
        {member["value"]: member["name"] for member in ci_yaml},
    )


class BermudaDataUpdateCoordinator(
    RegistryMixin, MetadeviceMixin, AreaMixin, PruneMixin, DumpMixin, DataUpdateCoordinator
):
    """
    Class to manage fetching data from the Bluetooth component.

    Since we are not actually using an external API and only computing local
    data already gathered by the bluetooth integration, the update process is
    very cheap, and the processing process (currently) rather cheap.

    TODO / IDEAS:
    - when we get to establishing a fix, we can apply a path-loss factor to
      a calculated vector based on previously measured losses on that path.
      We could perhaps also fine-tune that with real-time measurements from
      fixed beacons to compensate for environmental factors.
    - An "obstruction map" or "radio map" could provide field strength estimates
      at given locations, and/or hint at attenuation by counting "wall crossings"
      for a given vector/path.

    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: BermudaConfigEntry,
    ) -> None:
        """Initialize."""
        self.platforms = []
        self.config_entry = entry

        self.sensor_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

        # set some version flags
        self.hass_version_min_2025_2 = HA_VERSION_MAJ > 2025 or (HA_VERSION_MAJ == 2025 and HA_VERSION_MIN >= 2)
        # when habasescanner.discovered_device_timestamps became a public method.
        self.hass_version_min_2025_4 = HA_VERSION_MAJ > 2025 or (HA_VERSION_MAJ == 2025 and HA_VERSION_MIN >= 4)

        # ##### Redaction Data ###
        #
        # match/replacement pairs for redacting addresses
        self.redactions: dict[str, str] = {}

        self.stamp_redactions_expiry: float | None = None

        self.update_in_progress: bool = False  # A lock to guard against huge backlogs / slow processing
        self.stamp_last_update: float = 0  # Last time we ran an update, from monotonic_time_coarse()
        self.stamp_last_update_started: float = 0
        # When an advert last triggered an update attempt (see async_handle_advert).
        self._stamp_last_advert_update_attempt: float = 0
        self.stamp_last_prune: float = 0  # When we last pruned device list
        self.stamp_started: float = monotonic_time_coarse()  # When Roomwise started, for the silent-proxy grace
        self.stamp_last_silent_check: float = 0  # When we last looked for silent proxies

        self.member_uuids = {}
        self.company_uuids = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self._waitingfor_load_manufacturer_ids = True
        entry.async_create_background_task(
            hass, self.async_load_manufacturer_ids(), "Load Bluetooth IDs", eager_start=True
        )

        self._manager: HomeAssistantBluetoothManager = _get_manager(hass)  # instance of the bluetooth manager
        self._hascanners: set[BaseHaScanner] = set()  # Links to the backend scanners
        self._hascanner_timestamps: dict[str, dict[str, float]] = {}  # scanner_address, device_address, stamp
        self._scanner_list: set[str] = set()
        self._scanners: set[BermudaDevice] = set()  # Set of all in self.devices that is_scanner=True
        self.irk_manager = BermudaIrkManager(in_use=self._irk_addresses_in_use)

        self.ar = ar.async_get(self.hass)
        self.er = er.async_get(self.hass)
        self.dr = dr.async_get(self.hass)
        self.fr = fr.async_get(self.hass)
        self.have_floors: bool = self.init_floors()

        self._scanners_without_areas: list[str] | None = None  # Tracks any proxies that don't have an area assigned.
        self._scanners_silent: list[str] | None = None  # Tracks any proxies that have gone quiet.
        # Proxies that have unregistered from HA Bluetooth (an ESPHome proxy that drops off the
        # network does this), by address, so they can still be reported as silent until they
        # register again or are deleted from the device registry.
        self._scanners_departed: dict[str, dict] = {}

        # Track the list of Private BLE devices, noting their entity id
        # and current "last address".
        self.pb_state_sources: dict[str, str | None] = {}

        self.metadevices: dict[str, BermudaDevice] = {}

        self._ad_listener_cancel: Cancellable | None = None

        # Tracks the last stamp that we *actually* saved our config entry. Mostly for debugging,
        # we use a request stamp for tracking our add_job request.
        self.last_config_entry_update: float = 0  # Stamp of last *save-out* of config.data

        # We want to delay the first save-out, since it takes a few seconds for things
        # to stabilise. So set the stamp into the future.
        self.last_config_entry_update_request = (
            monotonic_time_coarse() + SAVEOUT_COOLDOWN
        )  # Stamp for save-out requests

        # AJG 2025-04-23 Disabling, see the commented method below for notes.
        # self.config_entry.async_on_unload(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self.handle_state_changes))

        # First time around we freshen the restored scanner info by
        # forcing a scan of the captured info.
        self._scanner_init_pending = True

        self._seed_configured_devices_done = False

        # First time go through the private ble devices to see if there's
        # any there for us to track.
        self._do_private_device_init = True

        # Listen for changes to the device registry and handle them.
        # Primarily for changes to scanners and Private BLE Devices.
        self.config_entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, self.handle_devreg_changes)
        )
        # Rooms and floors: a renamed room, a room moved to a floor, or the first
        # floor being added must not need a restart to show up.
        for _event in (ar.EVENT_AREA_REGISTRY_UPDATED, fr.EVENT_FLOOR_REGISTRY_UPDATED):
            self.config_entry.async_on_unload(self.hass.bus.async_listen(_event, self.handle_area_floor_changes))

        self.options = {}

        # TODO: This is only here because we haven't set up migration of config
        # entries yet, so some users might not have this defined after an update.
        self.options[CONF_ATTENUATION] = DEFAULT_ATTENUATION
        self.options[CONF_DEVTRACK_TIMEOUT] = DEFAULT_DEVTRACK_TIMEOUT
        self.options[CONF_MAX_RADIUS] = DEFAULT_MAX_RADIUS
        self.options[CONF_MAX_VELOCITY] = DEFAULT_MAX_VELOCITY
        self.options[CONF_REF_POWER] = DEFAULT_REF_POWER
        self.options[CONF_SMOOTHING_SAMPLES] = DEFAULT_SMOOTHING_SAMPLES
        self.options[CONF_UPDATE_INTERVAL] = DEFAULT_UPDATE_INTERVAL
        self.options[CONF_RSSI_OFFSETS] = {}

        if hasattr(entry, "options"):
            # Firstly, on some calls (specifically during reload after settings changes)
            # we seem to get called with a non-existant config_entry.
            # Anyway... if we DO have one, convert it to a plain dict so we can
            # serialise it properly when it goes into the device and scanner classes.
            for key, val in entry.options.items():
                if key in (
                    CONF_ATTENUATION,
                    CONF_DEVICES,
                    CONF_DEVTRACK_TIMEOUT,
                    CONF_MAX_RADIUS,
                    CONF_MAX_VELOCITY,
                    CONF_REF_POWER,
                    CONF_SMOOTHING_SAMPLES,
                    CONF_RSSI_OFFSETS,
                    CONF_SCANNER_MAX_RADIUS,
                ):
                    self.options[key] = val

        # A saved value out of range (attenuation 0 divides by zero on every advert)
        # falls back to the default rather than taking the update loop down.
        if fixed := sanitize_options(self.options):
            _LOGGER.warning(
                "Ignoring out-of-range Roomwise option(s) %s and using the default; "
                "set a valid value under Configure > Global Options",
                ", ".join(fixed),
            )

        self.devices: dict[str, BermudaDevice] = {}
        # self.updaters: dict[str, BermudaPBDUCoordinator] = {}

        # Register the dump_devices service. Admin only: even redacted, the dump
        # describes every device in the home and which room it is in, and
        # unredacted it holds addresses and the identity keys (IRKs) of phones.
        async_register_admin_service(
            hass,
            DOMAIN,
            "dump_devices",
            self.service_dump_devices,
            vol.Schema(
                {
                    vol.Optional("addresses"): cv.string,
                    vol.Optional("configured_devices"): cv.boolean,
                    vol.Optional("redact"): cv.boolean,
                }
            ),
            SupportsResponse.ONLY,
        )

        # Register for newly discovered / changed BLE devices
        if self.config_entry is not None:
            self.config_entry.async_on_unload(
                bluetooth.async_register_callback(
                    self.hass,
                    self.async_handle_advert,
                    bluetooth.BluetoothCallbackMatcher(connectable=False),
                    bluetooth.BluetoothScanningMode.ACTIVE,
                )
            )

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

    def get_manufacturer_from_id(self, uuid: int | str) -> tuple[str, bool] | tuple[None, None]:
        """
        An opinionated Bluetooth UUID to Name mapper.

        - uuid must be four hex chars in a string, or an `int`

        Retreives the manufacturer name from the Bluetooth SIG Member UUID listing,
        using a cached copy of https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml

        HOWEVER: Bermuda adds some opinionated overrides for the benefit of user clarity:
        - Legal entity names may be overriden with well-known brand names
        - Special-use prefixes may be tagged as such (eg iBeacon etc)
        - Generics can be excluded by setting exclude_generics=True
        """
        if isinstance(uuid, str):
            uuid = int(uuid.replace(":", ""), 16)

        _generic = False
        # Because iBeacon and (soon) GFMD and AppleFindmy etc are common protocols, they
        # don't do a good job of uniquely identifying a manufacturer, so we use them
        # as fallbacks only.
        if uuid == 0x0BA9:
            # allterco robotics, aka...
            _name = "Shelly Devices"
        elif uuid == 0x004C:
            # Apple have *many* UUIDs, but since they don't OEM for others (AFAIK)
            # and only the iBeacon / FindMy adverts seem to be third-partied, match just
            # this one instead of their entire set.
            _name = "Apple Inc."
            _generic = True
        elif uuid == 0x181C:
            _name = "BTHome v1 cleartext"
            _generic = True
        elif uuid == 0x181E:
            _name = "BTHome v1 encrypted"
            _generic = True
        elif uuid == 0xFCD2:
            _name = "BTHome V2"  # Sponsored by Allterco / Shelly
            _generic = True
        elif uuid in self.member_uuids:
            _name = self.member_uuids[uuid]
            # Hardware manufacturers who OEM MAC PHYs etc, or offer the use
            # of their OUIs to third parties (specific known ones can be moved
            # to a case in the above conditions).
            if any(x in _name for x in ["Google", "Realtek"]):
                _generic = True
        elif uuid in self.company_uuids:
            _name = self.company_uuids[uuid]
            _generic = False
        else:
            return (None, None)
        return (_name, _generic)

    async def async_load_manufacturer_ids(self):
        """Import yaml files containing manufacturer name mappings."""
        try:
            # Next to this file, wherever the integration is installed (not
            # assumed to be under the config folder's custom_components).
            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml
            member_path = MANUFACTURER_ID_DIR / "member_uuids.yaml"
            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/company_identifiers/company_identifiers.yaml
            company_path = MANUFACTURER_ID_DIR / "company_identifiers.yaml"
            # Reading and parsing ~200 KB of YAML blocks for a noticeable time, so
            # it runs in the executor rather than on the event loop.
            self.member_uuids, self.company_uuids = await self.hass.async_add_executor_job(
                _load_manufacturer_ids, member_path, company_path
            )
        finally:
            # Ensure that an issue reading these files (which are optional, really) doesn't stop the whole show.
            self._waitingfor_load_manufacturer_ids = False

    @callback
    def async_handle_advert(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """
        Handle an incoming advert callback from the bluetooth integration.

        These should come in as adverts are received, rather than on our update schedule.
        The data *should* be as fresh as can be, but actually the backend only sends
        these periodically (mainly when the data changes, I think). So it's no good for
        responding to changing rssi values, but it *is* good for seeding our updates in case
        there are no defined sensors yet (or the defined ones are away).
        """
        # _LOGGER.debug(
        #     "New Advert! change: %s, scanner: %s mac: %s name: %s serviceinfo: %s",
        #     change,
        #     service_info.source,
        #     service_info.address,
        #     service_info.name,
        #     service_info,
        # )

        # If there are no active entities created after Bermuda's
        # initial setup, then no updates will be triggered on the co-ordinator.
        # So let's check if we haven't updated recently, and do so...
        #
        # The attempt is stamped *before* running: stamp_last_update only moves
        # on success, so after a failing cycle every advert from every device
        # (often hundreds a second) would otherwise run the whole update again,
        # fail again and hand the traceback to the Bluetooth stack each time.
        nowstamp = monotonic_time_coarse()
        cutoff = nowstamp - (UPDATE_INTERVAL * 2)
        if self.stamp_last_update < cutoff and self._stamp_last_advert_update_attempt < cutoff:
            self._stamp_last_advert_update_attempt = nowstamp
            try:
                self._async_update_data_internal()
            except Exception:  # noqa: BLE001 - must not escape into the Bluetooth callback
                _LOGGER_SPAM_LESS.error(
                    "advert_update_failed",
                    "Roomwise update triggered by a Bluetooth advert failed",
                    exc_info=True,
                )

    def _check_all_platforms_created(self, address):
        """Checks if all platforms have finished loading a device's entities."""
        dev = self._get_device(address)
        if dev is not None:
            if all(
                [
                    dev.create_sensor_done,
                    dev.create_tracker_done,
                    dev.create_number_done,
                ]
            ):
                dev.create_all_done = True

    def sensor_created(self, address):
        """Allows sensor platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_sensor_done = True
            # _LOGGER.debug("Sensor confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")
        self._check_all_platforms_created(address)

    def device_tracker_created(self, address):
        """Allows device_tracker platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_tracker_done = True
            # _LOGGER.debug("Device_tracker confirmed created for %s", address)
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")
        self._check_all_platforms_created(address)

    def number_created(self, address):
        """Receives report from number platform that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_number_done = True
        self._check_all_platforms_created(address)

    # def button_created(self, address):
    #     """Receives report from number platform that sensors have been set up."""
    #     dev = self._get_device(address)
    #     if dev is not None:
    #         dev.create_button_done = True
    #     self._check_all_platforms_created(address)

    def count_active_devices(self) -> int:
        """
        Returns the number of bluetooth devices that have recent timestamps.

        Useful as a general indicator of health
        """
        stamp = monotonic_time_coarse() - 10  # seconds
        fresh_count = 0
        for device in self.devices.values():
            if device.last_seen > stamp:
                fresh_count += 1
        return fresh_count

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

    def _get_device(self, address: str) -> BermudaDevice | None:
        """Search for a device entry based on mac address."""
        # mac_norm tries to return a lower-cased, colon-separated mac address.
        # failing that, it returns the original, lower-cased.
        try:
            return self.devices[mac_norm(address)]
        except KeyError:
            return None

    def _get_or_create_device(self, address: str) -> BermudaDevice:
        mac = mac_norm(address)
        try:
            return self.devices[mac]
        except KeyError:
            self.devices[mac] = device = BermudaDevice(mac, self)
            return device

    async def _async_update_data(self):
        """Implementation of DataUpdateCoordinator update_data function."""
        # return False
        self._async_update_data_internal()

    def _async_update_data_internal(self):
        """
        The primary update loop that processes almost all data in Bermuda.

        This works only with local data, so should be cheap to run
        (no network requests made etc). This function takes care of:

        - gathering all bluetooth adverts since last run and saving them into
          Bermuda's device objects
        - Updating all metadata
        - Performing rssi and statistical calculations
        - Making area determinations
        - (periodically) pruning device entries

        """
        if self._waitingfor_load_manufacturer_ids:
            _LOGGER.debug("Waiting for BT data load...")
            return True
        if self.update_in_progress:
            # Eeep!
            _LOGGER_SPAM_LESS.warning("update_still_running", "Previous update still running, skipping this cycle.")
            return False
        self.update_in_progress = True

        try:  # so we can still clean up update_in_progress
            nowstamp = monotonic_time_coarse()

            # The main "get all adverts from the backend" part.
            result_gather_adverts = self._async_gather_advert_data()

            self.update_metadevices()

            # Calculate per-device data
            #
            # Scanner entries have been loaded up with latest data, now we can
            # process data for all devices over all scanners.
            for device in self.devices.values():
                # Recalculate smoothed distances, last_seen etc
                device.calculate_data()

            self._refresh_areas_by_min_distance()

            # We might need to freshen deliberately on first start if no new scanners
            # were discovered in the first scan update. This is likely if nothing has changed
            # since the last time we booted.
            # if self._do_full_scanner_init:
            #     if not self._refresh_scanners():
            #         # _LOGGER.debug("Failed to refresh scanners, likely config entry not ready.")
            #         # don't fail the update, just try again next time.
            #         # self.last_update_success = False
            #         pass

            # If any *configured* devices have not yet been seen, create device
            # entries for them so they will claim the restored sensors in HA
            # (this prevents them from restoring at startup as "Unavailable" if they
            # are not currently visible, and will instead show as "Unknown" for
            # sensors and "Away" for device_trackers).
            #
            # This isn't working right if it runs once. Bodge it for now (cost is low)
            # and sort it out when moving to device-based restoration (ie using DR/ER
            # to decide what devices to track and deprecating CONF_DEVICES)
            #
            # if not self._seed_configured_devices_done:
            for _source_address in self.options.get(CONF_DEVICES, []):
                self._get_or_create_device(_source_address)
            self._seed_configured_devices_done = True

            # Trigger creation of any new entities
            #
            # The devices are all updated now (and any new scanners and beacons seen have been added),
            # so let's ensure any devices that we create sensors for are set up ready to go.
            for address, device in self.devices.items():
                if device.create_sensor:
                    if not device.create_all_done:
                        _LOGGER.debug("Firing device_new for %s", device.__repr__())
                        # Note that the below should be OK thread-wise, debugger indicates this is being
                        # called by _run in events.py, so pretty sure we are "in the event loop".
                        async_dispatcher_send(self.hass, SIGNAL_DEVICE_NEW, address)

            # Device Pruning (only runs periodically)
            self.prune_devices()

            # Silent proxy repair (only runs periodically)
            self._async_check_silent_scanners()

        finally:
            # end of async update
            self.update_in_progress = False

        self.stamp_last_update_started = nowstamp
        self.stamp_last_update = monotonic_time_coarse()
        self.last_update_success = True
        return result_gather_adverts

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

    def dt_mono_to_datetime(self, stamp) -> datetime:
        """Given a monotonic timestamp, convert to datetime object."""
        age = monotonic_time_coarse() - stamp
        return now() - timedelta(seconds=age)

    def dt_mono_to_age(self, stamp) -> str:
        """Convert monotonic timestamp to age (eg: "6 seconds ago")."""
        return get_age(self.dt_mono_to_datetime(stamp))

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

    # *** Not required now that we don't reload for scanners.
    # @callback
    # def async_call_update_entry(self, confdata_scanners) -> None:
    #     """
    #     Call in the event loop to update the scanner entries in our config.

    #     We do this via add_job to ensure it runs in the event loop.
    #     """
    #     # Clear the flag for init and update the stamp
    #     self._do_full_scanner_init = False
    #     self.last_config_entry_update = monotonic_time_coarse()
    #     # Apply new config (will cause reload if there are changes)
    #     self.hass.config_entries.async_update_entry(
    #         self.config_entry,
    #         data={
    #             **self.config_entry.data,
    #             CONFDATA_SCANNERS: confdata_scanners,
    #         },
    #     )

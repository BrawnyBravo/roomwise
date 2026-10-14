"""
Home Assistant glue for room auto-tuning.

- Finds landmarks (stationary devices with a known room) in the device registry.
- Collects rssi samples from adverts the coordinator already receives (no extra
  radio work): it listens for coordinator updates and records each new advert
  stamp per (landmark, scanner).
- Runs the pure tuner in autotune.py in an executor.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .autotune import (
    Landmark,
    Sample,
    Scanner,
    Settings,
    TuneOptions,
    TuneReport,
    evaluate,
    export_dict,
)
from .const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_OTHER,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    BDADDR_TYPE_RANDOM_STATIC,
    BDADDR_TYPE_RANDOM_UNRESOLVABLE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_MAX_RADIUS,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SCANNER_MAX_RADIUS,
    DEFAULT_ATTENUATION,
    DEFAULT_MAX_RADIUS,
    DEFAULT_REF_POWER,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    METADEVICE_TYPE_PRIVATE_BLE_SOURCE,
)
from .util import mac_math_offset, name_is_address

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import BermudaDataUpdateCoordinator

DEFAULT_AUTOTUNE_MINUTES = 10
CONF_AUTOTUNE_LANDMARKS = "autotune_landmarks"  # {address: area_id}, saved on Apply
EXPORT_FILENAME = "roomwise_autotune_samples.json"

# Size limits, so a big selection cannot exhaust memory or CPU on a small box.
# Memory: at most MAX_TOTAL_SAMPLES samples are kept (about 10 MB, plus about as
# much again while the optional export file is written).
MAX_SAMPLES_PER_PAIR = 600  # per (device, receiver): a uniform sample of the whole window
MAX_TOTAL_SAMPLES = 100_000
MAX_AUTOTUNE_LANDMARKS = 60  # devices that can be ticked in one run
MAX_AUTOTUNE_MINUTES = 60
MAX_AUTOTUNE_DEVICE_MINUTES = 1200  # devices x minutes, e.g. 40 devices for 30 minutes
CAPPED_NOTE = (
    "Auto-tune stopped recording new readings after {count:,} (its memory limit); "
    "the result uses what it had collected by then."
)

# Words in a device's name, model or manufacturer that mean "this thing moves".
MOVING_DEVICE_WORDS = (
    "phone",
    "iphone",
    "ipad",
    "pixel",
    "galaxy",
    "watch",
    "band",
    "airtag",
    "tile",
    "tracker",
    "earbud",
    "buds",
    "airpods",
    "headphone",
    "headset",
    "keyfob",
    "key fob",
    "wallet",
    "laptop",
    "macbook",
    "tablet",
    "remote",
    "collar",
)

# Never landmarks: beacons and private BLE devices are built by the integration
# from things that move, and non-MAC48 ids are not radio addresses.
NEVER_LANDMARK_TYPES = {
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_NOT_MAC48,
}

# Fixed addresses: public (OTHER) or random-static.
STABLE_ADDRESS_TYPES = {BDADDR_TYPE_OTHER, BDADDR_TYPE_RANDOM_STATIC}

# Classed as rotating from the address shape alone. A public address whose
# first hex digit is 0-7 looks exactly like these when the Bluetooth stack gives
# no hint, so such an address is still usable once it has been heard, unchanged,
# for longer than a private address lives (about 15 minutes by the spec).
SHAPE_ROTATING_TYPES = {BDADDR_TYPE_RANDOM_RESOLVABLE, BDADDR_TYPE_RANDOM_UNRESOLVABLE}
STEADY_MIN_AGE = 20 * 60  # seconds the same address must have been heard
STEADY_MAX_GAP = 120  # ...and heard within this many seconds

# A WiFi/Ethernet MAC in the registry often sits next to the device's Bluetooth
# MAC. Same idea the coordinator uses for proxies (WIFI+2, ETHER-1).
MAC_NEIGHBOUR_OFFSETS = (0, 1, 2, -1)


@dataclass
class Discovery:
    """What we found: landmarks, scanners, and what we skipped and why."""

    landmarks: list[Landmark] = field(default_factory=list)
    scanners: list[Scanner] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (device name, reason)
    landmark_names: dict[str, str] = field(default_factory=dict)
    area_names: dict[str, str] = field(default_factory=dict)
    how: dict[str, str] = field(default_factory=dict)  # address -> how it was matched, when it is a guess


def _looks_mobile(device: dr.DeviceEntry) -> bool:
    text = " ".join(str(x).lower() for x in (device.name_by_user, device.name, device.model, device.manufacturer) if x)
    return any(word in text for word in MOVING_DEVICE_WORDS)


def _heard_steadily(device, now: float) -> bool:
    """The same address has been heard for longer than any private address lives, and lately."""
    first_seen = getattr(device, "first_seen", None)
    last_seen = getattr(device, "last_seen", 0) or 0
    return first_seen is not None and now - first_seen >= STEADY_MIN_AGE and now - last_seen <= STEADY_MAX_GAP


def _fixed_address(device, now: float) -> bool:
    """
    Say whether this address could belong to a device that never moves.

    Public and random-static addresses: yes. Addresses that look rotating from
    their shape: only when heard steadily (they may be public addresses with no
    hint from the Bluetooth stack). Never: beacons, private BLE devices and the
    current address of a known private BLE device (an IRK match, a phone or watch).
    """
    if device.is_scanner or device.address_type in NEVER_LANDMARK_TYPES:
        return False
    if METADEVICE_TYPE_PRIVATE_BLE_SOURCE in (getattr(device, "metadevice_type", None) or ()):
        return False
    if device.address_type in STABLE_ADDRESS_TYPES:
        return True
    if device.address_type in SHAPE_ROTATING_TYPES:
        return _heard_steadily(device, now)
    return False


def _usable_address(coordinator: BermudaDataUpdateCoordinator, address: str, now: float | None = None) -> bool:
    known = coordinator.devices.get(address)
    if known is None:
        return False
    return _fixed_address(known, monotonic_time_coarse() if now is None else now)


def _name_looks_mobile(name: str | None) -> bool:
    text = (name or "").lower()
    return any(word in text for word in MOVING_DEVICE_WORDS)


def receiver_name(scanner: object | None, address: str) -> str:
    """
    How to show a receiver (scanner) to a person.

    Its Home Assistant device name (as the user named it, then as its
    integration named it), then the Bluetooth scanner's own name, and only
    then its address.
    """
    if scanner is not None:
        ha_scanner = getattr(scanner, "_hascanner", None)
        for name in (
            getattr(scanner, "name_by_user", None),
            getattr(scanner, "name_devreg", None),
            getattr(ha_scanner, "name", None),
            getattr(scanner, "name", None),
        ):
            if isinstance(name, str) and not name_is_address(name):
                return name.strip()
    return address.upper()


def discover(hass: HomeAssistant, coordinator: BermudaDataUpdateCoordinator) -> Discovery:  # noqa: C901
    """Find landmarks and scanners. Must run in the event loop."""
    devreg = dr.async_get(hass)
    entreg = er.async_get(hass)
    found = Discovery()

    areareg = ar.async_get(hass)

    def area_name(area_id: str | None) -> str:
        if area_id is None:
            return ""
        area = areareg.async_get_area(area_id)
        return area.name if area is not None else area_id

    scanner_addresses = set()
    for address in sorted(coordinator.scanner_list):
        scanner = coordinator.devices.get(address)
        area = scanner.area_id if scanner is not None else None
        found.scanners.append(Scanner(address, area, receiver_name(scanner, address)))
        scanner_addresses.add(address.lower())
        if scanner is not None:
            for alt in (scanner.address_ble_mac, scanner.address_wifi_mac):
                if alt:
                    scanner_addresses.add(alt.lower())
        if area:
            found.area_names[area] = area_name(area)

    tracked = {a.lower() for a in coordinator.options.get(CONF_DEVICES, [])}
    our_entry = coordinator.config_entry.entry_id if coordinator.config_entry else None
    seen_addresses: set[str] = set()

    for device in devreg.devices.values():
        name = device.name_by_user or device.name or device.id
        bt_addresses = [c[1].lower() for c in device.connections if c[0] == dr.CONNECTION_BLUETOOTH]
        mac_addresses = [c[1].lower() for c in device.connections if c[0] == dr.CONNECTION_NETWORK_MAC]
        if not bt_addresses and not mac_addresses:
            continue
        if our_entry is not None and our_entry in device.config_entries:
            continue  # Roomwise's own devices are things it tracks
        if any(ident[0] == DOMAIN_PRIVATE_BLE_DEVICE for ident in device.identifiers):
            found.skipped.append((name, "private Bluetooth device (rotating address)"))
            continue

        # Pick the address the coordinator actually hears.
        address = None
        for candidate in bt_addresses:
            if _usable_address(coordinator, candidate):
                address = candidate
                break
        how = ""
        if address is None:
            how = "matched from its network address, please check"
            for mac in mac_addresses:
                for off in MAC_NEIGHBOUR_OFFSETS:
                    candidate = mac_math_offset(mac, off)
                    if candidate and _usable_address(coordinator, candidate):
                        address = candidate
                        break
                if address is not None:
                    break
        if address is None:
            if any(a in coordinator.devices for a in bt_addresses):
                found.skipped.append((name, "rotating or beacon address"))
            elif bt_addresses:
                found.skipped.append((name, "not heard by any scanner"))
            continue
        if address in scanner_addresses or any(a in scanner_addresses for a in bt_addresses + mac_addresses):
            continue  # a scanner itself
        if device.area_id is None:
            found.skipped.append((name, "no room set"))
            continue
        if address in tracked:
            found.skipped.append((name, "tracked by Roomwise as a moving device"))
            continue
        if _looks_mobile(device):
            found.skipped.append((name, "looks like something that moves"))
            continue
        if any(
            ent.domain == "device_tracker" and ent.platform != DOMAIN
            for ent in er.async_entries_for_device(entreg, device.id)
        ):
            found.skipped.append((name, "has a device tracker, so it moves"))
            continue
        if address in seen_addresses:
            continue
        seen_addresses.add(address)
        found.landmarks.append(Landmark(address, device.area_id, name))
        found.landmark_names[address] = name
        if how:
            found.how[address] = how
        found.area_names[device.area_id] = area_name(device.area_id)
    return found


def current_settings(options: dict, scanner_ids: list[str]) -> Settings:
    """Read the live calibration from the options, the same way the coordinator does."""
    offsets = options.get(CONF_RSSI_OFFSETS, {}) or {}
    return Settings(
        ref_power=float(options.get(CONF_REF_POWER, DEFAULT_REF_POWER)),
        attenuation=float(options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION)),
        max_radius=float(options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)),
        offsets={sid: offsets.get(sid, 0) for sid in scanner_ids},
        radii={sid: float(r) for sid, r in (options.get(CONF_SCANNER_MAX_RADIUS, {}) or {}).items()},
    )


def settings_to_options(report: TuneReport, options: dict) -> dict:
    """The option values to save, in the same shape manual calibration saves them."""
    new = dict(options.get(CONF_RSSI_OFFSETS, {}) or {})
    for sid, value in report.proposed.offsets.items():
        new[sid] = max(min(round(value), 127), -127)  # same clip as calibration2 (#497)
    out = {CONF_RSSI_OFFSETS: new}
    if report.proposed.radii or options.get(CONF_SCANNER_MAX_RADIUS):
        out[CONF_SCANNER_MAX_RADIUS] = dict(report.proposed.radii)
    if report.tuned_ref_power and report.proposed.ref_power != report.current.ref_power:
        out[CONF_REF_POWER] = report.proposed.ref_power
    return out


class SampleCollector:
    """
    Records (landmark, scanner, rssi, stamp) from the coordinator's own data.

    Memory is bounded: each (landmark, scanner) pair keeps at most
    max_per_pair readings, chosen uniformly over the whole run (reservoir
    sampling, seeded so a run is repeatable), and no more than max_total
    readings are held overall. When the overall limit stops new pairs from
    growing, `capped` is set so the result can say so.
    """

    def __init__(
        self,
        coordinator: BermudaDataUpdateCoordinator,
        addresses: list[str],
        max_per_pair: int | None = None,
        max_total: int | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.addresses = [a.lower() for a in addresses]
        self.max_per_pair = MAX_SAMPLES_PER_PAIR if max_per_pair is None else max_per_pair
        self.max_total = MAX_TOTAL_SAMPLES if max_total is None else max_total
        self.stored = 0
        self.capped = False
        self._pairs: dict[tuple[str, str], list[Sample]] = {}
        self._seen: dict[tuple[str, str], int] = {}
        self._last: dict[tuple[str, str], float] = {}
        self._rng = random.Random(0)  # noqa: S311 - sampling, not security
        self._unsub = None

    @property
    def samples(self) -> list[Sample]:
        """Every kept reading. Call after stop() (or on the event loop)."""
        return [sample for kept in self._pairs.values() for sample in kept]

    def start(self) -> None:
        self._unsub = self.coordinator.async_add_listener(self.poll)
        self.poll()

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def _keep(self, key: tuple[str, str], sample: Sample) -> None:
        kept = self._pairs.setdefault(key, [])
        seen = self._seen.get(key, 0) + 1
        self._seen[key] = seen
        if len(kept) < self.max_per_pair:
            if self.stored >= self.max_total:
                self.capped = True
                return
            kept.append(sample)
            self.stored += 1
            return
        slot = self._rng.randrange(seen)
        if slot < self.max_per_pair:
            kept[slot] = sample

    @callback
    def poll(self) -> None:
        """Take any adverts that arrived since the last coordinator update."""
        for address in self.addresses:
            device = self.coordinator.devices.get(address)
            if device is None:
                continue
            for advert in list(device.adverts.values()):
                if advert.rssi is None or not advert.stamp:
                    continue
                key = (address, advert.scanner_address)
                if self._last.get(key) == advert.stamp:
                    continue
                self._last[key] = advert.stamp
                self._keep(key, Sample(address, advert.scanner_address, float(advert.rssi), advert.stamp))


@dataclass
class Candidate:
    """An address Roomwise hears that could be a landmark, for the user to confirm."""

    address: str
    label: str
    area_id: str | None  # suggested room (saved choice, else from the device registry)
    suggested: bool  # pre-ticked


def candidates(
    coordinator: BermudaDataUpdateCoordinator,
    found: Discovery,
    saved: dict[str, str] | None = None,
    max_age: float = 600,
    min_readings: int = 3,
    limit: int = 200,
) -> list[Candidate]:
    """
    Fixed-address devices heard recently and steadily, for the manual landmark list.

    Address class alone is not trusted: an address that looks rotating is
    offered when the same address has been heard for longer than a private
    address lives (see _fixed_address). Rotating private addresses, known
    private BLE devices, beacons, tracked devices and names that look like
    things that move are left out.

    Most stationary Bluetooth devices have no Bluetooth link in the device
    registry, so the registry can only pre-fill this list; the user ticks the
    devices that never move and picks their rooms.
    """
    saved = {k.lower(): v for k, v in (saved or {}).items()}
    registry = {lm.id: lm for lm in found.landmarks}
    tracked = {a.lower() for a in coordinator.options.get(CONF_DEVICES, [])}
    now = monotonic_time_coarse()
    out: list[Candidate] = []
    for address, device in coordinator.devices.items():
        if address in saved or address in registry:
            pass  # always offer what the user or registry already named
        else:
            if address in tracked or not _fixed_address(device, now):
                continue
            if _name_looks_mobile(getattr(device, "name", None)):
                continue
            last_seen = getattr(device, "last_seen", 0) or 0
            if now - last_seen > max_age:
                continue
            readings = max((len(getattr(a, "hist_rssi", []) or []) for a in device.adverts.values()), default=0)
            if readings < min_readings:
                continue
        reg = registry.get(address)
        name = device.name
        label = f"{reg.name} ({name})" if reg is not None and reg.name != name else name
        if address.upper() not in label.upper():
            label = f"{label} [{address.upper()}]"
        if address in found.how:
            label += f" - {found.how[address]}"
        area = saved.get(address) or (reg.area if reg else None)
        out.append(Candidate(address, label, area, address in saved or reg is not None))
    for address, area in saved.items():  # saved landmarks not heard right now
        if address not in coordinator.devices:
            out.append(Candidate(address, f"[{address.upper()}] (saved, not heard now)", area, True))
    out.sort(key=lambda c: (not c.suggested, c.label.lower()))
    return out[:limit]


async def async_run_autotune(
    hass: HomeAssistant,
    coordinator: BermudaDataUpdateCoordinator,
    options: dict,
    seconds: float,
    landmarks: list[Landmark],
    scanners: list[Scanner],
    area_names: dict[str, str] | None = None,
    export_path: str | None = None,
    tune_options: TuneOptions | None = None,
) -> TuneReport:
    """
    Collect for `seconds` from the coordinator's own adverts, then tune (off the event loop).

    If the task is cancelled (the user closed the dialog), the tuner running in
    the executor is told to stop at its next check instead of running to the end.
    """
    collector = SampleCollector(coordinator, [lm.id for lm in landmarks])
    collector.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        collector.stop()
    settings = current_settings(options, [s.id for s in scanners])
    stop = threading.Event()
    tune_options = replace(tune_options or TuneOptions(), should_stop=stop.is_set)
    try:
        if export_path:
            await hass.async_add_executor_job(
                _write_export, export_path, landmarks, scanners, settings, collector, area_names
            )
        report = await hass.async_add_executor_job(
            _tune, landmarks, scanners, collector, settings, tune_options, area_names
        )
    except asyncio.CancelledError:
        stop.set()
        raise
    if collector.capped:
        report.summary += " " + CAPPED_NOTE.format(count=collector.stored)
    return report


def _tune(
    landmarks: list[Landmark],
    scanners: list[Scanner],
    collector: SampleCollector,
    settings: Settings,
    tune_options: TuneOptions,
    area_names: dict[str, str] | None,
) -> TuneReport:
    """Executor job: the collector is stopped, so its readings no longer change."""
    return evaluate(landmarks, scanners, collector.samples, settings, tune_options, area_names=area_names)


def _write_export(
    path: str,
    landmarks: list[Landmark],
    scanners: list[Scanner],
    settings: Settings,
    collector: SampleCollector,
    area_names: dict[str, str] | None,
) -> None:
    """Executor job: build and write the export file (both can take a while on a big run)."""
    _write_json(path, export_dict(landmarks, scanners, settings, collector.samples, area_names))


def _write_json(path: str, data: dict) -> None:
    with Path(path).open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)

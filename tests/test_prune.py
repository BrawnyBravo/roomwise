"""Regression tests for BermudaDataUpdateCoordinator.prune_devices."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.roomwise.coordinator as coordinator_module
from custom_components.roomwise.const import (
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_OTHER,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    METADEVICE_IBEACON_DEVICE,
    PRUNE_TIME_DEFAULT,
)
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

NOW = 100_000.0


class _Dev(SimpleNamespace):
    """Device stub. Hashable by address, as the real BermudaDevice is."""

    def __hash__(self):
        return hash(self.address)


def _dev(address, last_seen, address_type, **kwargs):
    fields = {
        "address": address,
        "name": address,
        "last_seen": last_seen,
        "address_type": address_type,
        "create_sensor": False,
        "is_scanner": False,
        "metadevice_type": set(),
        "metadevice_sources": [],
        "adverts": {},
    }
    fields.update(kwargs)
    return _Dev(**fields)


def _coordinator(devices, metadevices=None):
    return SimpleNamespace(
        devices=devices,
        metadevices=metadevices if metadevices is not None else {},
        scanner_list=set(),
        stamp_last_prune=0,
        redactions={},
        stamp_redactions_expiry=None,
        irk_manager=SimpleNamespace(async_prune=lambda: None),
        _get_device=devices.get,
    )


def test_prune_rotated_private_ble_source_does_not_raise_and_cleans_adverts(monkeypatch):
    """A rotated Private BLE source older than 16 min must be pruned once, cleanly.

    It satisfies both the metadevice-source sweep (older than PRUNE_TIME_KNOWN_IRK)
    and the unknown-IRK sweep (older than PRUNE_TIME_UNKNOWN_IRK), so it used to be
    put on the prune list twice; the second ``del`` raised KeyError, aborting the
    update and skipping the clean-up of the metadevice's sources and adverts.
    """
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    old = "4a:00:00:00:00:0a"
    new = "4b:00:00:00:00:0b"
    dev_old = _dev(old, NOW - 20 * 60, BDADDR_TYPE_RANDOM_RESOLVABLE)
    dev_new = _dev(new, NOW - 5, BDADDR_TYPE_RANDOM_RESOLVABLE)
    meta = _dev(
        "0123456789abcdef0123456789abcdef",
        NOW - 5,
        "private_ble_device",
        create_sensor=True,
        metadevice_sources=[new, old],
        adverts={(old, "scanner1"): SimpleNamespace(device_address=old, stamp=NOW - 1200)},
    )
    devices = {old: dev_old, new: dev_new, meta.address: meta}
    coordinator = _coordinator(devices, {meta.address: meta})

    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    assert old not in devices
    assert new in devices
    assert meta.address in devices
    assert meta.metadevice_sources == [new]
    assert (old, "scanner1") not in meta.adverts


@pytest.mark.parametrize(("prunable_count", "quota"), [(2, 1), (0, 1), (3, 4), (3, 3), (3, 2), (3, 5), (5, 1)])
def test_prune_quota_prunes_oldest_up_to_shortfall(monkeypatch, prunable_count, quota):
    """Over quota, prune exactly the shortfall (or all prunables if fewer), oldest first.

    The quota path indexed ``sorted_addresses[shortfall - 1]`` (IndexError when there
    were fewer prunables than the shortfall) and sliced one short of the shortfall.
    """
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    monkeypatch.setattr(coordinator_module, "PRUNE_MAX_COUNT", quota)
    devices = {
        "tracked": _dev("tracked", NOW, "static", create_sensor=True),
        "also_tracked": _dev("also_tracked", NOW, "static", create_sensor=True),
    }
    # Fresh static addresses: not stale, so only prunable to meet quota.
    # Inserted newest first so the test also checks the oldest go first.
    for index in reversed(range(prunable_count)):
        address = f"c0:00:00:00:00:{index:02x}"
        devices[address] = _dev(address, NOW - 100 + index, "static")
    coordinator = _coordinator(devices)

    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    shortfall = prunable_count + 2 - quota
    prune_count = min(prunable_count, max(0, shortfall))
    assert set(devices) == {"tracked", "also_tracked"} | {
        f"c0:00:00:00:00:{index:02x}" for index in range(prune_count, prunable_count)
    }


def _beacon(address, source, last_seen, tracked=False):
    return _dev(
        address,
        last_seen,
        BDADDR_TYPE_NOT_MAC48,
        metadevice_type={METADEVICE_IBEACON_DEVICE},
        create_sensor=tracked,
        metadevice_sources=[source],
    )


def test_stale_untracked_ibeacon_metadevice_and_source_are_pruned(monkeypatch):
    """An untracked iBeacon unseen for a day goes, with its metadevice and source.

    Nothing ever removed entries from ``metadevices``, and the source-keeper sweep
    always keeps a metadevice's newest source, so every iBeacon ever heard stayed
    in memory for good. Fresh or tracked beacons must stay.
    """
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    old = NOW - PRUNE_TIME_DEFAULT - 10
    stale_src = _dev("11:22:33:44:55:01", old, BDADDR_TYPE_OTHER)
    fresh_src = _dev("11:22:33:44:55:02", NOW - 10, BDADDR_TYPE_OTHER)
    tracked_src = _dev("11:22:33:44:55:03", old, BDADDR_TYPE_OTHER)
    stale = _beacon("aaaa_1_1", stale_src.address, old)
    fresh = _beacon("bbbb_1_1", fresh_src.address, NOW - 10)
    tracked = _beacon("cccc_1_1", tracked_src.address, old, tracked=True)
    devices = {d.address: d for d in (stale_src, fresh_src, tracked_src, stale, fresh, tracked)}
    metadevices = {d.address: d for d in (stale, fresh, tracked)}
    coordinator = _coordinator(devices, metadevices)

    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    assert set(metadevices) == {"bbbb_1_1", "cccc_1_1"}
    assert "aaaa_1_1" not in devices
    # The stale beacon's source is no longer a keeper, so it prunes as a stale MAC.
    assert stale_src.address not in devices
    assert fresh_src.address in devices
    assert tracked_src.address in devices


def test_general_sweep_never_prunes_a_registered_metadevice(monkeypatch):
    """The general sweep must skip anything registered in ``metadevices``.

    ``metadevices`` is keyed by address, but the sweep tested the device object
    against it, which never matches, so a registered metadevice could be deleted
    from ``devices`` while staying in ``metadevices`` (the two drifting apart).
    """
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    # A non-iBeacon metadevice whose address looks like a MAC and is a day stale.
    meta = _dev("aa:bb:cc:dd:ee:01", NOW - PRUNE_TIME_DEFAULT - 10, BDADDR_TYPE_OTHER, metadevice_type={"other"})
    devices = {meta.address: meta}
    metadevices = {meta.address: meta}
    coordinator = _coordinator(devices, metadevices)

    BermudaDataUpdateCoordinator.prune_devices(coordinator, force_pruning=True)

    assert meta.address in metadevices
    assert meta.address in devices

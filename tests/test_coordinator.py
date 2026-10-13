"""Tests for BermudaDataUpdateCoordinator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator


def test_handle_devreg_malformed_identifier():
    """A malformed device identifier must not crash the devreg handler.

    Regression test: Home Assistant device identifiers are expected to be
    ``(domain, id)`` 2-tuples, but a buggy integration can register a
    malformed one (observed in the wild: a Plejd device whose id string was
    stored as many single-character elements). Bermuda unpacked every
    identifier directly, so such a device raised
    ``ValueError: too many values to unpack`` and broke the entire
    ``device_registry_updated`` handler on every registry change.

    The handler must skip malformed identifiers, still process valid ones,
    and run to completion.
    """
    # A device with a non-Roomwise connection (so we reach the identifier
    # branch), one malformed identifier and one valid Roomwise identifier.
    device_entry = SimpleNamespace(
        connections={("mac", "AA:BB:CC:DD:EE:FF")},
        identifiers={
            ("plejd", "D", "8", "9", "D", "F", "D", "A"),  # malformed: not a 2-tuple
            ("roomwise", "aa:bb:cc:dd:ee:ff"),  # valid (domain, id)
        },
        name_by_user=None,
    )

    # Lightweight stand-in for the coordinator; we invoke the real (unbound)
    # handler with it as ``self`` to avoid setting up the full integration.
    coordinator = SimpleNamespace(
        devices={},
        dr=SimpleNamespace(async_get=lambda device_id: device_entry),
        _scanner_init_pending=False,
        _do_private_device_init=False,
    )

    event = SimpleNamespace(data={"action": "update", "device_id": "malformed-device", "changes": {}})

    # Previously raised ValueError: too many values to unpack (expected 2).
    BermudaDataUpdateCoordinator.handle_devreg_changes(coordinator, event)

    # Reached the end of the identifier branch without raising.
    assert coordinator._scanner_init_pending is True


def test_gather_passes_bluetooth_address_details_to_device():
    """Each advert's BLEDevice details reach the device, so public addresses are recognised."""
    details = {"props": {"AddressType": "public"}}
    bledevice = SimpleNamespace(address="4a:bb:cc:dd:ee:ff", details=details)
    advert = SimpleNamespace(rssi=-60)
    scanner_device = MagicMock()
    scanner_device.async_as_scanner_get_stamp.return_value = None
    ha_scanner = SimpleNamespace(
        source="scanner-1",
        discovered_devices_and_advertisement_data={bledevice.address: (bledevice, advert)},
    )
    device = MagicMock()
    coordinator = SimpleNamespace(
        _scanner_init_pending=False,
        _hascanners=[ha_scanner],
        _get_device=lambda address: scanner_device,
        _get_or_create_device=lambda address: device,
        stamp_last_update_started=0,
    )

    BermudaDataUpdateCoordinator._async_gather_advert_data(coordinator)

    device.async_apply_address_details.assert_called_once_with(details)
    device.process_advertisement.assert_called_once_with(scanner_device, advert)

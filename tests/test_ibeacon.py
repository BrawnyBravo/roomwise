"""iBeacon parsing: identity changes, alternating frames and short frames."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from bleak.backends.scanner import AdvertisementData
from homeassistant.core import HomeAssistant

from custom_components.roomwise.const import METADEVICE_TYPE_IBEACON_SOURCE

BEACON_MAC = "c0:00:00:00:00:01"
SCANNER = "aa:bb:cc:dd:ee:10"
UUID = "e2c56db5dffb48d2b060d0f5a71096e0"
ID_A = f"{UUID}_1_1"
ID_B = f"{UUID}_1_2"


def _ibeacon(minor: int, *, power: bool = True) -> bytes:
    frame = b"\x02\x15" + bytes.fromhex(UUID) + (1).to_bytes(2, "big") + minor.to_bytes(2, "big")
    return frame + (b"\xc5" if power else b"")


class _Feeder:
    """Feeds adverts from one beacon, heard by one (local) receiver, into the real coordinator."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.scanner = coordinator._get_or_create_device(SCANNER)
        self.source = coordinator._get_or_create_device(BEACON_MAC)
        self.rssi = -60

    def send(self, manufacturer_data: dict[int, bytes]):
        # Local receivers only count an advert as new when the RSSI changes.
        self.rssi = -60 if self.rssi != -60 else -61
        self.source.process_advertisement(
            self.scanner,
            AdvertisementData(
                local_name=None,
                manufacturer_data=manufacturer_data,
                service_data={},
                service_uuids=[],
                tx_power=None,
                rssi=self.rssi,
                platform_data=(),
            ),
        )


@pytest.fixture
def feeder(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    return _Feeder(setup_bermuda_entry.runtime_data.coordinator)


async def test_beacon_that_changes_identity_follows_the_newest_one(feeder):
    feeder.send({76: _ibeacon(1)})
    assert feeder.source.beacon_unique_id == ID_A

    feeder.send({76: _ibeacon(2)})
    assert feeder.source.beacon_unique_id == ID_B

    coordinator = feeder.coordinator
    coordinator.update_metadevices()
    # The live adverts go to the new identity, and the old one lets go of the source.
    assert BEACON_MAC in coordinator.metadevices[ID_B].metadevice_sources
    assert BEACON_MAC not in coordinator.metadevices[ID_A].metadevice_sources

    # Changing back again is followed too.
    feeder.send({76: _ibeacon(1)})
    assert feeder.source.beacon_unique_id == ID_A


async def test_alternating_frames_do_not_reregister_the_beacon(feeder):
    """A beacon alternating an iBeacon frame with another frame is not re-matched on every packet."""
    other = {76: b"\x10\x05\x01\x18\x00\x00\x00"}  # an Apple "nearby" frame, not an iBeacon
    feeder.send({76: _ibeacon(1)})
    with patch.object(feeder.coordinator, "register_ibeacon_source") as register:
        for _ in range(10):
            feeder.send(other)
            feeder.send({76: _ibeacon(1)})
        register.assert_not_called()
    assert feeder.source.beacon_unique_id == ID_A
    # The history holds each distinct frame once, newest first.
    advert = next(iter(feeder.source.adverts.values()))
    assert advert.manufacturer_data == [{76: _ibeacon(1)}, other]


async def test_short_apple_frame_is_not_treated_as_an_ibeacon(feeder):
    """Too short to hold uuid/major/minor: no shared "None_None_None" beacon, no error log."""
    with patch.object(feeder.coordinator, "register_ibeacon_source") as register:
        feeder.send({76: b"\x02\x15" + b"\x00" * 10})
        register.assert_not_called()
    assert feeder.source.beacon_unique_id is None
    assert METADEVICE_TYPE_IBEACON_SOURCE not in feeder.source.metadevice_type
    assert "None_None_None" not in feeder.coordinator.metadevices


async def test_ibeacon_without_power_byte_clears_stale_power(feeder):
    feeder.send({76: _ibeacon(1)})
    assert feeder.source.beacon_power == -59
    feeder.send({76: _ibeacon(2, power=False)})
    assert feeder.source.beacon_unique_id == ID_B
    assert feeder.source.beacon_power is None


async def test_metadevice_takes_missing_details_from_its_source(feeder):
    """Names and manufacturer the beacon metadevice lacks are filled in from the device sending it."""
    feeder.send({76: _ibeacon(1)})
    metadevice = feeder.coordinator.metadevices[ID_A]
    metadevice.manufacturer = None
    feeder.source.manufacturer = "Test Beacons Ltd"
    feeder.source.name_bt_local_name = "Test Beacon"

    feeder.coordinator.update_metadevices()

    assert metadevice.manufacturer == "Test Beacons Ltd"
    assert metadevice.name_bt_local_name == "Test Beacon"
    assert metadevice.beacon_unique_id == ID_A  # identity untouched

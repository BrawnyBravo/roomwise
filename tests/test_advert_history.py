"""Advert history details: service-data change detection and the first interval."""

from __future__ import annotations

from unittest.mock import MagicMock

from bleak.backends.scanner import AdvertisementData

from custom_components.roomwise.bermuda_advert import BermudaAdvert


def _data(rssi, service_data):
    return AdvertisementData(
        local_name=None,
        manufacturer_data={},
        service_data=service_data,
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )


def _advert(first):
    parent = MagicMock(ref_power=0, address="aa:bb:cc:dd:ee:ff", name_bt_local_name=None)
    scanner = MagicMock(address="aa:bb:cc:dd:ee:10", is_remote_scanner=False, last_seen=0.0)
    return parent, scanner, BermudaAdvert(parent, first, {}, scanner)


def test_repeated_service_data_does_not_rebuild_the_name():
    a = {"0000fe9f-0000-1000-8000-00805f9b34fb": b"\x01"}
    b = {"0000fe9f-0000-1000-8000-00805f9b34fb": b"\x02"}
    parent, scanner, advert = _advert(_data(-60, a))
    advert.update_advertisement(_data(-61, b), scanner)  # new: name may change
    parent.make_name.reset_mock()
    for i in range(6):
        advert.update_advertisement(_data(-62 - i, a if i % 2 == 0 else b), scanner)
    parent.make_name.assert_not_called()


def test_first_interval_is_unknown_not_the_clock():
    parent, scanner, advert = _advert(_data(-60, {}))
    assert advert.hist_interval == [None]  # was the whole monotonic clock (days of seconds)
    advert.stamp -= 2.0  # pretend the first advert was 2 s ago
    advert.update_advertisement(_data(-65, {}), scanner)
    assert 0 < advert.hist_interval[0] < 10

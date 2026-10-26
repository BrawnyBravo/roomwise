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


def _calc(hist_stamp, hist_distance, by_interval, raw, *, max_velocity=3.0, samples=5):
    """Run one calculate_data cycle on an advert that is already tracking (has a distance)."""
    _parent, _scanner, advert = _advert(_data(-60, {}))
    advert.conf_max_velocity = max_velocity
    advert.conf_smoothing_samples = samples
    advert.hist_stamp = list(hist_stamp)
    advert.hist_distance = list(hist_distance)
    advert.hist_distance_by_interval = list(by_interval)
    advert.rssi_distance = by_interval[0]
    advert.rssi_distance_raw = raw
    advert.new_stamp = hist_stamp[0]
    advert.calculate_data()
    return advert


def test_reading_that_retreats_too_fast_is_ignored():
    # 1 m to 10 m in one second is 9 m/s, over the 3 m/s limit: the last smoothed value is repeated.
    advert = _calc([10.0, 9.0], [10.0, 1.0], [1.0], raw=10.0)
    assert advert.hist_velocity[0] == 9.0
    assert advert.hist_distance_by_interval == [1.0, 1.0]
    assert advert.rssi_distance == 1.0


def test_approach_is_never_filtered():
    advert = _calc([10.0, 9.0], [1.0, 10.0], [10.0], raw=1.0)
    assert advert.hist_velocity[0] == -9.0
    assert advert.hist_distance_by_interval == [1.0, 10.0]
    assert advert.rssi_distance == 1.0


def test_smoothing_hugs_the_closer_readings():
    # New raw 2.0 m after 1.0 and 3.0: running minimum 2, 1, 1 -> average 4/3.
    advert = _calc([10.0, 9.0], [2.0, 2.0], [1.0, 3.0], raw=2.0)
    assert advert.hist_distance_by_interval == [2.0, 1.0, 3.0]
    assert abs(advert.rssi_distance - 4 / 3) < 1e-9


def test_smoothing_window_is_limited_to_the_sample_count():
    advert = _calc([10.0, 9.0], [5.0, 5.0], [5.0] * 6, raw=5.0, samples=4)
    assert len(advert.hist_distance_by_interval) == 4
    assert advert.rssi_distance == 5.0

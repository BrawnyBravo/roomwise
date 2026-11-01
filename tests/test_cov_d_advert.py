"""Coverage for BermudaAdvert: scanner swaps, stale stamps, USB adaptors, smoothing edge cases."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from bleak.backends.scanner import AdvertisementData
from bluetooth_data_tools import monotonic_time_coarse

from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.const import (
    CONF_ATTENUATION,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_SMOOTHING_SAMPLES,
)

SCANNER_ADDRESS = "11:22:33:44:55:66"
DEVICE_ADDRESS = "aa:bb:cc:dd:ee:ff"


def _options(**over):
    base = {CONF_REF_POWER: -55, CONF_ATTENUATION: 3.0, CONF_MAX_VELOCITY: 3.0, CONF_SMOOTHING_SAMPLES: 5}
    base.update(over)
    return base


def _device():
    calls = SimpleNamespace(names=0, manufacturer=0)
    dev = SimpleNamespace(
        address=DEVICE_ADDRESS,
        ref_power=0,
        name="Test Device",
        name_bt_local_name=None,
        create_sensor=False,
        calls=calls,
    )
    dev.process_manufacturer_data = lambda advert: setattr(calls, "manufacturer", calls.manufacturer + 1)
    dev.make_name = lambda: setattr(calls, "names", calls.names + 1)
    return dev


def _scanner(address=SCANNER_ADDRESS, remote=True, stamp=100.0):
    scn = SimpleNamespace(
        address=address,
        name="Test Proxy",
        area_id="hall",
        area_name="Hall",
        is_remote_scanner=remote,
        last_seen=0.0,
        stamp=stamp,
    )
    scn.async_as_scanner_get_stamp = lambda _address: scn.stamp
    return scn


def _ad(rssi=-60):
    return AdvertisementData(
        local_name=None,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=rssi,
        platform_data=(),
    )


def _advert(scanner=None, device=None, **option_overrides):
    scanner = scanner or _scanner()
    device = device or _device()
    advert = BermudaAdvert(device, _ad(), _options(**option_overrides), scanner)
    return advert, device, scanner


def test_adverts_hash_by_device_and_receiver():
    advert, _device_, _scanner_ = _advert()
    assert hash(advert) == hash((DEVICE_ADDRESS, SCANNER_ADDRESS))


def test_a_scanner_with_the_wrong_address_is_reported(caplog):
    advert, _device_, _scanner_ = _advert()
    caplog.set_level(logging.ERROR)
    advert.apply_new_scanner(_scanner(address="99:99:99:99:99:99"))
    assert "wrong address" in caplog.text


def test_a_new_scanner_object_replaces_the_stale_one():
    advert, _device_, scanner = _advert()
    fresh = _scanner(stamp=110.0)
    advert.update_advertisement(_ad(-61), fresh)
    assert advert.scanner_device is fresh
    assert advert.stamp == 110.0
    assert advert.hist_rssi[0] == -61
    assert advert.scanner_device is not scanner


def test_an_older_stamp_than_recorded_is_ignored():
    advert, _device_, scanner = _advert()
    assert advert.stamp == 100.0
    scanner.stamp = 90.0
    advert.update_advertisement(_ad(-70), scanner)
    assert advert.stamp == 100.0
    assert advert.stale_update_count == 1
    assert advert.hist_rssi == [-60]


def test_a_usb_adaptor_with_an_unchanged_reading_adds_nothing():
    advert, _device_, scanner = _advert(scanner=_scanner(remote=False))
    assert advert.hist_rssi == [-60]
    advert.update_advertisement(_ad(-60), scanner)
    assert advert.hist_rssi == [-60]
    assert len(advert.hist_stamp) == 1
    # a changed reading is taken
    advert.update_advertisement(_ad(-64), scanner)
    assert advert.hist_rssi == [-64, -60]


def test_overriding_a_distance_with_no_history_starts_the_history():
    advert, _device_, _scanner_ = _advert()
    advert.rssi_distance = 9.0
    advert.hist_distance.clear()
    advert.hist_distance_by_interval.clear()
    new = advert._update_raw_distance(reading_is_new=False)
    assert advert.rssi_distance == new
    assert advert.hist_distance == [new]
    assert advert.hist_distance_by_interval == []


def test_setting_the_same_reference_power_returns_the_current_raw_distance():
    advert, _device_, _scanner_ = _advert()
    raw = advert.rssi_distance_raw
    assert advert.set_ref_power(advert.ref_power) == raw
    changed = advert.set_ref_power(-45)
    assert changed != raw
    assert advert.ref_power == -45


def test_a_device_gone_quiet_is_away_and_its_smoothing_history_is_dropped():
    advert, _device_, _scanner_ = _advert()
    advert.calculate_data()  # arrives
    assert advert.rssi_distance is not None
    assert advert.hist_distance_by_interval
    advert.stamp = monotonic_time_coarse() - 1000
    advert.calculate_data()  # no new stamp, long ago
    assert advert.rssi_distance is None
    assert advert.hist_distance_by_interval == []


def _mid_life(advert, **state):
    """Put an advert in the 'already tracking' state so calculate_data takes the smoothing branch."""
    advert.rssi_distance = 2.0
    advert.new_stamp = 1.0
    for key, value in state.items():
        setattr(advert, key, value)


def test_history_entries_without_a_usable_time_are_skipped_in_the_velocity_walk():
    advert, _device_, _scanner_ = _advert()
    _mid_life(
        advert,
        rssi_distance_raw=2.0,
        # newest first: a missing stamp, a repeat of the newest stamp, then a genuinely older one
        hist_stamp=[100.0, 90.0, None, 100.0, 50.0],
        hist_distance=[2.0, 2.0, 2.0, 2.0, 2.0],
        hist_distance_by_interval=[2.0],
    )
    advert.calculate_data()
    assert advert.hist_velocity[0] == 0
    assert advert.rssi_distance == 2.0


def test_a_too_fast_reading_with_no_smoothing_history_uses_the_raw_distance():
    advert, _device_, _scanner_ = _advert()
    _mid_life(
        advert,
        rssi_distance_raw=50.0,
        hist_stamp=[100.0, 99.0],
        hist_distance=[50.0, 1.0],  # 49 m in one second
        hist_distance_by_interval=[],
    )
    advert.calculate_data()
    assert advert.hist_velocity[0] == 49.0
    assert advert.hist_distance_by_interval == [50.0]
    assert advert.rssi_distance == 50.0


def test_with_zero_smoothing_samples_the_raw_distance_is_used():
    advert, _device_, _scanner_ = _advert(**{CONF_SMOOTHING_SAMPLES: 0})
    _mid_life(
        advert,
        rssi_distance_raw=4.0,
        hist_stamp=[100.0],
        hist_distance=[4.0],
        hist_distance_by_interval=[],
    )
    advert.calculate_data()
    assert advert.hist_distance_by_interval == []
    assert advert.rssi_distance == 4.0

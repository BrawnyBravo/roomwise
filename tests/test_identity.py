"""Devices and adverts are compared by identity: two different ones are never 'equal'."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from bluetooth_data_tools import monotonic_time_coarse

from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator


def _scanner(address, area):
    return MagicMock(
        address=address, area_id=area, area_name=area, is_remote_scanner=False, last_seen=monotonic_time_coarse()
    )


def _advert(scanner, distance):
    parent = MagicMock(ref_power=0, address="11:22:33:44:55:66", name_bt_local_name=None)
    data = MagicMock(rssi=-60, local_name=None, manufacturer_data={}, service_data={}, service_uuids=[])
    advert = BermudaAdvert(parent, data, {}, scanner)
    advert.stamp = monotonic_time_coarse()
    advert.rssi_distance = distance
    advert.hist_distance_by_interval = [distance] * 5
    return advert


def test_distinct_devices_and_adverts_are_not_equal():
    coordinator = MagicMock(options={})
    a = BermudaDevice("aa:bb:cc:dd:ee:01", coordinator)
    b = BermudaDevice("aa:bb:cc:dd:ee:02", coordinator)
    assert a != b
    assert a != {}
    assert a == a  # noqa: PLR0124

    kitchen = _advert(_scanner("aa:bb:cc:dd:ee:10", "Kitchen"), 2.0)
    office = _advert(_scanner("aa:bb:cc:dd:ee:20", "Office"), 3.0)
    assert kitchen != office
    assert [kitchen, office].index(office) == 1


def test_moving_between_receivers_records_the_area_switch():
    kitchen = _advert(_scanner("aa:bb:cc:dd:ee:10", "Kitchen"), 6.0)
    office = _advert(_scanner("aa:bb:cc:dd:ee:20", "Office"), 1.0)  # much closer: wins outright
    chosen = {}
    device = SimpleNamespace(
        name="device",
        area_advert=kitchen,
        area_beyond_range=None,
        adverts={("d", "k"): kitchen, ("d", "o"): office},
        diag_area_switch=None,
        apply_scanner_selection=lambda adv: chosen.setdefault("adv", adv),
    )
    coord = SimpleNamespace(options={"max_area_radius": 20.0}, AreaTests=BermudaDataUpdateCoordinator.AreaTests)
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert chosen["adv"] is office
    assert device.diag_area_switch is not None

"""Coverage for BermudaDevice: odd addresses, scanner refresh, room selection, naming, dumps."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bluetooth_data_tools import monotonic_time_coarse
from homeassistant.components.bluetooth import BaseHaRemoteScanner, BaseHaScanner
from homeassistant.const import STATE_HOME, STATE_NOT_HOME

from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.const import (
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_OTHER,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    DOMAIN,
    ICON_DEFAULT_AREA,
    ICON_DEFAULT_FLOOR,
)


@pytest.fixture
def coordinator():
    coord = MagicMock()
    coord.options = {}
    coord.hass_version_min_2025_4 = True
    return coord


@pytest.fixture
def device(coordinator):
    return BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=coordinator)


def _scanner(spec=BaseHaScanner):
    scanner = MagicMock(spec=spec)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "aa:bb:cc:00:00:01"
    scanner.name = "Test Proxy"
    return scanner


def test_a_name_that_is_not_an_address_is_marked_not_a_mac(coordinator):
    device = BermudaDevice(address="not-an-address", coordinator=coordinator)
    assert device.address_type == BDADDR_TYPE_NOT_MAC48
    # make_name leaves such a device on its default name rather than inventing one
    assert device.make_name() == f"{DOMAIN}_not-an-address".replace("-", "_")


def test_five_colons_but_the_wrong_length_is_treated_as_public(coordinator):
    device = BermudaDevice(address="1:2:3:4:5:6", coordinator=coordinator)
    assert device.address_type == BDADDR_TYPE_OTHER


def test_make_name_falls_back_to_the_address_and_uses_the_maker_when_known(device):
    assert device.make_name() == f"{DOMAIN}_aa_bb_cc_dd_ee_ff"
    device.manufacturer = "Acme Widgets"
    assert device.make_name() == "acme_widgets_aa_bb_cc_dd_ee_ff"


def test_resolving_a_scanner_without_a_bluetooth_scanner_warns_and_stops(device, caplog):
    caplog.set_level(logging.WARNING)
    device.async_as_scanner_resolve_device_entries()
    assert "has no ha_scanner" in caplog.text
    assert device.entry_id is None
    device._coordinator.dr.async_get_devices.assert_not_called()


def test_an_area_whose_floor_is_gone_shows_as_an_invalid_floor(device):
    area = SimpleNamespace(name="Den", icon=None, floor_id="floor_gone")
    device.ar = MagicMock()
    device.ar.async_get_area.return_value = area
    device.fr = MagicMock()
    device.fr.async_get_floor.return_value = None

    device._update_area_and_floor("den")

    assert device.area_name == "Den"
    assert device.area_icon == ICON_DEFAULT_AREA
    assert device.floor_id is None
    assert device.floor_name == "Invalid Floor ID"
    assert device.floor_icon == ICON_DEFAULT_FLOOR
    assert device.floor_level is None


def test_a_replacement_scanner_object_is_taken_on(device, caplog):
    caplog.set_level(logging.INFO)
    first, second = _scanner(), _scanner()
    device.async_as_scanner_init(first)
    device.async_as_scanner_update(second)
    assert device._hascanner is second
    assert "Received replacement ha_scanner object" in caplog.text
    # the message names the device, it does not print a method object
    assert "bound method" not in caplog.text


def test_scanner_time_going_backwards_keeps_the_newer_stamp(device, caplog):
    caplog.set_level(logging.DEBUG)
    scanner = _scanner()
    device.async_as_scanner_init(scanner)
    newer = monotonic_time_coarse() + 100
    device.last_seen = newer
    device.async_as_scanner_update(scanner)
    assert device.last_seen == newer
    assert "went backwards" in caplog.text


def test_older_home_assistant_reads_the_private_timestamp_table(coordinator):
    coordinator.hass_version_min_2025_4 = False
    proxy = BermudaDevice(address="11:22:33:44:55:66", coordinator=coordinator)
    remote = _scanner(BaseHaRemoteScanner)
    remote._discovered_device_timestamps = {"AA:BB:CC:DD:EE:FF": 42.0}
    proxy.async_as_scanner_init(remote)
    assert proxy.stamps == {"AA:BB:CC:DD:EE:FF": 42.0}
    assert proxy.async_as_scanner_get_stamp("aa:bb:cc:dd:ee:ff") == 42.0


def test_a_remote_scanner_without_a_timestamp_table_has_no_stamp(coordinator):
    proxy = BermudaDevice(address="11:22:33:44:55:66", coordinator=coordinator)
    proxy.async_as_scanner_init(_scanner(BaseHaRemoteScanner))
    proxy.stamps = None
    assert proxy.async_as_scanner_get_stamp("aa:bb:cc:dd:ee:ff") is None


def test_the_winning_receiver_sets_the_room_and_distance(device):
    device._update_area_and_floor = MagicMock()
    device.area_name = "Kitchen"
    device.area_id = "kitchen"
    device.area_icon = "mdi:pot"
    advert = SimpleNamespace(area_id="kitchen", rssi_distance=2.5, rssi=-60)
    device.apply_scanner_selection(advert)
    device._update_area_and_floor.assert_called_once_with("kitchen")
    assert device.area_advert is advert
    assert device.area_distance == 2.5
    assert device.area_rssi == -60
    assert device.area_last_seen == "Kitchen"
    assert device.area_last_seen_id == "kitchen"
    assert device.area_last_seen_icon == "mdi:pot"


def test_no_winning_receiver_clears_the_room_and_remembers_the_last_one(device):
    device._update_area_and_floor("nowhere")  # unknown room: sets an "Invalid Area" name
    device.area_advert = object()
    device.area_distance = 3.0
    device.area_rssi = -70
    device.area_last_seen = "Kitchen"
    device.apply_scanner_selection(SimpleNamespace(area_id="kitchen", rssi_distance=None, rssi=-90))
    assert device.area_advert is None
    assert device.area_name is None
    assert device.area_distance is None
    assert device.area_rssi is None
    assert device.area_last_seen == "Kitchen"  # kept for "last seen in"


def test_a_tracked_device_logs_when_its_room_changes(device, caplog):
    caplog.set_level(logging.DEBUG)
    device.create_sensor = True
    device.name = "Tracked Thing"
    device.area_name = "Kitchen"
    device.apply_scanner_selection(None)  # no receiver wins: the room is cleared
    assert device.area_name is None
    assert "was in 'Kitchen'" in caplog.text


def _advert_for(scanner_address: str, stamp: float | None):
    return SimpleNamespace(scanner_address=scanner_address, stamp=stamp)


def test_get_scanner_returns_the_newest_advert_from_that_receiver(device):
    old = _advert_for("scn1", 10.0)
    new = _advert_for("scn1", 20.0)
    other = _advert_for("scn2", 99.0)
    unstamped = _advert_for("scn1", None)
    device.adverts = {("a", "scn1"): old, ("b", "scn1"): new, ("c", "scn2"): other, ("d", "scn1"): unstamped}
    assert device.get_scanner("scn1") is new
    assert device.get_scanner("scn2") is other
    assert device.get_scanner("missing") is None


def test_calculate_data_updates_adverts_skips_junk_and_sets_zone(device, coordinator, caplog):
    caplog.set_level(logging.ERROR)
    good = MagicMock(spec=BermudaAdvert)
    device.adverts = {("a", "s1"): good, ("a", "s2"): {}}
    device.last_seen = monotonic_time_coarse()
    coordinator.options[CONF_DEVICES] = ["AA:BB:CC:DD:EE:FF"]
    device.calculate_data()
    good.calculate_data.assert_called_once_with()
    assert "not a BermudaDevice instance" in caplog.text
    assert device.zone == STATE_HOME
    assert device.create_sensor is True


def test_a_device_not_heard_within_the_timeout_is_away(device, coordinator):
    coordinator.options[CONF_DEVTRACK_TIMEOUT] = 30
    device.last_seen = monotonic_time_coarse() - 300
    device.calculate_data()
    assert device.zone == STATE_NOT_HOME
    assert device.create_sensor is False


def test_adverts_for_a_dead_metadevice_are_ignored(device):
    scanner = BermudaDevice(address="11:22:33:44:55:66", coordinator=device._coordinator)
    device.metadevice_sources = ["11:22:33:44:55:77"]
    device.process_advertisement(scanner, MagicMock())
    assert device.adverts == {}
    assert device.last_seen == 0


def test_service_uuids_name_the_maker_and_rename_the_device(device, coordinator):
    coordinator.get_manufacturer_from_id.return_value = ("Acme Widgets", False)
    advert = SimpleNamespace(service_uuids=["0000fe9f-0000-1000-8000-00805f9b34fb"], manufacturer_data=[])
    device.process_manufacturer_data(advert)
    coordinator.get_manufacturer_from_id.assert_called_once_with("fe9f")
    assert device.manufacturer == "Acme Widgets"
    assert device.name == "acme_widgets_aa_bb_cc_dd_ee_ff"


def test_a_generic_service_maker_does_not_replace_a_known_one(device, coordinator):
    device.manufacturer = "Real Maker"
    coordinator.get_manufacturer_from_id.return_value = ("Generic Co", True)
    advert = SimpleNamespace(service_uuids=["0000fe9f-0000-1000-8000-00805f9b34fb"], manufacturer_data=[])
    device.process_manufacturer_data(advert)
    assert device.manufacturer == "Real Maker"


def test_to_dict_shows_scanner_by_repr_and_each_advert_by_its_pair(device):
    scanner = _scanner()
    device._hascanner = scanner
    device.adverts = {
        ("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"): SimpleNamespace(
            device_address="aa:bb:cc:dd:ee:ff", scanner_address="11:22:33:44:55:66", to_dict=lambda: {"rssi": -60}
        )
    }
    out = device.to_dict()
    assert out["_hascanner"] == repr(scanner)
    assert out["adverts"] == {"aa:bb:cc:dd:ee:ff__11:22:33:44:55:66": {"rssi": -60}}
    assert "_coordinator" not in out
    assert "options" not in out

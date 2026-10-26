"""
Tests for BermudaDevice class in bermuda_device.py.
"""

import pytest
from unittest.mock import MagicMock, patch
from homeassistant.components.bluetooth import BaseHaScanner, BaseHaRemoteScanner
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.roomwise.autotune_ha import receiver_name
from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.const import ICON_DEFAULT_AREA, ICON_DEFAULT_FLOOR


@pytest.fixture
def mock_coordinator():
    """Fixture for mocking BermudaDataUpdateCoordinator."""
    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass_version_min_2025_4 = True
    return coordinator


@pytest.fixture
def mock_scanner():
    """Fixture for mocking BaseHaScanner."""
    scanner = MagicMock(spec=BaseHaScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def mock_remote_scanner():
    """Fixture for mocking BaseHaRemoteScanner."""
    scanner = MagicMock(spec=BaseHaRemoteScanner)
    scanner.time_since_last_detection.return_value = 5.0
    scanner.source = "mock_source"
    return scanner


@pytest.fixture
def bermuda_device(mock_coordinator):
    """Fixture for creating a BermudaDevice instance."""
    return BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=mock_coordinator)


@pytest.fixture
def bermuda_scanner(mock_coordinator):
    """Fixture for creating a BermudaDevice Scanner instance."""
    return BermudaDevice(address="11:22:33:44:55:66", coordinator=mock_coordinator)


def test_bermuda_device_initialization(bermuda_device):
    """Test BermudaDevice initialization."""
    assert bermuda_device.address == "aa:bb:cc:dd:ee:ff"
    assert bermuda_device.name.startswith("roomwise_")
    assert bermuda_device.area_icon == ICON_DEFAULT_AREA
    assert bermuda_device.floor_icon == ICON_DEFAULT_FLOOR
    assert bermuda_device.zone == "not_home"


def test_async_as_scanner_init(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_init method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    assert bermuda_scanner._hascanner == mock_scanner
    assert bermuda_scanner.is_scanner is True
    assert bermuda_scanner.is_remote_scanner is False


def test_async_as_scanner_update(bermuda_scanner, mock_scanner):
    """Test async_as_scanner_update method."""
    bermuda_scanner.async_as_scanner_update(mock_scanner)
    assert bermuda_scanner.last_seen > 0


def test_async_as_scanner_get_stamp(bermuda_scanner, mock_scanner, mock_remote_scanner):
    """Test async_as_scanner_get_stamp method."""
    bermuda_scanner.async_as_scanner_init(mock_scanner)
    bermuda_scanner.stamps = {"AA:BB:CC:DD:EE:FF": 123.45}

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp is None

    bermuda_scanner.async_as_scanner_init(mock_remote_scanner)

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:bb:CC:DD:EE:FF")
    assert stamp == 123.45

    stamp = bermuda_scanner.async_as_scanner_get_stamp("AA:BB:CC:DD:E1:FF")
    assert stamp is None


def test_make_name(bermuda_device):
    """Test make_name method."""
    bermuda_device.name_by_user = "Custom Name"
    name = bermuda_device.make_name()
    assert name == "Custom Name"
    assert bermuda_device.name == "Custom Name"


def test_process_advertisement(bermuda_device, bermuda_scanner):
    """Test process_advertisement method."""
    advertisement_data = MagicMock()
    bermuda_device.process_advertisement(bermuda_scanner, advertisement_data)
    assert len(bermuda_device.adverts) == 1


# def test_process_manufacturer_data(bermuda_device):
#     """Test process_manufacturer_data method."""
#     mock_advert = MagicMock()
#     mock_advert.service_uuids = ["0000abcd-0000-1000-8000-00805f9b34fb"]
#     mock_advert.manufacturer_data = [{"004C": b"\x02\x15"}]
#     bermuda_device.process_manufacturer_data(mock_advert)
#     assert bermuda_device.manufacturer == "Apple Inc."


def test_to_dict(bermuda_device):
    """Test to_dict method."""
    device_dict = bermuda_device.to_dict()
    assert isinstance(device_dict, dict)
    assert device_dict["address"] == "aa:bb:cc:dd:ee:ff"


def test_repr(bermuda_device):
    """Test __repr__ method."""
    repr_str = repr(bermuda_device)
    assert repr_str == f"{bermuda_device.name} [{bermuda_device.address}]"


@pytest.mark.parametrize(
    ("first_char", "expected"),
    [(c, "bd_addr_random_unresolvable") for c in "0123"]
    + [(c, "bd_addr_random_resolvable") for c in "4567"]
    + [(c, "bd_addr_other") for c in "89ab"]
    + [(c, "bd_addr_random_static") for c in "cdef"],
)
def test_address_type_classifier_uses_the_top_two_bits(mock_coordinator, first_char, expected):
    """Classify a bare MAC by the top two bits of its first octet, by equality.

    The classifier used ``&`` where it meant ``==``: 0b00 (non-resolvable) stayed
    unknown, 0b11 (random static) was labelled resolvable and fed to the IRK
    resolver, and random static was never assigned. 0b10 is reserved for random
    addresses, so an address in that range can only be public.
    """
    address = f"{first_char}a:bb:cc:dd:ee:ff"
    device = BermudaDevice(address=address, coordinator=mock_coordinator)
    assert device.address_type == expected
    if expected == "bd_addr_random_resolvable":
        mock_coordinator.irk_manager.check_mac.assert_called_once_with(address)
    else:
        mock_coordinator.irk_manager.check_mac.assert_not_called()


@pytest.mark.parametrize(
    ("details", "first_char", "expected"),
    [
        # BlueZ (D-Bus) says it outright.
        ({"props": {"AddressType": "public"}}, "4", "bd_addr_other"),
        ({"props": {"AddressType": "public"}}, "c", "bd_addr_other"),
        ({"props": {"AddressType": "public"}}, "0", "bd_addr_other"),
        ({"props": {"AddressType": "random"}}, "4", "bd_addr_random_resolvable"),
        ({"props": {"AddressType": "random"}}, "c", "bd_addr_random_static"),
        ({"props": {"AddressType": "random"}}, "0", "bd_addr_random_unresolvable"),
        # Integer address_type 0 is public in every encoding in use
        # (ESPHome BLE_ADDR_TYPE_PUBLIC, kernel mgmt BDADDR_BREDR).
        ({"source": "proxy", "address_type": 0}, "4", "bd_addr_other"),
        # 1 and 2 mean different things to different backends: no change.
        ({"source": "proxy", "address_type": 1}, "4", "bd_addr_random_resolvable"),
        ({"source": "proxy", "address_type": 2}, "c", "bd_addr_random_static"),
        # No hint at all (mgmt raw adverts, most remote scanners): no change.
        ({"path": "/org/bluez/hci0/dev_x", "props": {"Adapter": "/org/bluez/hci0"}}, "5", "bd_addr_random_resolvable"),
        ({}, "5", "bd_addr_random_resolvable"),
        (None, "5", "bd_addr_random_resolvable"),
    ],
)
def test_public_address_hint_from_bluetooth_stack(mock_coordinator, details, first_char, expected):
    """A public address must not be treated as a rotating private one.

    The address shape alone cannot tell a public address from a random one (a
    public address starting 4-7 looks resolvable), so when the Bluetooth stack
    says an address is public, believe it.
    """
    device = BermudaDevice(address=f"{first_char}a:bb:cc:dd:ee:ff", coordinator=mock_coordinator)
    device.async_apply_address_details(details)
    assert device.address_type == expected


def test_public_address_hint_ignored_for_metadevices(mock_coordinator):
    """Metadevice addresses (iBeacon ids, IRKs) are not MACs: hints never apply."""
    device = BermudaDevice(address="0123456789abcdef0123456789abcdef_1_2", coordinator=mock_coordinator)
    before = device.address_type
    device.async_apply_address_details({"props": {"AddressType": "public"}})
    assert device.address_type == before


# --------------------------------------------------------------------------
# Scanner names and rooms from Home Assistant's device registry
# --------------------------------------------------------------------------

PROXY_BLE = "aa:bb:cc:00:01:12"  # what the Bluetooth stack reports (Wi-Fi address + 2 on an ESP32)
PROXY_WIFI = "aa:bb:cc:00:01:10"


async def _proxy_in_registry(hass, *, router_device: bool):
    """An ESP32 proxy as HA sees it: the Bluetooth device, the ESPHome device, and maybe a router's copy."""
    hall = ar.async_get(hass).async_create("Hall")
    devreg = dr.async_get(hass)
    entries = {}
    for domain in ("bluetooth", "esphome", "router_test"):
        entries[domain] = MockConfigEntry(domain=domain)
        entries[domain].add_to_hass(hass)
    devreg.async_get_or_create(
        config_entry_id=entries["bluetooth"].entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, PROXY_BLE.upper())},
        name=f"Proxy 000110 ({PROXY_BLE.upper()})",
    )
    esphome = devreg.async_get_or_create(
        config_entry_id=entries["esphome"].entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, PROXY_WIFI)},
        name="Proxy 000110",
    )
    devreg.async_update_device(esphome.id, name_by_user="Hall proxy", area_id=hall.id)
    if router_device:
        # A router or network-scanner integration lists the proxy's Wi-Fi address too,
        # named after the address and without a room. Registered last, so it is the last match.
        devreg.async_get_or_create(
            config_entry_id=entries["router_test"].entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, PROXY_WIFI)},
            name=PROXY_WIFI,
        )
    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass = hass
    coordinator.dr = devreg
    ha_scanner = MagicMock(spec=BaseHaScanner)
    ha_scanner.time_since_last_detection.return_value = 5.0
    ha_scanner.source = PROXY_BLE.upper()
    ha_scanner.name = f"esp32-proxy-000110 ({PROXY_BLE.upper()})"
    scanner = BermudaDevice(address=PROXY_BLE, coordinator=coordinator)
    return scanner, ha_scanner, hall


@pytest.mark.parametrize("router_device", [False, True])
async def test_scanner_takes_the_name_and_room_the_user_gave_it(hass, router_device):
    """Another integration's copy of the proxy (named after its address) must not hide the user's name."""
    scanner, ha_scanner, hall = await _proxy_in_registry(hass, router_device=router_device)
    scanner.async_as_scanner_init(ha_scanner)
    assert scanner.name == "Hall proxy"
    assert scanner.area_id == hall.id
    assert scanner.address_wifi_mac == PROXY_WIFI
    assert receiver_name(scanner, PROXY_BLE) == "Hall proxy"


async def test_scanner_lookup_uses_no_deprecated_registry_calls(hass, ha_deprecation_reports):
    """`device_registry.devices.get_entries(...)` is deprecated and stops working in HA 2027.9."""
    scanner, ha_scanner, _hall = await _proxy_in_registry(hass, router_device=True)
    scanner.async_as_scanner_init(ha_scanner)
    assert scanner.name == "Hall proxy"
    assert ha_deprecation_reports() == []


async def test_floor_level_follows_the_room(hass):
    """The floor_level attribute comes from the room's floor (it was never filled in)."""
    from homeassistant.helpers import floor_registry as fr

    coordinator = MagicMock()
    coordinator.options = {}
    coordinator.hass = hass
    upstairs = fr.async_get(hass).async_create("Upstairs", level=1)
    bedroom = ar.async_get(hass).async_create("Bedroom")
    ar.async_get(hass).async_update(bedroom.id, floor_id=upstairs.floor_id)
    hall = ar.async_get(hass).async_create("Hall")  # no floor
    device = BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=coordinator)

    device._update_area_and_floor(bedroom.id)
    assert device.floor_level == 1
    device._update_area_and_floor(hall.id)
    assert device.floor_level is None
    device._update_area_and_floor(bedroom.id)
    device._update_area_and_floor("no_such_area")
    assert device.floor_level is None

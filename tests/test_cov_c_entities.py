"""Behaviour of the entity base classes, sensors, number and device tracker."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

import custom_components.roomwise.device_tracker as tracker
import custom_components.roomwise.entity as entity_mod
import custom_components.roomwise.number as number
import custom_components.roomwise.sensor as sensor
from custom_components.roomwise.const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    SCANNER_NETWORK_MAC_WAIT,
    SIGNAL_DEVICE_NEW,
    SIGNAL_SCANNERS_CHANGED,
)

ADDR = "aa:bb:cc:dd:ee:01"
SCANNER = "aa:bb:cc:dd:ee:02"


def _device(**over):
    base = {
        "name": "Test Phone",
        "address": ADDR,
        "address_type": "bd_addr_other",
        "unique_id": ADDR,
        "is_scanner": False,
        "address_wifi_mac": None,
        "address_ble_mac": None,
        "ref_power_changed": 0,
        "ref_power": -59,
        "area_name": "Living Room",
        "area_id": "living_room",
        "area_icon": "mdi:sofa",
        "area_last_seen": "Hall",
        "area_last_seen_icon": "mdi:history",
        "floor_id": "ground",
        "floor_name": "Ground",
        "floor_level": 0,
        "floor_icon": "mdi:floor-plan",
        "adverts": {},
        "area_advert": None,
        "area_rssi": -70,
        "area_distance": None,
        "diag_area_switch": None,
        "zone": None,
        "name_by_user": None,
        "get_scanner": lambda _addr: None,
        "set_ref_power": MagicMock(),
    }
    base.update(over)
    return SimpleNamespace(**base)


def _make(cls, hass, device=None, *extra, devices=None, **coord):
    device = device or _device()
    coordinator = SimpleNamespace(
        devices=devices or {device.address: device},
        hass=hass,
        scanner_list=[SCANNER],
        async_add_listener=lambda *_a, **_k: lambda: None,
        count_active_scanners=lambda: 3,
        count_active_devices=lambda: 5,
        **coord,
    )
    entry = SimpleNamespace(options={})
    ent = cls(coordinator, entry, device.address, *extra) if extra else cls(coordinator, entry, device.address)
    ent.hass = hass
    return ent


# ---------------------------------------------------------------- entity.py


async def test_cached_ratelimit_serves_cache_until_it_matters(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensor, hass)
    now = [1000.0]
    with patch.object(entity_mod, "monotonic_time_coarse", side_effect=lambda: now[0]):
        assert ent._cached_ratelimit(10, interval=30) == 10  # first value always published
        assert ent.bermuda_update_interval == 30
        now[0] = 1005.0
        assert ent._cached_ratelimit(12, fast_falling=False) == 10  # cached, not stale
        assert ent._cached_ratelimit(9, fast_falling=True) == 9  # falling is published at once
        assert ent._cached_ratelimit(11, fast_falling=False, fast_rising=True) == 11  # rising too
        assert ent._cached_ratelimit(None) is None  # None is always published
        assert ent._cached_ratelimit(4, fast_falling=False) == 4  # last state None: publish
        now[0] = 1100.0
        assert ent._cached_ratelimit(50, fast_falling=False) == 50  # stale cache refreshed
        ent._device.ref_power_changed = 1099.0
        now[0] = 1100.5
        assert ent._cached_ratelimit(51, fast_falling=False) == 51  # ref power just changed


async def test_coordinator_update_syncs_user_name_and_device_registry(hass: HomeAssistant):
    device = _device()
    ent = _make(sensor.BermudaSensor, hass, device)
    ent.dr = MagicMock()
    registry_entry = SimpleNamespace(id="reg-1", name_by_user="My Phone")
    with (
        patch.object(sensor.BermudaSensor, "device_entry", registry_entry),
        patch.object(sensor.BermudaSensor, "async_write_ha_state") as write,
    ):
        ent._handle_coordinator_update()
        assert device.name_by_user == "My Phone"
        assert ent.devreg_init_done is True
        ent.dr.async_update_device.assert_not_called()
        assert write.call_count == 1

        device.name = "Renamed Phone"
        ent._handle_coordinator_update()
        ent.dr.async_update_device.assert_called_once_with("reg-1", name="Renamed Phone")
        assert ent._lastname == "Renamed Phone"


async def test_coordinator_update_renames_without_registry_entry(hass: HomeAssistant):
    device = _device()
    ent = _make(sensor.BermudaSensor, hass, device)
    ent.dr = MagicMock()
    with (
        patch.object(sensor.BermudaSensor, "device_entry", None),
        patch.object(sensor.BermudaSensor, "async_write_ha_state"),
    ):
        device.name = "Other Name"
        ent._handle_coordinator_update()
    ent.dr.async_update_device.assert_not_called()
    assert ent._lastname == "Other Name"


async def test_unreadable_state_is_written_every_time(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensor, hass)
    ent.devreg_init_done = True
    with (
        patch.object(sensor.BermudaSensor, "device_entry", None),
        patch.object(sensor.BermudaSensor, "async_write_ha_state") as write,
        patch.object(sensor.BermudaSensor, "extra_state_attributes", property(lambda _s: 1 / 0)),
    ):
        assert ent._state_fingerprint() is None
        ent._handle_coordinator_update()
        ent._handle_coordinator_update()
    assert write.call_count == 2


async def test_device_info_for_a_plain_device(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensor, hass)
    assert ent.unique_id == ADDR
    assert ent.device_info == {
        "identifiers": {(DOMAIN, ADDR)},
        "connections": {(dr.CONNECTION_BLUETOOTH, ADDR.upper())},
        "name": "Test Phone",
    }


async def test_device_info_for_a_scanner_uses_both_macs(hass: HomeAssistant):
    device = _device(is_scanner=True, address_wifi_mac="AA:BB:CC:DD:EE:99", address_ble_mac=ADDR)
    info = _make(sensor.BermudaSensor, hass, device).device_info
    assert info["connections"] == {
        (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:dd:ee:99"),
        (dr.CONNECTION_BLUETOOTH, ADDR.upper()),
    }
    # With no separate macs it falls back to the one address for both.
    bare = _make(sensor.BermudaSensor, hass, _device(is_scanner=True)).device_info
    assert bare["connections"] == {(dr.CONNECTION_NETWORK_MAC, ADDR), (dr.CONNECTION_BLUETOOTH, ADDR.upper())}


async def test_device_info_for_ibeacon_sets_a_model(hass: HomeAssistant):
    info = _make(sensor.BermudaSensor, hass, _device(address_type=ADDR_TYPE_IBEACON)).device_info
    assert info["connections"] == {("ibeacon", ADDR)}
    assert info["model"] == f"iBeacon: {ADDR}"
    assert info["identifiers"] == {(DOMAIN, ADDR)}


async def test_device_info_for_private_ble_joins_that_integrations_device(hass: HomeAssistant):
    info = _make(sensor.BermudaSensor, hass, _device(address_type=ADDR_TYPE_PRIVATE_BLE_DEVICE)).device_info
    assert info["connections"] == {("private_ble_device", ADDR)}
    assert info["identifiers"] == {(DOMAIN_PRIVATE_BLE_DEVICE, ADDR)}
    assert "model" not in info


async def test_global_entity_basics(hass: HomeAssistant):
    coordinator = SimpleNamespace(hass=hass, scanner_list=[SCANNER, "x"])
    ent = sensor.BermudaTotalProxyCount(coordinator, SimpleNamespace())
    ent.hass = hass
    assert ent.device_info == {"identifiers": {(DOMAIN, "ROOMWISE_GLOBAL")}, "name": "Roomwise Global"}
    with patch.object(sensor.BermudaTotalProxyCount, "async_write_ha_state") as write:
        ent._handle_coordinator_update()
    write.assert_called_once()

    now = [1000.0]
    with patch.object(entity_mod, "monotonic_time_coarse", side_effect=lambda: now[0]):
        assert ent._cached_ratelimit("a", interval=10) == "a"
        now[0] = 1005.0
        assert ent._cached_ratelimit("b") == "a"  # within the interval
        now[0] = 1011.0
        assert ent._cached_ratelimit("b") == "b"


# ---------------------------------------------------------------- sensor.py


def test_scanner_ready_rules():
    now = 10_000.0
    ready = sensor._scanner_ready
    with patch.object(sensor, "monotonic_time_coarse", return_value=now):
        assert ready(SimpleNamespace(is_remote_scanner=None, address_wifi_mac=None, first_seen=now)) is False
        assert ready(SimpleNamespace(is_remote_scanner=False, address_wifi_mac=None, first_seen=now)) is True
        assert ready(SimpleNamespace(is_remote_scanner=True, address_wifi_mac="x", first_seen=now)) is True
        young = SimpleNamespace(is_remote_scanner=True, address_wifi_mac=None, first_seen=now)
        assert ready(young) is False
        old = SimpleNamespace(
            is_remote_scanner=True, address_wifi_mac=None, first_seen=now - SCANNER_NETWORK_MAC_WAIT - 1
        )
        assert ready(old) is True


async def test_area_sensor_icon_enabled_class_and_attributes(hass: HomeAssistant):
    area = _make(sensor.BermudaSensor, hass)
    assert area.name == "Area"
    assert area.has_entity_name is True
    assert area.native_value == "Living Room"
    assert area.icon == "mdi:sofa"
    assert area.entity_registry_enabled_default is True
    assert area.device_class is None
    assert area.extra_state_attributes == {
        "area_id": "living_room",
        "area_name": "Living Room",
        "floor_id": "ground",
        "floor_name": "Ground",
        "floor_level": 0,
        "current_mac": ADDR,
    }


async def test_floor_and_last_seen_sensors(hass: HomeAssistant):
    floor = _make(sensor.BermudaSensorFloor, hass)
    assert (floor.name, floor.unique_id, floor.native_value) == ("Floor", f"{ADDR}_floor", "Ground")
    assert floor.icon == "mdi:floor-plan"
    assert floor.entity_registry_enabled_default is True
    assert "floor_id" in floor.extra_state_attributes

    last = _make(sensor.BermudaSensorAreaLastSeen, hass)
    assert (last.name, last.unique_id, last.native_value) == ("Area Last Seen", f"{ADDR}_area_last_seen", "Hall")
    assert last.icon == "mdi:history"
    assert last.entity_registry_enabled_default is False
    assert "area_id" not in last.extra_state_attributes


async def test_other_sensors_use_the_default_icon(hass: HomeAssistant):
    rssi = _make(sensor.BermudaSensorRssi, hass)
    assert rssi.icon is None


async def test_metadevice_current_mac_follows_the_newest_advert(hass: HomeAssistant):
    adverts = {
        "old": SimpleNamespace(stamp=5, device_address="11:11:11:11:11:11"),
        "new": SimpleNamespace(stamp=9, device_address="22:22:22:22:22:22"),
    }
    ibeacon = _make(sensor.BermudaSensor, hass, _device(address_type=ADDR_TYPE_IBEACON, adverts=adverts))
    assert ibeacon.extra_state_attributes["current_mac"] == "22:22:22:22:22:22"
    private = _make(sensor.BermudaSensor, hass, _device(address_type=ADDR_TYPE_PRIVATE_BLE_DEVICE, adverts={}))
    assert private.extra_state_attributes["current_mac"] == STATE_UNAVAILABLE


async def test_nearest_scanner_sensor(hass: HomeAssistant):
    scanner_dev = _device(address=SCANNER, unique_id=SCANNER, name="Hall Proxy")
    device = _device(area_advert=SimpleNamespace(scanner_address=SCANNER))
    ent = _make(sensor.BermudaSensorScanner, hass, device, devices={ADDR: device, SCANNER: scanner_dev})
    assert (ent.name, ent.unique_id) == ("Nearest Scanner", f"{ADDR}_scanner")
    assert ent.native_value == "Hall Proxy"
    assert _make(sensor.BermudaSensorScanner, hass).native_value is None


async def test_rssi_sensor(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensorRssi, hass)
    assert (ent.name, ent.unique_id) == ("Nearest RSSI", f"{ADDR}_rssi")
    assert ent.native_value == -70
    assert ent.device_class == SensorDeviceClass.SIGNAL_STRENGTH
    assert ent.native_unit_of_measurement == "dBm"
    assert ent.state_class == SensorStateClass.MEASUREMENT


async def test_range_sensor(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensorRange, hass, _device(area_distance=1.26))
    assert (ent.name, ent.unique_id) == ("Distance", f"{ADDR}_range")
    assert ent.native_value == 1.3
    assert ent.device_class == SensorDeviceClass.DISTANCE
    assert ent.native_unit_of_measurement == "m"
    assert ent.state_class == SensorStateClass.MEASUREMENT
    assert _make(sensor.BermudaSensorRange, hass).native_value is None  # no distance known


async def test_per_scanner_range_sensors(hass: HomeAssistant):
    scanner_dev = _device(
        address=SCANNER,
        unique_id=SCANNER,
        name="Hall Proxy",
        address_wifi_mac="aa:bb:cc:dd:ee:77",
        area_id="hall",
        area_name="Hall",
    )
    heard = SimpleNamespace(rssi_distance=2.34567, rssi_distance_raw=2.98765, source="x")
    device = _device(get_scanner=lambda a: heard if a == SCANNER else None)
    devs = {ADDR: device, SCANNER: scanner_dev}

    rng = _make(sensor.BermudaSensorScannerRange, hass, device, SCANNER, devices=devs)
    assert rng.unique_id == f"{ADDR}_aa:bb:cc:dd:ee:77_range"
    assert rng.name == "Distance to Hall Proxy"
    assert rng.native_value == 2.346
    assert rng.extra_state_attributes == {
        "area_id": "hall",
        "area_name": "Hall",
        "area_scanner_mac": SCANNER,
        "area_scanner_name": "Hall Proxy",
    }

    raw = _make(sensor.BermudaSensorScannerRangeRaw, hass, device, SCANNER, devices=devs)
    assert raw.unique_id == f"{ADDR}_aa:bb:cc:dd:ee:77_range_raw"
    assert raw.name == "Unfiltered Distance to Hall Proxy"
    assert raw.native_value == 2.988


async def test_per_scanner_range_sensors_when_scanner_has_not_heard_us(hass: HomeAssistant):
    scanner_dev = _device(address=SCANNER, unique_id=SCANNER, name="Hall Proxy")
    device = _device()  # get_scanner returns None
    devs = {ADDR: device, SCANNER: scanner_dev}
    rng = _make(sensor.BermudaSensorScannerRange, hass, device, SCANNER, devices=devs)
    raw = _make(sensor.BermudaSensorScannerRangeRaw, hass, device, SCANNER, devices=devs)
    assert rng.unique_id == f"{ADDR}_{SCANNER}_range"  # falls back to the Bluetooth address
    assert rng.native_value is None
    assert rng.extra_state_attributes is None
    assert raw.native_value is None

    no_distance = SimpleNamespace(rssi_distance=None, rssi_distance_raw=None, source="x")
    device2 = _device(get_scanner=lambda _a: no_distance)
    rng2 = _make(
        sensor.BermudaSensorScannerRange, hass, device2, SCANNER, devices={ADDR: device2, SCANNER: scanner_dev}
    )
    assert rng2.native_value is None


async def test_area_switch_diagnostic_sensor(hass: HomeAssistant):
    ent = _make(sensor.BermudaSensorAreaSwitchReason, hass)
    assert ent.name == "Area Switch Diagnostic"
    assert ent.unique_id == f"{ADDR}_area_switch_reason"
    assert ent.entity_registry_enabled_default is False
    assert ent._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert ent.native_value is None
    long = _make(sensor.BermudaSensorAreaSwitchReason, hass, _device(diag_area_switch="x" * 400))
    assert long.native_value == "x" * 255


async def test_global_sensors(hass: HomeAssistant):
    coordinator = SimpleNamespace(
        hass=hass,
        scanner_list=["a", "b"],
        devices={"a": 1, "b": 2, "c": 3},
        count_active_scanners=lambda: 1,
        count_active_devices=lambda: 2,
    )
    expected = {
        sensor.BermudaTotalProxyCount: ("ROOMWISE_GLOBAL_PROXY_COUNT", "Total proxy count", 2),
        sensor.BermudaActiveProxyCount: ("ROOMWISE_GLOBAL_ACTIVE_PROXY_COUNT", "Active proxy count", 1),
        sensor.BermudaTotalDeviceCount: ("ROOMWISE_GLOBAL_DEVICE_COUNT", "Total device count", 3),
        sensor.BermudaVisibleDeviceCount: ("ROOMWISE_GLOBAL_VISIBLE_DEVICE_COUNT", "Visible device count", 2),
    }
    for cls, (uid, name, value) in expected.items():
        ent = cls(coordinator, SimpleNamespace())
        assert (ent.unique_id, ent.name, ent.native_value) == (uid, name, value)
        assert ent._attr_entity_category == EntityCategory.DIAGNOSTIC
    # The shared base keeps a generic name.
    assert sensor.BermudaGlobalSensor(coordinator, SimpleNamespace()).name == "Area"


async def test_last_seen_sensor_restores_its_value(hass: HomeAssistant):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.test_phone_area_last_seen", "Kitchen"),
                {"native_value": "Kitchen", "native_unit_of_measurement": None},
            )
        ],
    )
    device = _device(area_last_seen=None)
    ent = _make(sensor.BermudaSensorAreaLastSeen, hass, device)
    ent.entity_id = "sensor.test_phone_area_last_seen"
    await ent.async_added_to_hass()
    assert device.area_last_seen == "Kitchen"
    assert ent.native_value == "Kitchen"


async def test_last_seen_sensor_keeps_unknown_when_nothing_stored(hass: HomeAssistant):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.test_phone_area_last_seen", "unknown"),
                {"native_value": None, "native_unit_of_measurement": None},
            )
        ],
    )
    device = _device(area_last_seen=None)
    ent = _make(sensor.BermudaSensorAreaLastSeen, hass, device)
    ent.entity_id = "sensor.test_phone_area_last_seen"
    await ent.async_added_to_hass()
    assert device.area_last_seen is None


async def test_sensor_platform_creates_each_device_once_and_adds_floors_later(hass: HomeAssistant):
    listeners = []
    coordinator = SimpleNamespace(
        get_scanners=[],
        scanner_list=[],
        have_floors=True,
        sensor_created=MagicMock(),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []

    def made(kind):
        return lambda _c, _e, address=None, scanner=None: (kind, address, scanner)

    with ExitStack() as stack:
        for name in dir(sensor):
            if name.startswith("Bermuda") and isinstance(getattr(sensor, name), type):
                stack.enter_context(patch.object(sensor, name, made(name)))
        await sensor.async_setup_entry(hass, entry, lambda entities, *_: added.extend(entities))
        async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, ADDR)
        async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, ADDR)  # duplicate request
        async_dispatcher_send(hass, SIGNAL_SCANNERS_CHANGED)
        await hass.async_block_till_done()

        kinds = [item[0] for item in added if item[1] == ADDR]
        assert sorted(kinds) == sorted(
            [
                "BermudaSensor",
                "BermudaSensorFloor",
                "BermudaSensorRange",
                "BermudaSensorScanner",
                "BermudaSensorRssi",
                "BermudaSensorAreaLastSeen",
                "BermudaSensorAreaSwitchReason",
            ]
        )
        assert coordinator.sensor_created.call_count == 2  # told both times, built once

        # A device set up before floors existed gets its Floor sensor later.
        created_floors_missing = len([k for k in kinds if k == "BermudaSensorFloor"])
        assert created_floors_missing == 1


async def test_floor_sensor_is_added_to_devices_that_predate_floors(hass: HomeAssistant):
    listeners = []
    coordinator = SimpleNamespace(
        get_scanners=[],
        scanner_list=[],
        have_floors=False,
        sensor_created=MagicMock(),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []

    def made(kind):
        return lambda _c, _e, address=None, scanner=None: (kind, address, scanner)

    with ExitStack() as stack:
        for name in dir(sensor):
            if name.startswith("Bermuda") and isinstance(getattr(sensor, name), type):
                stack.enter_context(patch.object(sensor, name, made(name)))
        await sensor.async_setup_entry(hass, entry, lambda entities, *_: added.extend(entities))
        async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, ADDR)
        await hass.async_block_till_done()
        assert ("BermudaSensorFloor", ADDR, None) not in added
        coordinator.have_floors = True
        for cb in listeners:
            cb()
        assert added.count(("BermudaSensorFloor", ADDR, None)) == 1
        for cb in listeners:
            cb()  # nothing more to add
        assert added.count(("BermudaSensorFloor", ADDR, None)) == 1


# ---------------------------------------------------------------- number.py


async def test_number_value_set_and_unique_id(hass: HomeAssistant):
    device = _device()
    ent = _make(number.BermudaNumber, hass, device)
    assert ent.unique_id == f"{ADDR}_ref_power"
    assert ent.native_value == -59
    with patch.object(number.BermudaNumber, "async_write_ha_state") as write:
        await ent.async_set_native_value(-65)
    device.set_ref_power.assert_called_once_with(-65)
    write.assert_called_once()


async def test_number_restores_previous_calibration(hass: HomeAssistant):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.test_phone_ref_power", "-61"),
                {
                    "native_value": -61,
                    "native_min_value": -127,
                    "native_max_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "dBm",
                },
            )
        ],
    )
    device = _device()
    ent = _make(number.BermudaNumber, hass, device)
    ent.entity_id = "number.test_phone_ref_power"
    await ent.async_added_to_hass()
    device.set_ref_power.assert_called_once_with(-61)


async def test_number_without_stored_value_leaves_calibration_alone(hass: HomeAssistant):
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.test_phone_ref_power", "unknown"),
                {
                    "native_value": None,
                    "native_min_value": -127,
                    "native_max_value": 0,
                    "native_step": 1,
                    "native_unit_of_measurement": "dBm",
                },
            )
        ],
    )
    device = _device()
    ent = _make(number.BermudaNumber, hass, device)
    ent.entity_id = "number.test_phone_ref_power"
    await ent.async_added_to_hass()
    device.set_ref_power.assert_not_called()


@pytest.mark.parametrize("module,created", [(number, "number_created"), (tracker, "device_tracker_created")])
async def test_platform_creates_one_entity_per_device(hass: HomeAssistant, module, created):
    coordinator = SimpleNamespace(
        hass=hass,
        devices={ADDR: _device()},
        number_created=MagicMock(),
        device_tracker_created=MagicMock(),
    )
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=coordinator), options={})
    batches = []
    await module.async_setup_entry(hass, entry, lambda entities, update: batches.append((list(entities), update)))
    async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, ADDR)
    async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, ADDR)
    await hass.async_block_till_done()

    assert len(batches) == 1  # second request ignored
    entities, update_before_add = batches[0]
    assert len(entities) == 1
    assert entities[0].address == ADDR
    assert update_before_add is False
    assert getattr(coordinator, created).call_count == 2  # coordinator told each time
    entry.async_on_unload.assert_called_once()


# ---------------------------------------------------------- device_tracker.py


async def test_device_tracker_state_and_attributes(hass: HomeAssistant):
    ent = _make(tracker.BermudaDeviceTracker, hass)
    assert ent.unique_id == ADDR
    assert ent.is_connected is None  # zone unknown

    ent._device.zone = STATE_HOME
    assert ent.is_connected is True
    assert ent.icon == "mdi:bluetooth-connect"

    ent._device.zone = STATE_NOT_HOME
    assert ent.is_connected is False
    assert ent.icon == "mdi:bluetooth-off"

    ent._device.zone = "zone.work"
    assert ent.is_connected is True  # anything but not_home counts as connected

    assert ent.extra_state_attributes == {"scanner": None, "area": "Living Room"}
    ent._device.area_advert = SimpleNamespace(name="Hall Proxy")
    assert ent.extra_state_attributes == {"scanner": "Hall Proxy", "area": "Living Room"}


async def test_base_entity_unique_id_is_the_device_id(hass: HomeAssistant):
    ent = _make(entity_mod.BermudaEntity, hass)
    assert ent.unique_id == ADDR

"""Coordinator behaviour: registry events, update cycle, gathering, pruning, metadevices, areas, redaction."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.roomwise.coordinator as coordinator_module
import custom_components.roomwise.coordinator_prune as prune_module
from custom_components.roomwise.const import (
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    AREA_MAX_AD_AGE,
    BDADDR_TYPE_RANDOM_RESOLVABLE,
    CONF_DEVICES,
    DOMAIN_PRIVATE_BLE_DEVICE,
    METADEVICE_IBEACON_DEVICE,
    METADEVICE_TYPE_IBEACON_SOURCE,
    METADEVICE_TYPE_PRIVATE_BLE_SOURCE,
    PRUNE_TIME_DEFAULT,
    SIGNAL_DEVICE_NEW,
)
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

Coordinator = BermudaDataUpdateCoordinator

MAC_A = "aa:bb:cc:dd:ee:01"
MAC_B = "aa:bb:cc:dd:ee:02"
IRK = "0123456789abcdef0123456789abcdef"
BEACON_UUID = "e2c56db5dffb48d2b060d0f5a71096e0"


@pytest.fixture
def coordinator(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    """The real coordinator of a loaded entry, with the manufacturer-table load considered done."""
    coord = setup_bermuda_entry.runtime_data.coordinator
    coord._waitingfor_load_manufacturer_ids = False
    coord._do_private_device_init = False
    return coord


# ---------------------------------------------------------------- manufacturer names


def _name_tables():
    return SimpleNamespace(
        member_uuids={0x1234: "Google LLC", 0x5678: "Acme Sensors"},
        company_uuids={0x9ABC: "Beta Company"},
    )


@pytest.mark.parametrize(
    ("uuid", "expected"),
    [
        (0x0BA9, ("Shelly Devices", False)),
        ("0BA9", ("Shelly Devices", False)),
        (0x004C, ("Apple Inc.", True)),
        ("00:4c", ("Apple Inc.", True)),
        (0x181C, ("BTHome v1 cleartext", True)),
        (0x181E, ("BTHome v1 encrypted", True)),
        ("FC:D2", ("BTHome V2", True)),
        (0x1234, ("Google LLC", True)),  # member name that is a hardware OEM: generic
        (0x5678, ("Acme Sensors", False)),
        (0x9ABC, ("Beta Company", False)),
        (0x7777, (None, None)),
    ],
)
def test_manufacturer_name_lookup(uuid, expected):
    assert Coordinator.get_manufacturer_from_id(_name_tables(), uuid) == expected


# ---------------------------------------------------------------- device registry events


def _devreg_coordinator(**kwargs):
    fields = {
        "devices": {},
        "dr": SimpleNamespace(async_get=lambda device_id: None),
        "get_scanners": [],
        "_scanners_departed": {},
        "_scanner_init_pending": False,
        "_do_private_device_init": False,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _event(action, device_id="dev-1"):
    data = {"action": action, "device_id": device_id}
    if action == "update":
        data["changes"] = {}
    return SimpleNamespace(data=data)


def test_devreg_create_without_device_id_is_ignored(caplog):
    coord = _devreg_coordinator()
    with caplog.at_level(logging.ERROR):
        Coordinator.handle_devreg_changes(coord, _event("create", device_id=None))
    assert "without a device_id" in caplog.text
    assert coord._scanner_init_pending is False
    assert coord._do_private_device_init is False


def test_devreg_private_ble_device_triggers_private_device_rescan():
    entry = SimpleNamespace(connections={("private_ble_device", "x")}, identifiers=set(), name_by_user=None)
    coord = _devreg_coordinator(dr=SimpleNamespace(async_get=lambda device_id: entry))
    Coordinator.handle_devreg_changes(coord, _event("create"))
    assert coord._do_private_device_init is True
    assert coord._scanner_init_pending is False


def test_devreg_ibeacon_device_needs_nothing():
    entry = SimpleNamespace(connections={("ibeacon", "x")}, identifiers=set(), name_by_user=None)
    coord = _devreg_coordinator(dr=SimpleNamespace(async_get=lambda device_id: entry))
    Coordinator.handle_devreg_changes(coord, _event("update"))
    assert coord._do_private_device_init is False
    assert coord._scanner_init_pending is False


def test_devreg_rename_of_our_device_copies_the_users_name():
    tracked = MagicMock()
    entry = SimpleNamespace(
        connections={("mac", MAC_A)},
        identifiers={("roomwise", MAC_A.upper())},
        name_by_user="Hallway Tag",
    )
    coord = _devreg_coordinator(dr=SimpleNamespace(async_get=lambda device_id: entry), devices={MAC_A: tracked})
    Coordinator.handle_devreg_changes(coord, _event("update"))
    assert tracked.name_by_user == "Hallway Tag"
    tracked.make_name.assert_called_once_with()
    assert coord._scanner_init_pending is True


def test_devreg_unknown_device_id_is_logged(caplog):
    coord = _devreg_coordinator()
    with caplog.at_level(logging.ERROR):
        Coordinator.handle_devreg_changes(coord, _event("create"))
    assert "does not exist" in caplog.text


def test_devreg_remove_of_a_scanner_triggers_scanner_rescan():
    scanner = SimpleNamespace(entry_id="dev-1", name="Living Room Proxy")
    other = SimpleNamespace(entry_id="dev-9", name="Kitchen Proxy")
    coord = _devreg_coordinator(get_scanners=[scanner, other])
    Coordinator.handle_devreg_changes(coord, _event("remove"))
    assert coord._scanner_init_pending is True
    assert coord._do_private_device_init is False


def test_devreg_remove_of_other_device_opportunistically_rechecks_private_ble():
    coord = _devreg_coordinator(get_scanners=[SimpleNamespace(entry_id="dev-9", name="Kitchen Proxy")])
    Coordinator.handle_devreg_changes(coord, _event("remove"))
    assert coord._do_private_device_init is True
    assert coord._scanner_init_pending is False


# ---------------------------------------------------------------- platform creation reports


def test_platform_reports_set_flags_and_all_done_after_the_last(coordinator):
    device = coordinator._get_or_create_device(MAC_A)
    assert not device.create_all_done

    coordinator.sensor_created(MAC_A)
    coordinator.device_tracker_created(MAC_A)
    assert device.create_sensor_done
    assert device.create_tracker_done
    assert not device.create_all_done

    coordinator.number_created(MAC_A)
    assert device.create_number_done
    assert device.create_all_done


def test_platform_reports_for_untracked_device_warn_and_change_nothing(coordinator, caplog):
    with caplog.at_level(logging.WARNING):
        coordinator.sensor_created(MAC_B)
        coordinator.device_tracker_created(MAC_B)
    coordinator.number_created(MAC_B)
    assert caplog.text.count("Very odd") == 2
    assert MAC_B not in coordinator.devices


def test_get_device_returns_none_for_unknown_address(coordinator):
    assert coordinator._get_device(MAC_B) is None
    device = coordinator._get_or_create_device(MAC_B)
    assert coordinator._get_device(MAC_B.upper()) is device


# ---------------------------------------------------------------- update cycle


def test_update_skips_while_previous_cycle_is_still_running(coordinator):
    coordinator.update_in_progress = True
    assert coordinator._async_update_data_internal() is False
    # The cycle that is running owns the flag; the skipped one must not clear it.
    assert coordinator.update_in_progress is True


async def test_update_seeds_configured_devices_and_announces_unfinished_ones(hass: HomeAssistant, coordinator):
    coordinator.options[CONF_DEVICES] = [MAC_A]
    announced = []

    @callback
    def _heard(address):
        announced.append(address)

    async_dispatcher_connect(hass, SIGNAL_DEVICE_NEW, _heard)

    assert coordinator._async_update_data_internal() is True
    assert MAC_A in coordinator.devices
    assert announced == []  # no entities wanted for it yet

    device = coordinator.devices[MAC_A]
    device.create_sensor = True
    coordinator._async_update_data_internal()
    await hass.async_block_till_done()
    assert announced == [MAC_A]

    device.create_all_done = True
    coordinator._async_update_data_internal()
    await hass.async_block_till_done()
    assert announced == [MAC_A]  # finished devices are not announced again
    assert coordinator.update_in_progress is False


# ---------------------------------------------------------------- gathering adverts


def _ha_scanner(source, adverts):
    return SimpleNamespace(source=source, discovered_devices_and_advertisement_data=adverts)


def _gather_coordinator(ha_scanners, get_device, **kwargs):
    fields = {
        "_scanner_init_pending": False,
        "_hascanners": ha_scanners,
        "_get_device": get_device,
        "_get_or_create_device": MagicMock(),
        "_refresh_scanners": MagicMock(),
        "stamp_last_update_started": 1000,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _bledevice(address, advert_rssi=-60):
    return SimpleNamespace(address=address, details={}), SimpleNamespace(rssi=advert_rssi)


def test_gather_refreshes_scanners_when_one_is_unknown_then_uses_it():
    scanner_device = MagicMock()
    scanner_device.async_as_scanner_get_stamp.return_value = None
    known = []

    def get_device(address):
        return scanner_device if known else None

    coord = _gather_coordinator([_ha_scanner("proxy-1", {})], get_device)
    coord._refresh_scanners.side_effect = lambda force: known.append(force)

    assert Coordinator._async_gather_advert_data(coord) is True

    coord._refresh_scanners.assert_called_once_with(force=True)
    scanner_device.async_as_scanner_update.assert_called_once()


def test_gather_reports_scanner_that_cannot_be_found_and_moves_on(caplog):
    caplog.set_level(logging.ERROR)
    good = MagicMock()
    good.async_as_scanner_get_stamp.return_value = None
    ble, advert = _bledevice("4a:bb:cc:dd:ee:ff")
    coord = _gather_coordinator(
        [_ha_scanner("ghost-proxy", {"x": (ble, advert)}), _ha_scanner("proxy-1", {"x": (ble, advert)})],
        lambda address: good if address == "proxy-1" else None,
    )

    Coordinator._async_gather_advert_data(coord)

    assert "Failed to find config for scanner ghost-proxy" in caplog.text
    # Only the findable scanner's advert was processed.
    assert coord._get_or_create_device.call_count == 1
    good.async_as_scanner_update.assert_called_once()


def test_gather_skips_old_adverts_and_bogus_bluez_adverts():
    scanner_device = MagicMock()
    scanner_device.async_as_scanner_get_stamp.side_effect = lambda address: {
        "old": 900.0,  # before stamp_last_update_started - 3: already processed
        "fresh": 1001.0,
    }.get(address)
    old_ble, old_advert = _bledevice("old")
    bogus_ble, bogus_advert = _bledevice("bogus", advert_rssi=-127)
    fresh_ble, fresh_advert = _bledevice("fresh")
    coord = _gather_coordinator(
        [
            _ha_scanner(
                "proxy-1",
                {"o": (old_ble, old_advert), "b": (bogus_ble, bogus_advert), "f": (fresh_ble, fresh_advert)},
            )
        ],
        lambda address: scanner_device,
    )

    Coordinator._async_gather_advert_data(coord)

    coord._get_or_create_device.assert_called_once_with("fresh")
    coord._get_or_create_device.return_value.process_advertisement.assert_called_once_with(scanner_device, fresh_advert)


# ---------------------------------------------------------------- pruning


class _Dev(SimpleNamespace):
    """Device stub. Hashable by address, as the real BermudaDevice is."""

    def __hash__(self):
        return hash(self.address)


NOW = 500_000.0


def _stub(address, last_seen, address_type, **kwargs):
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


def _prune_coordinator(devices, **kwargs):
    fields = {
        "devices": devices,
        "metadevices": {},
        "scanner_list": set(),
        "stamp_last_prune": 0,
        "redactions": {},
        "stamp_redactions_expiry": None,
        "irk_manager": SimpleNamespace(async_prune=lambda: None),
        "_get_device": devices.get,
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def test_prune_does_nothing_if_it_ran_recently_unless_forced(monkeypatch):
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    stale = _stub(MAC_A, NOW - PRUNE_TIME_DEFAULT - 10, "public")
    devices = {MAC_A: stale}
    coord = _prune_coordinator(devices, stamp_last_prune=NOW - 10)

    Coordinator.prune_devices(coord)
    assert MAC_A in devices

    Coordinator.prune_devices(coord, force_pruning=True)
    assert MAC_A not in devices


def test_prune_clears_expired_redaction_data_only(monkeypatch):
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    coord = _prune_coordinator({}, redactions={MAC_A: "x"}, stamp_redactions_expiry=NOW + 5)
    Coordinator.prune_devices(coord, force_pruning=True)
    assert coord.redactions == {MAC_A: "x"}

    coord.stamp_redactions_expiry = NOW - 1
    coord.stamp_last_prune = 0
    Coordinator.prune_devices(coord)
    assert coord.redactions == {}
    assert coord.stamp_redactions_expiry is None


def test_prune_over_quota_drops_the_oldest_unknown_private_addresses_first(monkeypatch):
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: NOW)
    monkeypatch.setattr(prune_module, "PRUNE_MAX_COUNT", 2)
    oldest = _stub("4a:00:00:00:00:01", NOW - 230, BDADDR_TYPE_RANDOM_RESOLVABLE)
    older = _stub("4a:00:00:00:00:02", NOW - 210, BDADDR_TYPE_RANDOM_RESOLVABLE)
    fresh = _stub("4a:00:00:00:00:03", NOW - 5, BDADDR_TYPE_RANDOM_RESOLVABLE)
    devices = {d.address: d for d in (oldest, older, fresh)}
    coord = _prune_coordinator(devices)

    Coordinator.prune_devices(coord, force_pruning=True)

    # Three devices against a limit of two: only the single oldest is shaved off.
    assert set(devices) == {older.address, fresh.address}


# ---------------------------------------------------------------- Private BLE metadevices


async def test_private_ble_devices_become_tracked_metadevices(hass: HomeAssistant, coordinator):
    pb_entry = MockConfigEntry(domain=DOMAIN_PRIVATE_BLE_DEVICE, entry_id="pb-1", data={})
    pb_entry.add_to_hass(hass)
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    pb_device = device_reg.async_get_or_create(
        config_entry_id=pb_entry.entry_id,
        identifiers={(DOMAIN_PRIVATE_BLE_DEVICE, "phone")},
        name="Phone",
    )
    device_reg.async_update_device(pb_device.id, name_by_user="Guest Phone")
    with_address = entity_reg.async_get_or_create(
        "device_tracker",
        DOMAIN_PRIVATE_BLE_DEVICE,
        f"{IRK}_device_tracker",
        config_entry=pb_entry,
        device_id=pb_device.id,
    )
    hass.states.async_set(with_address.entity_id, "home", {"current_address": "4A:BB:CC:DD:EE:99"})

    irk_two = "fedcba9876543210fedcba9876543210"
    without_address = entity_reg.async_get_or_create(
        "device_tracker", DOMAIN_PRIVATE_BLE_DEVICE, f"{irk_two}_device_tracker", config_entry=pb_entry
    )
    # A non-tracker entity of the same device is not interesting.
    entity_reg.async_get_or_create("sensor", DOMAIN_PRIVATE_BLE_DEVICE, f"{irk_two}_sensor", config_entry=pb_entry)

    coordinator._do_private_device_init = True
    coordinator.discover_private_ble_metadevices()

    assert coordinator._do_private_device_init is False
    first = coordinator.metadevices[IRK]
    assert first.create_sensor is True
    assert first.name_by_user == "Guest Phone"
    assert first.metadevice_sources == ["4a:bb:cc:dd:ee:99"]
    source = coordinator.devices["4a:bb:cc:dd:ee:99"]
    assert METADEVICE_TYPE_PRIVATE_BLE_SOURCE in source.metadevice_type
    assert coordinator.pb_state_sources[with_address.entity_id] == "4a:bb:cc:dd:ee:99"

    second = coordinator.metadevices[irk_two]
    assert second.create_sensor is True
    assert second.metadevice_sources == []
    assert coordinator.pb_state_sources[without_address.entity_id] is None

    # A second pass without the flag does nothing.
    coordinator.pb_state_sources.clear()
    coordinator.discover_private_ble_metadevices()
    assert coordinator.pb_state_sources == {}


async def test_private_ble_tracker_without_state_has_no_source_yet(hass: HomeAssistant, coordinator):
    pb_entry = MockConfigEntry(domain=DOMAIN_PRIVATE_BLE_DEVICE, entry_id="pb-2", data={})
    pb_entry.add_to_hass(hass)
    tracker = er.async_get(hass).async_get_or_create(
        "device_tracker", DOMAIN_PRIVATE_BLE_DEVICE, f"{IRK}_device_tracker", config_entry=pb_entry
    )
    coordinator._do_private_device_init = True
    coordinator.discover_private_ble_metadevices()

    assert coordinator.metadevices[IRK].metadevice_sources == []
    assert coordinator.pb_state_sources == {tracker.entity_id: None}


# ---------------------------------------------------------------- iBeacon registration


def test_ibeacon_registration_rejects_non_beacon_sources(coordinator, caplog):
    source = coordinator._get_or_create_device(MAC_A)
    with caplog.at_level(logging.ERROR):
        coordinator.register_ibeacon_source(source)
    assert "is not a valid iBeacon" in caplog.text
    assert coordinator.metadevices == {}


def test_ibeacon_registration_complains_when_source_is_not_flagged_as_beacon(coordinator, caplog):
    source = coordinator._get_or_create_device(MAC_A)
    source.beacon_unique_id = f"{BEACON_UUID}_1_2"
    with caplog.at_level(logging.ERROR):
        coordinator.register_ibeacon_source(source)
    assert "Only IBEACON_SOURCE devices" in caplog.text


def test_ibeacon_registration_creates_tracked_metadevice_for_configured_beacon(coordinator):
    unique_id = f"{BEACON_UUID}_1_2"
    coordinator.options[CONF_DEVICES] = [unique_id.upper()]
    source = coordinator._get_or_create_device(MAC_A)
    source.metadevice_type.add(METADEVICE_TYPE_IBEACON_SOURCE)
    source.beacon_unique_id = unique_id
    source.beacon_major = 1
    source.beacon_minor = 2

    coordinator.register_ibeacon_source(source)

    metadevice = coordinator.metadevices[unique_id]
    assert metadevice.create_sensor is True
    assert metadevice.metadevice_sources == [MAC_A]
    assert metadevice.beacon_major == 1

    # An untracked beacon is registered but not given sensors.
    other_id = f"{BEACON_UUID}_3_4"
    other_source = coordinator._get_or_create_device(MAC_B)
    other_source.metadevice_type.add(METADEVICE_TYPE_IBEACON_SOURCE)
    other_source.beacon_unique_id = other_id
    coordinator.register_ibeacon_source(other_source)
    assert coordinator.metadevices[other_id].create_sensor is False


# ---------------------------------------------------------------- metadevice updates


def test_metadevice_with_unseen_source_is_left_alone(coordinator):
    meta = coordinator._get_or_create_device(IRK)
    meta.metadevice_sources = ["4a:00:00:00:00:77"]  # not in the backend (any more)
    coordinator.metadevices[IRK] = meta

    coordinator.update_metadevices()

    assert meta.metadevice_sources == ["4a:00:00:00:00:77"]
    assert meta.adverts == {}


def test_ibeacon_source_that_changes_identity_is_severed_with_its_adverts(coordinator):
    meta = coordinator._get_or_create_device(f"{BEACON_UUID}_1_2")
    meta.metadevice_type.add(METADEVICE_IBEACON_DEVICE)
    meta.beacon_unique_id = f"{BEACON_UUID}_1_2"
    source = coordinator._get_or_create_device(MAC_A)
    source.beacon_unique_id = f"{BEACON_UUID}_1_9"  # the beacon now says something else
    other = coordinator._get_or_create_device(MAC_B)
    other.beacon_unique_id = f"{BEACON_UUID}_1_2"
    meta.metadevice_sources = [MAC_A, MAC_B]
    meta.adverts = {
        (MAC_A, "proxy-1"): SimpleNamespace(),
        (MAC_A, "proxy-2"): SimpleNamespace(),
        (MAC_B, "proxy-1"): SimpleNamespace(),
    }
    coordinator.metadevices[meta.address] = meta

    coordinator.update_metadevices()

    assert meta.metadevice_sources == [MAC_B]
    assert list(meta.adverts) == [(MAC_B, "proxy-1")]


def test_source_device_takes_over_the_metadevices_ref_power(coordinator):
    meta = coordinator._get_or_create_device(IRK)
    meta.ref_power = -62
    source = coordinator._get_or_create_device(MAC_A)
    assert source.ref_power == 0
    meta.metadevice_sources = [MAC_A]
    coordinator.metadevices[IRK] = meta

    coordinator.update_metadevices()

    assert source.ref_power == -62


# ---------------------------------------------------------------- time and area helpers


def test_monotonic_stamp_conversion(coordinator, monkeypatch):
    fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: 1000.0)
    monkeypatch.setattr(coordinator_module, "now", lambda: fixed_now)

    assert coordinator.dt_mono_to_datetime(970.0) == datetime(2026, 1, 1, 11, 59, 30, tzinfo=UTC)

    monkeypatch.setattr(coordinator_module, "get_age", lambda when: f"aged {when:%H:%M:%S}")
    assert coordinator.dt_mono_to_age(970.0) == "aged 11:59:30"


async def test_resolve_area_name(hass: HomeAssistant, coordinator):
    area = ar.async_get(hass).async_create("Spare Room")
    assert coordinator.resolve_area_name(area.id) == "Spare Room"
    assert coordinator.resolve_area_name("no-such-area") is None
    assert coordinator.resolve_area_name(None) is None


def test_only_tracked_devices_get_an_area_decision(coordinator):
    tracked = coordinator._get_or_create_device(MAC_A)
    tracked.create_sensor = True
    coordinator._get_or_create_device(MAC_B)

    with patch.object(coordinator, "_refresh_area_by_min_distance") as decide:
        coordinator._refresh_areas_by_min_distance()

    decide.assert_called_once_with(tracked)


def test_area_tests_text_dump_lists_every_field():
    tests = Coordinator.AreaTests()
    tests.device = "Hall Tag"
    tests.scannername = ("Living Room Proxy", "Kitchen Proxy")
    tests.distance = (3.5, 2.25)
    tests.pcnt_diff = 0.3456
    tests.reason = "WIN by not losing!"

    text = str(tests)

    assert "device               Hall Tag\n" in text
    assert "scannername          Living Room Proxy Kitchen Proxy \n" in text
    assert "distance             3.50 2.25 \n" in text
    assert "pcnt_diff            0.346\n" in text
    assert text.endswith("reason               WIN by not losing!\n")


# ---------------------------------------------------------------- area selection


def _advert(scanner, area, distance, stamp, history=None, **kwargs):
    fields = {
        "name": scanner,
        "scanner_address": scanner,
        "stamp": stamp,
        "rssi_distance": distance,
        "area_id": area,
        "area_name": area,
        "hist_distance_by_interval": [distance] * 5 if history is None else history,
        "scanner_device": SimpleNamespace(last_seen=stamp),
    }
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def _decide(adverts, incumbent=None):
    """Run the real area decision once and return what it applied (None = unplaced) plus the device."""
    chosen = {}
    device = SimpleNamespace(
        name="device",
        area_advert=adverts.get(incumbent),
        area_beyond_range=None,
        adverts=adverts,
        diag_area_switch=None,
        apply_scanner_selection=lambda adv: chosen.setdefault("adv", adv),
    )
    coord = SimpleNamespace(
        options={"max_area_radius": 20.0, "scanner_max_radius": {}},
        AreaTests=Coordinator.AreaTests,
    )
    Coordinator._refresh_area_by_min_distance(coord, device)
    adv = chosen["adv"]
    return (None if adv is None else adv.scanner_address), device


def test_advert_too_old_cannot_win_the_area():
    now = coordinator_module.monotonic_time_coarse()
    adverts = {
        "stale": _advert("stale", "Den", 1.0, now - AREA_MAX_AD_AGE - 5),
        "fresh": _advert("fresh", "Kitchen", 6.0, now),
    }
    winner, _ = _decide(adverts)
    assert winner == "fresh"
    assert _decide({"stale": adverts["stale"]})[0] is None


def test_newer_closer_advert_in_the_same_area_takes_over():
    now = coordinator_module.monotonic_time_coarse()
    adverts = {
        "old": _advert("old", "Den", 4.0, now - 3),
        "new": _advert("new", "Den", 3.9, now),
    }
    winner, device = _decide(adverts, incumbent="old")
    assert winner == "new"
    assert device.diag_area_switch is not None


def test_same_area_advert_that_is_not_clearly_newer_does_not_take_over():
    now = coordinator_module.monotonic_time_coarse()
    adverts = {
        "old": _advert("old", "Den", 4.0, now - 0.5),
        "new": _advert("new", "Den", 3.9, now),
    }
    assert _decide(adverts, incumbent="old")[0] == "old"


def test_much_closer_receiver_wins_outright_without_history():
    now = coordinator_module.monotonic_time_coarse()
    adverts = {
        "far": _advert("far", "Den", 10.0, now),
        "near": _advert("near", "Kitchen", 5.0, now, history=[]),
    }
    winner, device = _decide(adverts, incumbent="far")
    assert winner == "near"
    assert "WIN by not losing!" in device.diag_area_switch


# ---------------------------------------------------------------- dump_devices and redaction


def _call(**data):
    return SimpleNamespace(data=data)


async def test_dump_devices_selected_by_address(coordinator):
    coordinator._get_or_create_device(MAC_A)
    coordinator._get_or_create_device(MAC_B)

    out = await coordinator.service_dump_devices(_call(addresses=MAC_A.upper(), redact=False))

    assert set(out) == {MAC_A}


async def test_dump_devices_configured_devices_include_scanners_metadevices_and_private_sources(coordinator):
    for address in (MAC_A, MAC_B, "aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:04", "aa:bb:cc:dd:ee:05"):
        coordinator._get_or_create_device(address)
    coordinator.options[CONF_DEVICES] = [MAC_A]
    coordinator._scanner_list.add("aa:bb:cc:dd:ee:03")
    coordinator.metadevices["aa:bb:cc:dd:ee:04"] = coordinator.devices["aa:bb:cc:dd:ee:04"]
    coordinator.pb_state_sources = {"device_tracker.phone": "aa:bb:cc:dd:ee:05", "device_tracker.unseen": None}

    out = await coordinator.service_dump_devices(_call(configured_devices=True, redact=False))

    assert set(out) == {MAC_A, "aa:bb:cc:dd:ee:03", "aa:bb:cc:dd:ee:04", "aa:bb:cc:dd:ee:05"}


async def test_dump_devices_is_redacted_by_default_and_warns_when_slow(coordinator, monkeypatch, caplog):
    coordinator._get_or_create_device(MAC_A)
    ticks = iter(range(0, 10_000, 10))  # every look at the clock is ten seconds later
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: next(ticks))
    caplog.set_level(logging.WARNING)

    out = await coordinator.service_dump_devices(_call())

    assert MAC_A not in str(out)
    assert "Dump devices redaction took" in caplog.text
    assert "Redaction list update took" in caplog.text


def test_redaction_names_every_kind_of_address_without_leaking_it(coordinator):
    beacon_cfg = f"{BEACON_UUID}_7_8"
    other_cfg = "mystery-config-value"
    coordinator.options[CONF_DEVICES] = [beacon_cfg.upper(), MAC_A.upper(), other_cfg]

    irk_dev = coordinator._get_or_create_device(IRK)
    irk_dev.address_type = ADDR_TYPE_PRIVATE_BLE_DEVICE
    beacon_other = f"{'0f' * 16}_1_2"
    coordinator._get_or_create_device(beacon_other)
    mac_dev = coordinator._get_or_create_device(MAC_B)
    odd = coordinator._get_or_create_device("not-an-address")

    coordinator.redaction_list_update()
    redactions = coordinator.redactions

    assert redactions[beacon_cfg] == f"{BEACON_UUID[:4]}::CFG_iBea_1::_7_8"
    assert redactions[BEACON_UUID] == f"{BEACON_UUID[:4]}::CFG_iBea_1__7_8::"
    assert redactions[MAC_A] == "aa::CFG_MAC_2::01"
    assert redactions[other_cfg] == "CFG_OTHER_3"
    assert redactions[IRK] == f"{IRK[:4]}::IRK_DEV_4"
    assert redactions[beacon_other] == "0f0f::OTHER_iBea_5::_1_2"
    assert redactions["0f" * 16] == "0f0f::OTHER_iBea_5__1_2::"
    assert redactions[mac_dev.address] == "aa::OTHER_MAC_6::02"
    assert redactions[odd.address] == "OTHER_7"
    assert coordinator.stamp_redactions_expiry is not None
    # A second run adds nothing for addresses already named.
    before = dict(redactions)
    coordinator.redaction_list_update()
    assert coordinator.redactions == before

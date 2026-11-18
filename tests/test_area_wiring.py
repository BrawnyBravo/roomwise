"""
The area loop passes the room hold settings to the device and fills its "area reason".

These tests run the real `_refresh_area_by_min_distance` on a real BermudaDevice
(only the area registry lookup is replaced), with a fake clock and fake adverts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.const import STATE_HOME, STATE_NOT_HOME

from custom_components.roomwise import coordinator_area
from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.const import (
    CONF_AREA_DWELL,
    CONF_UNKNOWN_GRACE,
    DEFAULT_UNKNOWN_GRACE,
)
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator
from custom_components.roomwise.room_stability import APPLY, HOLD_DWELL, HOLD_GRACE

NAMES = {"area_lr": "Living Room", "area_kn": "Kitchen", "area_hl": "Hallway"}
FAR = 1000.0  # metres: beyond any exit margin


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(coordinator_area, "monotonic_time_coarse", clk)
    return clk


def _advert(relay: str, area: str, distance: float | None, clock: Clock) -> SimpleNamespace:
    return SimpleNamespace(
        name=relay,
        scanner_address=relay,
        stamp=clock.now,
        rssi=-60.0,
        rssi_distance=distance,
        area_id=area,
        area_name=NAMES[area],
        hist_distance_by_interval=[],  # too short for the history test: only the distance tests decide
        scanner_device=SimpleNamespace(last_seen=clock.now),
    )


def _freshen(clock: Clock, *adverts: SimpleNamespace) -> None:
    for advert in adverts:
        advert.stamp = clock.now
        advert.scanner_device.last_seen = clock.now


def _new_device() -> BermudaDevice:
    dev = BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=MagicMock(options={}))
    dev.zone = STATE_HOME  # the real loop sets this from last_seen before the area pass

    def _set(area_id):
        dev.area_id = area_id
        dev.area_name = NAMES.get(area_id)

    dev._update_area_and_floor = _set  # the real one reads the area registry
    return dev


@pytest.fixture
def device():
    return _new_device()


def _coord(**options) -> SimpleNamespace:
    return SimpleNamespace(
        options={"max_area_radius": 20.0, "scanner_max_radius": {}, **options},
        AreaTests=BermudaDataUpdateCoordinator.AreaTests,
        resolve_area_name=NAMES.get,
    )


def _refresh(coord: SimpleNamespace, device: BermudaDevice) -> dict:
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    return device.area_explain


def test_first_pick_gives_the_instawin_reason_then_the_room_is_kept(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord()

    explain = _refresh(coord, device)
    assert device.area_name == "Living Room"
    assert explain["room"] == "Living Room"
    assert explain["relay"] == "relay_a"
    assert explain["reason"] == "First receiver in range"
    assert explain["hold"] is None
    assert device.room_hold_state == APPLY

    clock.now += 5
    _freshen(clock, lr)
    explain = _refresh(coord, device)
    assert explain["reason"] == "Kept current room"
    assert explain["room"] == "Living Room"
    assert device.area_advert is lr


def test_no_receiver_in_range_keeps_the_room_during_grace_then_shows_unknown(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord()  # default options: grace is DEFAULT_UNKNOWN_GRACE
    _refresh(coord, device)

    clock.now += 1  # one update after the room was last picked
    lr.rssi_distance = FAR  # the only receiver is now out of range
    explain = _refresh(coord, device)
    assert device.area_advert is None
    assert device.room_hold_state == HOLD_GRACE
    assert device.area_name == "Living Room"  # still shown
    assert explain["reason"] == "No receiver in range"
    assert explain["hold"] == "grace"
    # The hold ends grace seconds after the room was last picked, one update before this one.
    assert explain["hold_seconds_left"] == DEFAULT_UNKNOWN_GRACE - 1
    assert explain["room"] == "Living Room"

    clock.now += DEFAULT_UNKNOWN_GRACE + 1  # the grace time has passed
    explain = _refresh(coord, device)
    assert device.room_hold_state == APPLY
    assert device.area_id is None
    assert explain["room"] is None
    assert explain["reason"] == "No receiver in range"
    assert explain["hold"] is None
    assert explain["recent_switches"][0]["from"] == "Living Room"
    assert explain["recent_switches"][0]["to"] is None


def test_a_timed_out_reading_counts_as_no_receiver(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord()
    _refresh(coord, device)

    clock.now += 40
    lr.rssi_distance = None  # the advert timed out: the loop keeps it, the device does not
    explain = _refresh(coord, device)
    assert device.area_advert is None
    assert device.room_hold_state == HOLD_GRACE
    assert explain["reason"] == "No receiver in range"
    assert explain["relay"] is None
    assert explain["room"] == "Living Room"

    clock.now += DEFAULT_UNKNOWN_GRACE + 1
    explain = _refresh(coord, device)
    assert device.area_id is None
    assert explain["reason"] == "No receiver in range"
    assert explain["recent_switches"][0]["reason"] == "No receiver in range"


def test_grace_option_zero_shows_unknown_at_once(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord(**{CONF_UNKNOWN_GRACE: 0})
    _refresh(coord, device)

    clock.now += 1
    lr.rssi_distance = FAR
    explain = _refresh(coord, device)
    assert device.area_id is None
    assert explain["room"] is None
    assert explain["reason"] == "No receiver in range"
    assert explain["hold"] is None


def test_dwell_holds_the_room_names_the_pending_room_and_then_switches(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    kn = _advert("relay_b", "area_kn", 9.0, clock)
    device.adverts = {"a": lr, "b": kn}
    coord = _coord(**{CONF_AREA_DWELL: 30, CONF_UNKNOWN_GRACE: 0})
    _refresh(coord, device)
    assert device.area_name == "Living Room"

    clock.now += 9  # the Living Room is still the pick for a while (this is what the shown room is held against)
    _freshen(clock, lr, kn)
    _refresh(coord, device)

    # The Kitchen relay is now clearly closer, but the device has to stay closest for 30 s.
    clock.now += 1
    lr.rssi_distance = 9.0
    kn.rssi_distance = 2.0
    _freshen(clock, lr, kn)
    explain = _refresh(coord, device)
    assert device.room_hold_state == HOLD_DWELL
    assert device.area_name == "Living Room"
    assert explain["room"] == "Living Room"
    assert explain["reason"] == "Waiting 30 s before moving to Kitchen"
    assert explain["hold"] == "dwell"
    assert explain["hold_seconds_left"] == 29  # counted from the Living Room's last pick, 1 s ago
    assert explain["pending_room"] == "Kitchen"  # a name, not the area id
    assert explain["pending_relay"] == "relay_b"  # the receiver picked for the new room
    assert explain["relay"] == "relay_a"  # the receiver backing the shown room
    assert explain["distance_m"] == 9.0
    assert explain["runner_up_room"] == "Kitchen"
    assert explain["recent_switches"] == []

    clock.now += 28
    _freshen(clock, lr, kn)
    explain = _refresh(coord, device)
    assert device.area_name == "Living Room"
    assert explain["hold_seconds_left"] == 1

    clock.now += 2  # 30 s since the Kitchen was first picked
    _freshen(clock, lr, kn)
    explain = _refresh(coord, device)
    assert device.room_hold_state == APPLY
    assert device.area_name == "Kitchen"
    assert explain["room"] == "Kitchen"
    assert explain["reason"] == "Stayed closest for 30 s"
    assert explain["hold"] is None
    assert explain["pending_room"] is None
    assert len(explain["recent_switches"]) == 1
    switch = explain["recent_switches"][0]
    assert (switch["from"], switch["to"], switch["reason"]) == ("Living Room", "Kitchen", "Stayed closest for 30 s")


def test_a_normal_win_carries_the_winning_reason_even_if_a_later_challenger_loses(clock, device):
    lr = _advert("relay_a", "area_lr", 6.0, clock)
    kn = _advert("relay_b", "area_kn", 2.0, clock)  # much closer than the current room: wins
    hl = _advert("relay_c", "area_hl", 1.9, clock)  # barely closer than the new winner: loses
    device.adverts = {"a": lr}
    coord = _coord(**{CONF_UNKNOWN_GRACE: 0})
    _refresh(coord, device)  # the device starts in the Living Room
    assert device.area_name == "Living Room"

    clock.now += 5
    device.adverts = {"a": lr, "b": kn, "c": hl}
    _freshen(clock, lr, kn, hl)
    explain = _refresh(coord, device)
    assert device.area_advert is kn
    assert "LOSS" in device.diag_area_switch  # the last test run was the loss ...
    assert explain["room"] == "Kitchen"
    assert explain["reason"] == "Much closer than the current room"  # ... but the room was chosen by the win
    assert explain["recent_switches"][0]["reason"] == "Much closer than the current room"
    assert explain["runner_up_relay"] == "relay_c"


def test_a_dwell_hold_with_no_receiver_in_the_shown_room_has_no_relay(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    kn = _advert("relay_b", "area_kn", 9.0, clock)
    device.adverts = {"a": lr, "b": kn}
    coord = _coord(**{CONF_AREA_DWELL: 30, CONF_UNKNOWN_GRACE: 0})
    _refresh(coord, device)

    clock.now += 10
    lr.rssi_distance = FAR  # the shown room's only receiver is out of range
    kn.rssi_distance = 2.0
    _freshen(clock, lr, kn)
    explain = _refresh(coord, device)
    assert device.room_hold_state == HOLD_DWELL
    assert explain["room"] == "Living Room"
    assert explain["relay"] is None
    assert explain["pending_relay"] == "relay_b"


def test_a_room_change_without_a_dwell_or_a_new_pick_says_the_receivers_room_changed(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord(**{CONF_UNKNOWN_GRACE: 0})
    _refresh(coord, device)

    clock.now += 5
    lr.area_id, lr.area_name = "area_kn", "Kitchen"  # the receiver was moved to another room
    _freshen(clock, lr)
    explain = _refresh(coord, device)
    assert device.area_name == "Kitchen"
    assert explain["reason"] == "Receiver's room changed"
    assert explain["recent_switches"][0]["reason"] == "Receiver's room changed"


def test_a_device_that_is_away_does_not_get_the_grace_period(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord()  # default options: grace is DEFAULT_UNKNOWN_GRACE
    _refresh(coord, device)

    clock.now += 40
    lr.rssi_distance = FAR
    device.zone = STATE_NOT_HOME  # not heard for longer than the tracker timeout
    explain = _refresh(coord, device)
    assert device.room_hold_state == APPLY
    assert device.area_id is None
    assert explain["room"] is None
    assert explain["hold"] is None


def _dropout(clock, device, coord, lr, seconds=20, step=5):
    """Lose the only receiver for `seconds`, then get it back. Returns the room shown at each step."""
    shown = []
    for _ in range(seconds // step):
        clock.now += step
        lr.rssi_distance = FAR
        _refresh(coord, device)
        shown.append(device.area_name)
    clock.now += step
    lr.rssi_distance = 2.0
    _freshen(clock, lr)
    _refresh(coord, device)
    shown.append(device.area_name)
    return shown


def test_a_short_dropout_changes_nothing_with_the_default_grace_but_flickers_without_it(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    device.adverts = {"a": lr}
    coord = _coord()
    _refresh(coord, device)
    assert _dropout(clock, device, coord, lr) == ["Living Room"] * 5
    assert device.area_explain["recent_switches"] == []

    # The same dropout with the grace period switched off: unknown, then back again.
    other = _new_device()
    lr2 = _advert("relay_a", "area_lr", 2.0, clock)
    other.adverts = {"a": lr2}
    coord0 = _coord(**{CONF_UNKNOWN_GRACE: 0})
    _refresh(coord0, other)
    assert _dropout(clock, other, coord0, lr2) == [None, None, None, None, "Living Room"]
    assert len(other.area_explain["recent_switches"]) == 2


def test_flickering_picks_cannot_hold_the_shown_room_for_ever(clock, device):
    lr = _advert("relay_a", "area_lr", 2.0, clock)
    kn = _advert("relay_b", "area_kn", 9.0, clock)
    device.adverts = {"a": lr, "b": kn}
    coord = _coord(**{CONF_AREA_DWELL: 30, CONF_UNKNOWN_GRACE: 60})
    _refresh(coord, device)
    assert device.area_name == "Living Room"

    # The Living Room receiver goes out of range; the Kitchen receiver comes and goes.
    lr.rssi_distance = FAR
    changed_at = None
    for second in range(1, 121):
        clock.now += 1
        kn.rssi_distance = 2.0 if second % 2 else FAR
        _freshen(clock, kn, lr)
        _refresh(coord, device)
        if device.area_name != "Living Room":
            changed_at = second
            break
    assert changed_at is not None
    assert changed_at <= 60

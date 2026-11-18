"""Tests for the "why this room" explainer and its sensor."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.roomwise import sensor
from custom_components.roomwise.area_explain import (
    MAX_RECENT_SWITCHES,
    build_explain,
    find_in_room,
    plain_reason,
    summary,
)
from custom_components.roomwise.const import AREA_MAX_AD_AGE

NOW = 1000.0
ISO = "2026-01-01T12:00:00-08:00"


def _ad(name, area_id, area_name, dist, rssi=-70, stamp=NOW):
    return SimpleNamespace(
        scanner_address=name,
        name=name,
        area_id=area_id,
        area_name=area_name,
        rssi=rssi,
        rssi_distance=dist,
        stamp=stamp,
    )


def _dev(*ads, area_name="Kitchen", area_id="kitchen"):
    if area_name is None:
        area_id = None
    return SimpleNamespace(area_name=area_name, area_id=area_id, adverts={a.scanner_address: a for a in ads})


def _limit(_ad):
    return 10.0


def _build(device, winner, previous=None, **kw):
    args = {"reason": "Consistently closer for several readings", "hold_state": None, "hold_remaining": None}
    args.update(kw)
    return build_explain(device, winner, NOW, _limit, previous=previous, now_iso=ISO, **args)


def test_winner_and_runner_up_in_other_room():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.123, -71)
    b = _ad("relay_b", "living", "Living Room", 4.3, -80)
    c = _ad("relay_c", "living", "Living Room", 6.0)
    e = _build(_dev(a, b, c), a)
    assert e["relay"] == "relay_a"
    assert e["distance_m"] == 2.12
    assert e["runner_up_relay"] == "relay_b"
    assert e["runner_up_room"] == "Living Room"
    assert e["margin_m"] == 2.18
    assert summary(e) == "Kitchen via relay_a. Consistently closer for several readings"


def test_runner_up_filters():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    same_room = _ad("relay_s", "kitchen", "Kitchen", 2.5)
    stale = _ad("relay_t", "hall", "Hall", 2.6, stamp=NOW - AREA_MAX_AD_AGE - 1)
    far = _ad("relay_f", "den", "Den", 11.0)
    no_area = _ad("relay_n", None, None, 2.7)
    no_dist = _ad("relay_d", "porch", "Porch", None)
    e = _build(_dev(a, same_room, stale, far, no_area, no_dist), a)
    assert e["runner_up_relay"] is None
    assert e["margin_m"] is None
    assert e["runner_up_room"] is None


def test_runner_up_picks_smallest_distance():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    b = _ad("relay_b", "hall", "Hall", 5.0)
    c = _ad("relay_c", "den", "Den", 3.0)
    assert _build(_dev(a, b, c), a)["runner_up_relay"] == "relay_c"


def test_no_winner():
    e = _build(_dev(area_name=None), None, reason=None)
    assert e["room"] is None
    assert e["relay"] is None
    assert e["reason"] == "Kept current room"
    assert summary(e) == "Unknown. Kept current room"
    assert summary(_build(_dev(area_name=None), None, reason="No receiver in range")) == "Unknown. No receiver in range"
    assert summary(_build(_dev(), None, reason="Kept current room")) == "Kitchen. Kept current room"


def test_holds():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    grace = _build(_dev(), None, hold_state="grace", hold_remaining=18.2)
    assert grace["hold"] == "grace"
    assert summary(grace) == "Kitchen. Holding the room, no receiver in range"
    assert grace["hold_seconds_left"] == 18.2
    dwell = _build(_dev(a), a, hold_state="dwell", hold_remaining=3.6, pending_room="Living Room")
    assert dwell["pending_room"] == "Living Room"
    assert summary(dwell) == "Kitchen via relay_a. Waiting to move to Living Room"
    no_target = _build(_dev(a), a, hold_state="dwell", hold_remaining=2)
    assert summary(no_target) == "Kitchen via relay_a. Waiting for a room change"
    assert _build(_dev(a), a, hold_state="bogus")["hold"] is None


def test_the_summary_does_not_change_when_only_numbers_change():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0, -71)
    b = _ad("relay_b", "living", "Living Room", 4.0)
    first = _build(_dev(a, b), a, hold_state="dwell", hold_remaining=30, pending_room="Living Room")
    a.rssi_distance, a.rssi, b.rssi_distance = 2.6, -75.0, 3.1
    second = _build(_dev(a, b), a, hold_state="dwell", hold_remaining=12, pending_room="Living Room")
    assert first["distance_m"] != second["distance_m"]
    assert first["hold_seconds_left"] != second["hold_seconds_left"]
    assert summary(first) == summary(second)
    steady_1 = _build(_dev(a, b), a)
    a.rssi_distance = 1.1
    assert summary(steady_1) == summary(_build(_dev(a, b), a))


def test_pending_relay_and_shown_room_runner_up():
    shown = _ad("relay_a", "kitchen", "Kitchen", 6.0)
    picked = _ad("relay_b", "living", "Living Room", 2.0)
    other = _ad("relay_c", "hall", "Hall", 7.0)
    dev = _dev(shown, picked, other)
    e = _build(
        dev, shown, hold_state="dwell", pending_room="Living Room", pending_relay="relay_b", shown_area_id="kitchen"
    )
    assert e["relay"] == "relay_a"
    assert e["pending_relay"] == "relay_b"
    assert e["runner_up_relay"] == "relay_b"
    # with no receiver left in the shown room, the shown room can still not be the runner-up
    e = _build(_dev(picked, shown), None, hold_state="dwell", pending_relay="relay_b", shown_area_id="kitchen")
    assert e["relay"] is None
    assert e["runner_up_room"] == "Living Room"
    assert _build(dev, shown, pending_relay="relay_b")["pending_relay"] is None  # only during a dwell hold


def test_find_in_room():
    near = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    far = _ad("relay_b", "kitchen", "Kitchen", 3.0)
    stale = _ad("relay_c", "kitchen", "Kitchen", 1.0, stamp=NOW - AREA_MAX_AD_AGE - 1)
    elsewhere = _ad("relay_d", "hall", "Hall", 0.5)
    dev = _dev(far, near, stale, elsewhere)
    assert find_in_room(dev, "kitchen", NOW, _limit) is near
    assert find_in_room(dev, "den", NOW, _limit) is None
    assert find_in_room(dev, None, NOW, _limit) is None


def test_plain_reasons():
    assert plain_reason(None) is None
    assert plain_reason("WIN by not losing!") == "Much closer than the current room"
    assert plain_reason("WIN on historical min/max") == "Consistently closer for several readings"
    assert plain_reason("WIN awarded for same area, newer, closer advert") == "Newer, closer reading in the same room"
    assert plain_reason("WIN against an incumbent beyond its max radius") == "Current room's receiver out of range"
    assert plain_reason("WIN against no valid current receiver") == "First receiver in range"
    assert plain_reason("something new") == "Something new"


def test_switch_history():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    first = _build(_dev(a), a)
    assert first["recent_switches"] == []
    same = _build(_dev(a), a, previous=first)
    assert same["recent_switches"] == []
    prev = first
    for i in range(12):
        name = f"Room {i}"
        prev = _build(
            _dev(a, area_name=name, area_id=f"room_{i}"), a, previous=prev, reason=None if i == 0 else f"r{i}"
        )
    sw = prev["recent_switches"]
    assert len(sw) == MAX_RECENT_SWITCHES
    assert sw[0] == {"at": ISO, "from": "Room 10", "to": "Room 11", "reason": "r11"}
    assert sw[-1]["to"] == "Room 2"
    assert _build(_dev(a), a, previous={"room": None})["recent_switches"][0]["from"] is None
    # a room renamed in Home Assistant is not a switch: the ids are compared
    renamed = _build(_dev(a, area_name="Kitchenette"), a, previous=first)
    assert renamed["recent_switches"] == []
    assert renamed["room"] == "Kitchenette"


def test_default_now_iso():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    e = build_explain(_dev(a), a, NOW, _limit, "x", None, None, {"room": "Hall"})
    assert "T" in e["recent_switches"][0]["at"]


def test_summary_capped():
    long = "L" * 200
    e = {"room": long, "relay": "r" * 100, "distance_m": 1.0, "reason": "z" * 100}
    out = summary(e)
    assert len(out) == 255
    assert out.endswith("...")
    assert len(summary({"room": "A", "relay": "r", "distance_m": 1.0})) < 255


def _sensor(device):
    ent = sensor.BermudaSensorAreaSwitchReason.__new__(sensor.BermudaSensorAreaSwitchReason)
    ent._device = device
    ent.coordinator = MagicMock()
    return ent


def test_sensor_value_attributes_and_fallback():
    explain = {"room": "Kitchen", "relay": "relay_a", "distance_m": 2.0, "rssi": -70.0, "reason": "Ok"}
    dev = SimpleNamespace(area_explain=explain, diag_area_switch="old", unique_id="dev")
    ent = _sensor(dev)
    assert ent.native_value == "Kitchen via relay_a. Ok"
    assert ent.extra_state_attributes == explain
    assert "recent_switches" in sensor.BermudaSensorAreaSwitchReason._unrecorded_attributes
    dev.area_explain = None
    assert ent.native_value == "old"
    assert ent.extra_state_attributes is None
    dev.diag_area_switch = None
    assert ent.native_value is None


def test_number_only_changes_do_not_cause_a_write_until_the_refresh(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(sensor, "monotonic_time_coarse", lambda: now[0])
    explain = {"room": "Kitchen", "room_id": "kitchen", "relay": "relay_a", "distance_m": 2.0, "reason": "Ok"}
    dev = SimpleNamespace(area_explain=explain, diag_area_switch=None, unique_id="dev")
    ent = _sensor(dev)
    first = ent._state_fingerprint()

    dev.area_explain = {**explain, "distance_m": 3.5, "rssi": -80.0, "hold_seconds_left": 12}
    now[0] += 5
    assert ent._state_fingerprint() == first  # only numbers changed: no write

    dev.area_explain = {**explain, "relay": "relay_b"}
    assert ent._state_fingerprint() != first  # a decision change writes at once

    dev.area_explain = explain
    now[0] += sensor.AREA_REASON_REFRESH
    assert ent._state_fingerprint() != first  # the numbers are refreshed every 30 s


def test_every_detail_is_kept_out_of_the_history_database():
    a = _ad("relay_a", "kitchen", "Kitchen", 2.0)
    keys = set(_build(_dev(a), a))
    assert sensor.BermudaSensorAreaSwitchReason._unrecorded_attributes == keys

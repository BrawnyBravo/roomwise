"""Dwell before a room change, and grace before unknown (room_stability + BermudaDevice)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.room_stability import (
    APPLY,
    HOLD_DWELL,
    HOLD_GRACE,
    RoomHold,
    decide,
    remaining,
)

# ---- decide() ----------------------------------------------------------------


def test_same_room_applies_and_clears_everything():
    hold = RoomHold("kitchen", 5.0, 6.0)
    assert decide(hold, "living", "living", 10, 30, 30) == APPLY
    assert hold == RoomHold(confirmed_at=10)  # the shown room was just picked again


def test_still_unknown_applies():
    hold = RoomHold(lost_since=1.0)
    assert decide(hold, None, None, 10, 30, 30) == APPLY
    assert hold == RoomHold()


def test_signal_lost_is_held_during_grace():
    hold = RoomHold()
    assert decide(hold, "living", None, 100, 0, 60) == HOLD_GRACE
    assert hold.lost_since == 100
    assert decide(hold, "living", None, 159, 0, 60) == HOLD_GRACE
    assert hold.lost_since == 100  # the clock keeps running from the first loss


def test_signal_lost_applies_once_grace_has_run_out():
    hold = RoomHold()
    decide(hold, "living", None, 100, 0, 60)
    assert decide(hold, "living", None, 160, 0, 60) == APPLY
    assert hold.lost_since is None


def test_signal_lost_with_no_grace_applies_at_once():
    hold = RoomHold(lost_since=5.0)
    assert decide(hold, "living", None, 100, 0, 0) == APPLY
    assert hold.lost_since is None


def test_signal_lost_clears_a_pending_room():
    hold = RoomHold("kitchen", 90.0, None)
    assert decide(hold, "living", None, 100, 30, 60) == HOLD_GRACE
    assert hold.pending_area_id is None
    assert hold.pending_since is None


def test_different_room_is_held_during_dwell():
    hold = RoomHold()
    assert decide(hold, "living", "kitchen", 100, 30, 0) == HOLD_DWELL
    assert (hold.pending_area_id, hold.pending_since) == ("kitchen", 100)
    assert decide(hold, "living", "kitchen", 129, 30, 0) == HOLD_DWELL
    assert hold.pending_since == 100


def test_different_room_applies_after_dwell_and_resets():
    hold = RoomHold()
    decide(hold, "living", "kitchen", 100, 30, 0)
    assert decide(hold, "living", "kitchen", 130, 30, 0) == APPLY
    assert hold == RoomHold(confirmed_at=130)


def test_a_third_room_restarts_the_dwell():
    hold = RoomHold()
    decide(hold, "living", "kitchen", 100, 30, 0)
    assert decide(hold, "living", "hall", 120, 30, 0) == HOLD_DWELL
    assert (hold.pending_area_id, hold.pending_since) == ("hall", 120)


def test_a_different_room_cancels_a_running_grace():
    hold = RoomHold(lost_since=50.0)
    assert decide(hold, "living", "kitchen", 60, 30, 60) == HOLD_DWELL
    assert hold.lost_since is None


@pytest.mark.parametrize("current", ["living", None])
def test_room_change_is_instant_without_dwell_or_from_unknown(current):
    hold = RoomHold("hall", 1.0, None)
    dwell = 0 if current else 30
    assert decide(hold, current, "kitchen", 100, dwell, 60) == APPLY
    assert hold.pending_area_id is None
    assert hold.pending_since is None


# ---- the shown room can not be held for ever ---------------------------------


def _first_change(picks, dwell, grace, shown="living", seconds=300):
    """Run one pick per second; return the second the shown room first changed (None: never)."""
    hold = RoomHold()
    decide(hold, shown, shown, 0, dwell, grace)  # the shown room was picked at second 0
    current = shown
    for t in range(1, seconds + 1):
        pick = picks(t)
        if decide(hold, current, pick, t, dwell, grace) == APPLY:
            if pick != current:
                return t
            current = pick
    return None


def test_a_pick_flickering_between_another_room_and_nothing_still_ends_the_hold():
    # Each "kitchen" pick restarts the dwell and each "none" pick restarts the grace.
    changed = _first_change(lambda t: "kitchen" if t % 2 else None, dwell=30, grace=60)
    assert changed is not None
    assert changed <= max(30, 60)


def test_a_pick_flickering_between_two_other_rooms_still_ends_the_hold():
    changed = _first_change(lambda t: "kitchen" if t % 2 else "hall", dwell=30, grace=60)
    assert changed is not None
    assert changed <= max(30, 60)
    changed = _first_change(lambda t: "kitchen" if t % 2 else "hall", dwell=30, grace=0)
    assert changed is not None
    assert changed <= 30


def test_the_cap_counts_from_the_last_time_the_shown_room_was_picked():
    hold = RoomHold()
    decide(hold, "living", "living", 0, 30, 60)
    decide(hold, "living", "living", 50, 30, 60)  # still picked at 50
    assert hold.confirmed_at == 50
    assert decide(hold, "living", None, 51, 30, 60) == HOLD_GRACE
    assert decide(hold, "living", "kitchen", 109, 30, 60) == HOLD_DWELL
    assert decide(hold, "living", None, 110, 30, 60) == APPLY  # 60 s after 50
    assert hold.confirmed_at is None  # unknown is shown now


# ---- remaining() -------------------------------------------------------------


def test_remaining_for_dwell_and_grace():
    hold = RoomHold("kitchen", 100.0, None)
    assert remaining(hold, HOLD_DWELL, 110, 30, 60) == 20
    assert remaining(hold, HOLD_DWELL, 500, 30, 60) == 0.0  # never negative
    hold = RoomHold(lost_since=100.0)
    assert remaining(hold, HOLD_GRACE, 110, 30, 60) == 50
    assert remaining(hold, HOLD_GRACE, 500, 30, 60) == 0.0


def test_remaining_is_none_when_nothing_is_held():
    assert remaining(RoomHold(), APPLY, 10, 30, 60) is None
    assert remaining(RoomHold(), None, 10, 30, 60) is None
    assert remaining(RoomHold(), HOLD_DWELL, 10, 30, 60) is None  # no timer running
    assert remaining(RoomHold(), HOLD_GRACE, 10, 30, 60) is None


# ---- BermudaDevice.apply_scanner_selection -------------------------------------


@pytest.fixture
def device():
    coord = MagicMock()
    coord.options = {}
    dev = BermudaDevice(address="AA:BB:CC:DD:EE:FF", coordinator=coord)

    def _set(area_id):
        dev.area_id = area_id
        dev.area_name = area_id.title() if area_id else None

    dev._update_area_and_floor = _set
    return dev


def _adv(area_id, dist=2.0, rssi=-60):
    return SimpleNamespace(area_id=area_id, rssi_distance=dist, rssi=rssi)


def test_defaults_behave_as_before(device):
    device.apply_scanner_selection(_adv("kitchen"), 100)
    assert device.area_id == "kitchen"
    assert device.room_hold_state == APPLY
    device.apply_scanner_selection(_adv("hall", 3.0, -70), 101)
    assert device.area_id == "hall"
    device.apply_scanner_selection(None, 102)
    assert device.area_id is None
    assert device.area_distance is None
    assert device.area_advert is None
    assert device.area_last_seen == "Hall"


def test_room_is_kept_during_grace_then_unknown(device):
    device.apply_scanner_selection(_adv("kitchen"), 110, grace=60)
    device.apply_scanner_selection(None, 110, grace=60)
    assert device.room_hold_state == HOLD_GRACE
    assert device.area_id == "kitchen"
    assert device.area_distance is None
    assert device.area_rssi is None
    assert device.area_advert is None  # the algorithm's state is still "nothing selected"
    device.apply_scanner_selection(None, 169, grace=60)
    assert device.area_id == "kitchen"
    device.apply_scanner_selection(None, 170, grace=60)
    assert device.room_hold_state == APPLY
    assert device.area_id is None
    assert device.area_last_seen == "Kitchen"


def test_signal_returning_during_grace_keeps_the_room_and_resets(device):
    device.apply_scanner_selection(_adv("kitchen"), 100, grace=60)
    device.apply_scanner_selection(None, 110, grace=60)
    back = _adv("kitchen", 1.5, -50)
    device.apply_scanner_selection(back, 120, grace=60)
    assert device.room_hold_state == APPLY
    assert device.area_distance == 1.5
    assert device.area_advert is back
    assert device.room_hold.lost_since is None
    device.apply_scanner_selection(None, 121, grace=60)  # a fresh grace period starts now
    device.apply_scanner_selection(None, 179, grace=60)  # (the first one would have ended at 170)
    assert device.area_id == "kitchen"


def test_room_is_kept_during_dwell_then_switches(device):
    device.apply_scanner_selection(_adv("kitchen"), 110, dwell=30)
    hall = _adv("hall", 3.0, -70)
    device.apply_scanner_selection(hall, 110, dwell=30)
    assert device.room_hold_state == HOLD_DWELL
    assert device.area_id == "kitchen"
    assert device.area_distance == 3.0  # numbers describe the receiver now picked
    assert device.area_rssi == -70
    assert device.area_advert is hall
    device.apply_scanner_selection(hall, 139, dwell=30)
    assert device.area_id == "kitchen"
    device.apply_scanner_selection(hall, 140, dwell=30)
    assert device.room_hold_state == APPLY
    assert device.area_id == "hall"
    assert device.area_last_seen == "Hall"


def test_bouncing_back_to_the_old_room_resets_the_pending_change(device):
    device.apply_scanner_selection(_adv("kitchen"), 100, dwell=30)
    device.apply_scanner_selection(_adv("hall"), 110, dwell=30)
    device.apply_scanner_selection(_adv("kitchen"), 120, dwell=30)
    assert device.room_hold_state == APPLY
    assert device.room_hold.pending_area_id is None
    device.apply_scanner_selection(_adv("hall"), 120, dwell=30)  # the wait starts again from 120
    device.apply_scanner_selection(_adv("hall"), 149, dwell=30)
    assert device.area_id == "kitchen"
    device.apply_scanner_selection(_adv("hall"), 150, dwell=30)
    assert device.area_id == "hall"


def test_unknown_to_room_is_instant_even_with_dwell(device):
    device.apply_scanner_selection(_adv("kitchen"), 100, dwell=30, grace=60)
    assert device.area_id == "kitchen"
    assert device.room_hold_state == APPLY


def test_an_advert_without_a_distance_counts_as_signal_lost(device):
    device.apply_scanner_selection(_adv("kitchen"), 100, grace=60)
    device.apply_scanner_selection(_adv("kitchen", dist=None), 110, grace=60)
    assert device.room_hold_state == HOLD_GRACE
    assert device.area_advert is None


def test_hold_state_is_in_the_dump(device):
    device.apply_scanner_selection(_adv("kitchen"), 100, grace=60)
    device.apply_scanner_selection(None, 110, grace=60)
    out = device.to_dict()
    assert out["room_hold"] == {
        "pending_area_id": None,
        "pending_since": None,
        "lost_since": 110,
        "confirmed_at": 100,
    }
    assert out["room_hold_state"] == HOLD_GRACE
    assert out["area_explain"] is None

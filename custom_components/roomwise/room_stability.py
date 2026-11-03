"""
Room stability: dwell before a room change, and grace before "unknown".

The coordinator picks the best receiver for a device on every update. A weak
signal can make that pick flicker, or vanish for a while although the device
has not moved. This module decides whether a newly picked room is applied to
the device now, or whether the device keeps showing its current room a while
longer. It is pure logic (no Home Assistant imports) so it is easy to test.

Rules, per device:
- Same room as now (or still unknown): nothing to hold, apply it.
- No room picked (signal lost): keep the current room for `grace` seconds,
  then show unknown. A device with no current room has nothing to keep.
- A different room picked: keep the current room until the new room has been
  picked continuously for `dwell` seconds. Going from unknown to a room is
  always immediate. Switching back to the old room cancels the wait.
- A cap on both: the shown room is never held longer than max(dwell, grace)
  since the device was last actually picked in it, however the picks flicker.
A value of 0 for `dwell` or `grace` switches that rule off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

APPLY: Final = "apply"  # Use the newly picked room (or unknown) now.
HOLD_DWELL: Final = "hold_dwell"  # Keep the current room: new room has not been picked long enough.
HOLD_GRACE: Final = "hold_grace"  # Keep the current room: signal lost, still inside the grace period.


@dataclass
class RoomHold:
    """Per-device timers for the room hold rules."""

    pending_area_id: str | None = None  # Room waiting out its dwell time.
    pending_since: float | None = None  # When that room was first picked.
    lost_since: float | None = None  # When the signal was first lost.
    confirmed_at: float | None = None  # When the shown room was last actually picked.

    def reset(self) -> None:
        """Forget all waiting (confirmed_at is kept: it is about the shown room, not a wait)."""
        self.pending_area_id = None
        self.pending_since = None
        self.lost_since = None


def _signal_lost(hold: RoomHold, current_area_id: str | None, now: float, grace: float) -> str:
    """No room picked while one is shown: hold it for `grace` seconds."""
    hold.pending_area_id = None
    hold.pending_since = None
    if grace <= 0 or current_area_id is None:
        hold.lost_since = None
        return APPLY
    if hold.lost_since is None:
        hold.lost_since = now
    if now - hold.lost_since < grace:
        return HOLD_GRACE
    hold.lost_since = None
    return APPLY


def _room_changed(hold: RoomHold, current_area_id: str | None, new_area_id: str, now: float, dwell: float) -> str:
    """A different room picked: apply it once it has been picked for `dwell` seconds."""
    hold.lost_since = None
    if dwell <= 0 or current_area_id is None:
        hold.pending_area_id = None
        hold.pending_since = None
        return APPLY
    if hold.pending_area_id != new_area_id or hold.pending_since is None:
        hold.pending_area_id = new_area_id
        hold.pending_since = now
    if now - hold.pending_since < dwell:
        return HOLD_DWELL
    hold.reset()
    return APPLY


def _decide(
    hold: RoomHold,
    current_area_id: str | None,
    new_area_id: str | None,
    now: float,
    dwell: float,
    grace: float,
) -> str:
    if new_area_id == current_area_id:
        hold.reset()
        return APPLY
    if new_area_id is None:
        return _signal_lost(hold, current_area_id, now, grace)
    return _room_changed(hold, current_area_id, new_area_id, now, dwell)


def decide(
    hold: RoomHold,
    current_area_id: str | None,
    new_area_id: str | None,
    now: float,
    dwell: float,
    grace: float,
) -> str:
    """Return APPLY, HOLD_DWELL or HOLD_GRACE for the newly picked room, updating `hold`."""
    decision = _decide(hold, current_area_id, new_area_id, now, dwell, grace)
    if decision != APPLY and hold.confirmed_at is not None and now - hold.confirmed_at >= max(dwell, grace):
        # The shown room has gone unconfirmed as long as any hold may last. Flickering
        # picks restart each hold's own clock, so this cap is what ends the hold.
        hold.reset()
        decision = APPLY
    if decision == APPLY:
        # What is shown from now on is the pick itself (or unknown).
        hold.confirmed_at = now if new_area_id is not None else None
    return decision


def remaining(hold: RoomHold, decision: str | None, now: float, dwell: float, grace: float) -> float | None:
    """Seconds left on the hold for `decision`, or None when nothing is being held."""
    if decision == HOLD_DWELL and hold.pending_since is not None:
        left = dwell - (now - hold.pending_since)
    elif decision == HOLD_GRACE and hold.lost_since is not None:
        left = grace - (now - hold.lost_since)
    else:
        return None
    if hold.confirmed_at is not None:  # decide() also ends any hold at this cap
        left = min(left, max(dwell, grace) - (now - hold.confirmed_at))
    return max(0.0, left)

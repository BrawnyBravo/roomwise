"""
Plain-English "why this room" explainer for a tracked device.

Pure helpers: they read the device and the selected advert and return a
JSON-serialisable dict plus a short summary line. Nothing here touches Home
Assistant runtime state, so it is easy to test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from .const import AREA_MAX_AD_AGE

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_RECENT_SWITCHES: Final = 10
SUMMARY_MAX_LEN: Final = 255
DEFAULT_REASON: Final = "Kept current room"
NO_RECEIVER_REASON: Final = "No receiver in range"
ROOM_CHANGED_REASON: Final = "Receiver's room changed"

# The coordinator's internal win reasons, in words anyone can read.
_PLAIN_REASONS: Final[dict[str, str]] = {
    "WIN by not losing!": "Much closer than the current room",
    "WIN on historical min/max": "Consistently closer for several readings",
    "WIN awarded for same area, newer, closer advert": "Newer, closer reading in the same room",
    "WIN against an incumbent beyond its max radius": "Current room's receiver out of range",
    "WIN against no valid current receiver": "First receiver in range",
}

HOLD_DWELL: Final = "dwell"
HOLD_GRACE: Final = "grace"


def _round(value: Any) -> float | None:
    """Round a number to 2 places, passing None through."""
    if value is None:
        return None
    return round(float(value), 2)


def plain_reason(win_reason: str | None) -> str | None:
    """Turn an internal win reason into a short phrase; unknown texts pass through capitalised."""
    if win_reason is None:
        return None
    if win_reason in _PLAIN_REASONS:
        return _PLAIN_REASONS[win_reason]
    return win_reason[:1].upper() + win_reason[1:]


def _usable(advert: Any, nowstamp: float, range_limit: Callable[[Any], float]) -> bool:
    """True for an advert with a distance and a room, heard recently and inside its range limit."""
    if getattr(advert, "area_id", None) is None or getattr(advert, "rssi_distance", None) is None:
        return False
    stamp = getattr(advert, "stamp", None)
    if stamp is None or stamp < nowstamp - AREA_MAX_AD_AGE:
        return False
    return advert.rssi_distance <= range_limit(advert)


def find_in_room(device: Any, area_id: str | None, nowstamp: float, range_limit: Callable[[Any], float]) -> Any | None:
    """Return the closest fresh in-range advert in the given room, or None."""
    if area_id is None:
        return None
    best = None
    for advert in device.adverts.values():
        if advert.area_id != area_id or not _usable(advert, nowstamp, range_limit):
            continue
        if best is None or advert.rssi_distance < best.rssi_distance:
            best = advert
    return best


def find_runner_up(
    device: Any,
    winner: Any,
    nowstamp: float,
    range_limit: Callable[[Any], float],
    shown_area_id: str | None = None,
) -> Any | None:
    """
    Return the best fresh advert that points at a different room, or None.

    Only adverts with a distance and a room, heard recently and within the
    relay's own range limit, can be the runner-up. Neither the winner's room
    nor the shown room can be the runner-up's room.
    """
    winner_area = getattr(winner, "area_id", None) if winner is not None else None
    best = None
    for advert in device.adverts.values():
        if advert is winner or not _usable(advert, nowstamp, range_limit):
            continue
        area_id = advert.area_id
        if area_id in (winner_area, shown_area_id):
            continue
        distance = advert.rssi_distance
        if best is None or distance < best.rssi_distance:
            best = advert
    return best


def build_explain(
    device: Any,
    winner: Any,
    nowstamp: float,
    range_limit: Callable[[Any], float],
    reason: str | None,
    hold_state: str | None,
    hold_remaining: float | None,
    previous: dict[str, Any] | None,
    pending_room: str | None = None,
    now_iso: str | None = None,
    pending_relay: str | None = None,
    shown_area_id: str | None = None,
) -> dict[str, Any]:
    """
    Describe why the device is in its current room.

    `winner` is the receiver backing the shown room (None when there is none).
    While a room change waits out its dwell time, `pending_relay` names the
    receiver already picked for the new room.
    """
    room = device.area_name
    room_id = getattr(device, "area_id", None)
    runner = find_runner_up(device, winner, nowstamp, range_limit, shown_area_id)

    distance = getattr(winner, "rssi_distance", None) if winner is not None else None
    runner_distance = getattr(runner, "rssi_distance", None) if runner is not None else None
    margin = None
    if distance is not None and runner_distance is not None:
        margin = runner_distance - distance

    hold = hold_state if hold_state in (HOLD_DWELL, HOLD_GRACE) else None

    switches = list((previous or {}).get("recent_switches") or [])
    if previous is not None and previous.get("room_id") != room_id:
        switches.insert(
            0,
            {
                "at": now_iso or dt_util.now().isoformat(timespec="seconds"),
                "from": previous.get("room"),
                "to": room,
                "reason": reason or DEFAULT_REASON,
            },
        )
    switches = switches[:MAX_RECENT_SWITCHES]

    return {
        "room": room,
        "room_id": room_id,
        "relay": getattr(winner, "name", None) if winner is not None else None,
        "distance_m": _round(distance),
        "rssi": _round(getattr(winner, "rssi", None)) if winner is not None else None,
        "runner_up_relay": getattr(runner, "name", None) if runner is not None else None,
        "runner_up_room": getattr(runner, "area_name", None) if runner is not None else None,
        "runner_up_distance_m": _round(runner_distance),
        "runner_up_rssi": _round(getattr(runner, "rssi", None)) if runner is not None else None,
        "margin_m": _round(margin),
        "reason": reason or DEFAULT_REASON,
        "hold": hold,
        "hold_seconds_left": _round(hold_remaining) if hold is not None else None,
        "pending_room": pending_room if hold == HOLD_DWELL else None,
        "pending_relay": pending_relay if hold == HOLD_DWELL else None,
        "recent_switches": switches,
    }


def summary(explain: dict[str, Any]) -> str:
    """
    Return a one-line plain-English summary, at most 255 characters.

    It only holds what rarely changes (room, receiver, reason, hold kind, pending
    room). Distances, signal strength and countdowns live in the attributes, which
    the sensor refreshes only on a decision change or every 30 s (see sensor.py).
    """
    room = explain.get("room")
    hold = explain.get("hold")
    reason = explain.get("reason") or DEFAULT_REASON
    if room is None:
        text = f"Unknown. {reason}"
    else:
        text = room if explain.get("relay") is None else f"{room} via {explain['relay']}"
        if hold == HOLD_GRACE:
            text += ". Holding the room, no receiver in range"
        elif hold == HOLD_DWELL:
            pending = explain.get("pending_room")
            text += f". Waiting to move to {pending}" if pending else ". Waiting for a room change"
        else:
            text += f". {reason}"
    if len(text) > SUMMARY_MAX_LEN:
        text = text[: SUMMARY_MAX_LEN - 3] + "..."
    return text

"""Shared fake device for tests that drive the real area loop (`_refresh_area_by_min_distance`)."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.roomwise.room_stability import APPLY, RoomHold


def fake_device(adverts, area_advert=None, name="device", chosen=None, **fields) -> SimpleNamespace:
    """
    Return a stand-in device with the fields the area loop reads and writes.

    Its `apply_scanner_selection` accepts the dwell and grace arguments and always
    applies the pick at once: it records the first pick in `chosen["adv"]` (if given),
    and sets area_advert, area_id and area_name from the advert, like the real one.
    """
    device = SimpleNamespace(
        name=name,
        adverts=adverts,
        area_advert=area_advert,
        area_beyond_range=None,
        diag_area_switch=None,
        area_id=None,
        area_name=None,
        room_hold=RoomHold(),
        room_hold_state=APPLY,
        area_explain=None,
    )

    def apply(advert, *_args):
        if chosen is not None:
            chosen.setdefault("adv", advert)
        selected = advert if advert is not None and advert.rssi_distance is not None else None
        device.area_advert = selected
        device.area_id = selected.area_id if selected is not None else None
        device.area_name = selected.area_name if selected is not None else None
        device.room_hold_state = APPLY

    device.apply_scanner_selection = apply
    for key, value in fields.items():
        setattr(device, key, value)
    return device

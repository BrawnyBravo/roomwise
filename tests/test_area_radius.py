"""Max radius applies to the current room's receiver (the incumbent), not only to challengers."""

from __future__ import annotations

from types import SimpleNamespace

from bluetooth_data_tools import monotonic_time_coarse

from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

GLOBAL_RADIUS = 20.0


def _advert(scanner, area, distance, now):
    return SimpleNamespace(
        name=scanner,
        scanner_address=scanner,
        stamp=now,
        rssi_distance=distance,
        area_id=area,
        area_name=area,
        hist_distance_by_interval=[distance] * 5,
        scanner_device=SimpleNamespace(last_seen=now),
    )


def _select(adverts, incumbent_scanner=None, scanner_radius=None, max_radius=GLOBAL_RADIUS):
    """Run the real area loop once and return the scanner chosen (None = unplaced)."""
    chosen = {}
    device = SimpleNamespace(
        name="device",
        area_advert=adverts.get(incumbent_scanner),
        area_beyond_range=None,
        adverts=adverts,
        diag_area_switch=None,
        apply_scanner_selection=lambda adv: chosen.setdefault("adv", adv),
    )
    coord = SimpleNamespace(
        options={"max_area_radius": max_radius, "scanner_max_radius": dict(scanner_radius or {})},
        AreaTests=BermudaDataUpdateCoordinator.AreaTests,
    )
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    adv = chosen.get("adv")
    return None if adv is None else adv.scanner_address


def test_trimmed_receiver_cannot_hold_device_after_noise_dip():
    """A device that once dipped inside a trimmed receiver's range is released when it reads beyond it again."""
    now = monotonic_time_coarse()
    adverts = {"under": _advert("under", "Den", 5.0, now)}
    # The receiver's range was trimmed to 3 m; a noise dip made it the incumbent earlier.
    assert _select(adverts, incumbent_scanner="under", scanner_radius={"under": 3.0}) is None


def test_incumbent_beyond_global_radius_is_released():
    now = monotonic_time_coarse()
    adverts = {"a": _advert("a", "Kitchen", 9.0, now)}
    assert _select(adverts, incumbent_scanner="a", max_radius=6.0) is None


def test_out_of_range_incumbent_loses_to_nearby_valid_receiver_without_hysteresis():
    """Only 24% closer (below the 30% outright margin), but the incumbent is beyond its range and exit margin."""
    now = monotonic_time_coarse()
    adverts = {
        "under": _advert("under", "Den", 4.5, now),
        "kitchen": _advert("kitchen", "Kitchen", 3.55, now),
    }
    assert _select(adverts, incumbent_scanner="under", scanner_radius={"under": 3.0}) == "kitchen"


def test_hysteresis_unchanged_for_incumbent_within_range():
    """The same readings with the incumbent inside its range: the incumbent keeps the device."""
    now = monotonic_time_coarse()
    adverts = {
        "under": _advert("under", "Den", 4.0, now),
        "kitchen": _advert("kitchen", "Kitchen", 3.55, now),
    }
    assert _select(adverts, incumbent_scanner="under", scanner_radius={"under": 5.0}) == "under"
    assert _select(adverts, incumbent_scanner="under") == "under"


def test_incumbent_inside_trimmed_range_is_kept():
    now = monotonic_time_coarse()
    adverts = {"under": _advert("under", "Den", 2.5, now)}
    assert _select(adverts, incumbent_scanner="under", scanner_radius={"under": 3.0}) == "under"

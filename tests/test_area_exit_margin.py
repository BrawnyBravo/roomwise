"""
Exit margin for the current room's receiver (stage-D3 finding N-1).

A challenger needs to be within the max radius to win a room, but the
receiver that already holds the device (the incumbent) only loses it when it
reads beyond the radius by a clear margin (AREA_EXIT_RADIUS_FACTOR), or has
stayed beyond the plain radius for AREA_EXIT_SECONDS. Without that margin a
device whose readings sit near the edge flickers between the room and
"unplaced" hundreds of times an hour.

The noisy-readings tests feed raw rssi through the integration's own smoothing
(BermudaAdvert.calculate_data) and the real area loop, one update cycle per
second, on a fake clock.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from custom_components.roomwise import bermuda_advert, coordinator
from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.const import AREA_EXIT_RADIUS_FACTOR, AREA_EXIT_SECONDS
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator
from custom_components.roomwise.util import rssi_to_metres

REF_POWER = -55.0
ATTENUATION = 3.0
RADIUS = 6.0  # metres; about -78.3 dBm at these settings
ADVERT_EVERY = 2  # seconds between adverts from the device
SMOOTHING = 20  # the integration's default smoothing_samples
MAX_VELOCITY = 3  # the integration's default max_velocity


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clk = Clock()
    monkeypatch.setattr(coordinator, "monotonic_time_coarse", clk)
    monkeypatch.setattr(bermuda_advert, "monotonic_time_coarse", clk)
    return clk


def _new_advert(scanner: str, area: str) -> SimpleNamespace:
    """The attributes BermudaAdvert.calculate_data and the area loop use."""
    return SimpleNamespace(
        name=scanner,
        scanner_address=scanner,
        area_id=area,
        area_name=area,
        scanner_device=SimpleNamespace(last_seen=0.0),
        _device=SimpleNamespace(create_sensor=False, name="device"),
        conf_max_velocity=MAX_VELOCITY,
        conf_smoothing_samples=SMOOTHING,
        stamp=0.0,
        new_stamp=None,
        rssi=None,
        rssi_distance=None,
        rssi_distance_raw=None,
        hist_stamp=[],
        hist_rssi=[],
        hist_distance=[],
        hist_distance_by_interval=[],
        hist_interval=[],
        hist_velocity=[],
    )


def _hear(advert: SimpleNamespace, rssi: float, now: float) -> None:
    """What update_advertisement records for a new advert."""
    advert.rssi = rssi
    advert.hist_rssi.insert(0, rssi)
    dist = rssi_to_metres(rssi, REF_POWER, ATTENUATION)
    advert.rssi_distance_raw = dist
    advert.hist_distance.insert(0, dist)
    advert.hist_interval.insert(0, now - advert.stamp)
    advert.stamp = now
    advert.hist_stamp.insert(0, now)
    advert.new_stamp = now
    advert.scanner_device.last_seen = now


def _new_device(adverts: dict) -> SimpleNamespace:
    device = SimpleNamespace(name="device", adverts=adverts, area_advert=None, diag_area_switch=None)
    device.area_beyond_range = None

    def apply(adv):
        device.area_advert = adv if adv is not None and adv.rssi_distance is not None else None

    device.apply_scanner_selection = apply
    return device


def _coord(scanner_radius=None) -> SimpleNamespace:
    return SimpleNamespace(
        options={"max_area_radius": RADIUS, "scanner_max_radius": dict(scanner_radius or {})},
        AreaTests=BermudaDataUpdateCoordinator.AreaTests,
    )


def _run(clock: Clock, rssi_at, seconds: int, seed: int, sd: float, device=None, advert=None):
    """
    Run `seconds` update cycles. rssi_at(t) gives the true median at second t.

    Returns (device, advert, placed-per-second list).
    """
    rnd = random.Random(seed)
    if advert is None:
        advert = _new_advert("rx", "Room")
    if device is None:
        device = _new_device({"rx": advert})
    coord = _coord()
    placed = []
    for t in range(seconds):
        clock.now += 1.0
        if t % ADVERT_EVERY == 0:
            _hear(advert, rnd.gauss(rssi_at(t), sd), clock.now)
        BermudaAdvert.calculate_data(advert)
        BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
        placed.append(device.area_advert is not None)
    return device, advert, placed


def _longest_run(placed: list[bool]) -> int:
    best = run = 0
    for p in placed:
        run = run + 1 if p else 0
        best = max(best, run)
    return best


def _changes(placed: list[bool]) -> int:
    return sum(a != b for a, b in zip(placed, placed[1:], strict=False))


@pytest.mark.parametrize("sd", [3.0, 5.0])
def test_noisy_device_near_the_edge_stays_in_its_room(clock, sd):
    """
    The stage-D3 case: 6 m radius, ref -55, attenuation 3, readings with a median of -80 dBm.

    With the old instant cut-off this flipped between the room and unplaced
    about 180-290 times an hour. With the exit margin it should hardly move.
    """
    total_changes = 0
    total_unplaced = 0
    seeds = range(3)
    for seed in seeds:
        _, _, placed = _run(clock, lambda _t: -80.0, 3600, seed, sd)
        first_in = placed.index(True)  # it starts unplaced until the first reading inside the radius
        total_changes += _changes(placed[first_in:])
        total_unplaced += placed[first_in:].count(False)
    per_hour = total_changes / len(seeds)
    assert per_hour <= 8, f"{per_hour:.1f} room changes an hour at sd {sd}"
    assert total_unplaced / (3600 * len(seeds)) < 0.01


def test_device_well_inside_is_never_dropped(clock):
    _, _, placed = _run(clock, lambda _t: -74.0, 3600, seed=11, sd=5.0)
    assert _changes(placed[placed.index(True) :]) == 0


def test_device_that_leaves_is_released_promptly(clock):
    """Walk out of range (median -65 dBm, then -90 dBm): released within 20 seconds."""
    for seed in range(5):
        _, _, placed = _run(clock, lambda t: -65.0 if t < 300 else -90.0, 420, seed, sd=4.0)
        assert all(placed[60:300])
        released = next(t for t in range(300, 420) if not placed[t])
        assert released - 300 <= 20, f"seed {seed}: released after {released - 300} s"
        # A rare deep noise dip can still capture it again for a few seconds (the
        # smoothing keeps a dip for up to 20 samples, as before); it must not stick.
        assert _longest_run(placed[released:]) <= 30, f"seed {seed}: kept again after release"


def test_device_that_moves_just_past_the_edge_is_released(clock):
    """A move to about 1.8x the radius (median -86 dBm) still releases within about a minute."""
    for seed in range(5):
        _, _, placed = _run(clock, lambda t: -65.0 if t < 300 else -86.0, 600, seed, sd=4.0)
        released = next((t for t in range(300, 600) if not placed[t]), None)
        assert released is not None, f"seed {seed}: never released"
        assert released - 300 <= 70, f"seed {seed}: released after {released - 300} s"


def test_incumbent_held_inside_the_exit_margin(clock):
    """Steady reading a little beyond the radius but inside the exit margin: kept until the time limit."""
    advert = _new_advert("rx", "Room")
    device = _new_device({"rx": advert})
    coord = _coord()
    dist_held = RADIUS * (1 + (AREA_EXIT_RADIUS_FACTOR - 1) / 2)
    advert.rssi_distance = 5.0
    advert.hist_distance_by_interval = [5.0] * 5
    advert.stamp = clock.now
    advert.scanner_device.last_seen = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is advert
    advert.rssi_distance = dist_held
    for _ in range(int(AREA_EXIT_SECONDS) - 1):
        clock.now += 1.0
        advert.stamp = clock.now
        BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
        assert device.area_advert is advert
    clock.now += 2.0
    advert.stamp = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is None


def test_incumbent_beyond_exit_margin_is_released_at_once(clock):
    advert = _new_advert("rx", "Room")
    device = _new_device({"rx": advert})
    coord = _coord()
    advert.rssi_distance = 5.0
    advert.hist_distance_by_interval = [5.0] * 5
    advert.stamp = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is advert
    advert.rssi_distance = RADIUS * AREA_EXIT_RADIUS_FACTOR * 1.01
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is None


def test_challenger_still_needs_the_plain_radius(clock):
    """The margin is only for the receiver that already holds the device."""
    advert = _new_advert("rx", "Room")
    device = _new_device({"rx": advert})
    advert.rssi_distance = RADIUS * 1.1
    advert.hist_distance_by_interval = [RADIUS * 1.1] * 5
    advert.stamp = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(_coord(), device)  # noqa: SLF001
    assert device.area_advert is None


def test_held_incumbent_gives_way_to_any_receiver_in_range(clock):
    """Kept only to avoid flapping to unplaced: a valid nearby receiver takes over without the 30% margin."""
    held = _new_advert("held", "Den")
    near = _new_advert("near", "Kitchen")
    device = _new_device({"held": held, "near": near})
    for adv, dist in ((held, 2.5), (near, 9.0)):
        adv.rssi_distance = dist
        adv.hist_distance_by_interval = [dist] * 5
        adv.stamp = clock.now
        adv.scanner_device.last_seen = clock.now
    coord = _coord(scanner_radius={"held": 3.0})
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is held
    # Den's receiver now reads 3.3 m (beyond its 3 m trim, inside the exit margin);
    # Kitchen's reads 3.0 m: only 10% closer, normally not enough to switch.
    held.rssi_distance = 3.3
    near.rssi_distance = 3.0
    near.hist_distance_by_interval = [3.0] * 5
    clock.now += 1.0
    held.stamp = near.stamp = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    assert device.area_advert is near


def test_exit_timer_restarts_when_back_in_range(clock):
    advert = _new_advert("rx", "Room")
    device = _new_device({"rx": advert})
    coord = _coord()
    advert.rssi_distance = 5.0
    advert.hist_distance_by_interval = [5.0] * 5
    advert.stamp = clock.now
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
    for step in range(int(AREA_EXIT_SECONDS) * 3):
        clock.now += 1.0
        advert.stamp = clock.now
        # beyond the radius for most of each 10 s, back inside for one cycle
        advert.rssi_distance = 5.5 if step % 10 == 0 else RADIUS * 1.1
        BermudaDataUpdateCoordinator._refresh_area_by_min_distance(coord, device)  # noqa: SLF001
        assert device.area_advert is advert

"""Tests for the pure room auto-tuner (no Home Assistant needed for most of these)."""

from __future__ import annotations

import math
import random
from types import SimpleNamespace

import pytest
from bluetooth_data_tools import monotonic_time_coarse

from custom_components.roomwise import autotune as at
from custom_components.roomwise import util
from custom_components.roomwise.bermuda_advert import BermudaAdvert
from custom_components.roomwise.const import AREA_EXIT_RADIUS_FACTOR
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

# A synthetic single-storey house: four rooms on a 6 m grid, one scanner in each.
ROOMS = {"Kitchen": (0.0, 0.0), "Office": (6.0, 0.0), "Lounge": (0.0, 6.0), "Bedroom": (6.0, 6.0)}
SCANNERS = [at.Scanner(f"scanner_{room.lower()}", room, f"{room} proxy") for room in ROOMS]
DEFAULT = at.Settings(ref_power=-55.0, attenuation=3.0, max_radius=20.0, offsets={})


def make_house(gains: dict[str, float], per_room: int = 3, seed: int = 1, spread: float = 2.0, noise: float = 1.5):
    """Landmarks spread around each room; samples from a log-distance model plus per-scanner gain error."""
    rnd = random.Random(seed)
    landmarks: list[at.Landmark] = []
    samples: list[at.Sample] = []
    for room, (x, y) in ROOMS.items():
        for i in range(per_room):
            lx, ly = x + rnd.uniform(-spread, spread), y + rnd.uniform(-spread, spread)
            lm = at.Landmark(f"{room.lower()}_{i}", room, f"{room} thing {i}")
            landmarks.append(lm)
            for sc in SCANNERS:
                sx, sy = ROOMS[sc.area]
                dist = max(0.5, math.hypot(lx - sx, ly - sy))
                base = -55 - 30 * math.log10(dist) + gains.get(sc.id, 0)
                for k in range(12):
                    samples.append(at.Sample(lm.id, sc.id, round(base + rnd.gauss(0, noise)), float(k)))
    return landmarks, samples


# --------------------------------------------------------------------------
# Formula and room-rule parity with the integration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rssi", [-100, -85, -70.5, -55, -40, -20])
@pytest.mark.parametrize("ref_power", [-70, -55, -45.5])
@pytest.mark.parametrize("attenuation", [1.5, 2, 3, 4.2])
def test_formula_matches_util(rssi, ref_power, attenuation):
    assert at.rssi_to_metres(rssi, ref_power, attenuation) == pytest.approx(
        util.rssi_to_metres(rssi, ref_power, attenuation), rel=1e-12
    )


@pytest.mark.parametrize("offset", [-12, 0, 7])
def test_distance_matches_advert_raw_distance(offset):
    """estimated_distance must equal BermudaAdvert._update_raw_distance (offset, global ref_power)."""
    settings = at.Settings(-58.0, 2.7, 10.0, {"scn": offset})
    fake = SimpleNamespace(
        ref_power=0,  # no per-device ref_power, so the global one is used
        conf_ref_power=settings.ref_power,
        conf_attenuation=settings.attenuation,
        conf_rssi_offset=offset,
        rssi=-73,
        hist_distance=[],
        rssi_distance=None,
    )
    expected = BermudaAdvert._update_raw_distance(fake, reading_is_new=True)
    assert at.estimated_distance(settings, "scn", -73) == pytest.approx(expected, rel=1e-12)


def _run_coordinator_area_rule(
    heard: dict[str, float],
    scanners: dict[str, at.Scanner],
    settings: at.Settings,
    incumbent: str | None = None,
):
    """Run the real coordinator area loop once on fake adverts, from no incumbent unless one is given."""
    now = monotonic_time_coarse()
    adverts = {}
    for sid, rssi in heard.items():
        dist = at.estimated_distance(settings, sid, rssi)
        adverts[sid] = SimpleNamespace(
            name=sid,
            scanner_address=sid,
            stamp=now,
            rssi_distance=dist,
            area_id=scanners[sid].area,
            area_name=scanners[sid].area,
            hist_distance_by_interval=[dist] * 5,
            scanner_device=SimpleNamespace(last_seen=now),
        )
    chosen = {}
    device = SimpleNamespace(
        name="landmark",
        area_advert=adverts.get(incumbent) if incumbent else None,
        area_beyond_range=None,
        adverts=adverts,
        diag_area_switch=None,
        apply_scanner_selection=lambda adv: chosen.setdefault("adv", adv),
    )
    fake_coord = SimpleNamespace(
        options={"max_area_radius": settings.max_radius, "scanner_max_radius": dict(settings.radii)},
        AreaTests=BermudaDataUpdateCoordinator.AreaTests,
    )
    BermudaDataUpdateCoordinator._refresh_area_by_min_distance(fake_coord, device)
    adv = chosen.get("adv")
    return None if adv is None else adv.area_id


def test_predict_area_matches_coordinator():
    """On readings that are clearly apart (beyond hysteresis), our rule picks what the coordinator picks."""
    rnd = random.Random(7)
    scanners = {s.id: s for s in SCANNERS}
    checked = 0
    for _ in range(300):
        settings = at.Settings(-55.0, 3.0, rnd.choice([4.0, 6.0, 20.0]), {s: rnd.randint(-6, 6) for s in scanners})
        if rnd.random() < 0.5:  # sometimes a per-scanner max radius, as auto-tune may set
            settings.radii = {rnd.choice(sorted(scanners)): rnd.choice([1.5, 3.0, 5.0])}
        # distinct adjusted rssi, at least 3 dB apart, so hysteresis cannot decide differently
        levels = rnd.sample(range(-95, -45, 3), k=rnd.randint(1, 4))
        ids = rnd.sample(sorted(scanners), k=len(levels))
        heard = {sid: lvl - settings.offset(sid) for sid, lvl in zip(ids, levels)}
        ours, _ = at.predict_area(heard, scanners, settings)
        theirs = _run_coordinator_area_rule(heard, scanners, settings)
        assert ours == theirs, (heard, settings)
        checked += 1
    assert checked == 300


def test_exit_factor_matches_coordinator():
    assert at.COORDINATOR_EXIT_RADIUS_FACTOR == AREA_EXIT_RADIUS_FACTOR


def test_holding_area_matches_coordinator_exit_margin():
    """
    holding_area is None exactly when the coordinator leaves the device unplaced
    whichever receiver held it before (within the exit time limit).
    """
    rnd = random.Random(11)
    scanners = {s.id: s for s in SCANNERS}
    held_somewhere = released_everywhere = 0
    for _ in range(300):
        settings = at.Settings(-55.0, 3.0, rnd.choice([4.0, 6.0, 20.0]), {s: rnd.randint(-6, 6) for s in scanners})
        if rnd.random() < 0.5:
            settings.radii = {rnd.choice(sorted(scanners)): rnd.choice([1.5, 3.0, 5.0])}
        # Readings mostly beyond the plain radius, some inside the exit margin,
        # kept off the exact boundaries.
        heard = {}
        for sid in rnd.sample(sorted(scanners), k=rnd.randint(1, 4)):
            ratio = rnd.choice([0.5, 1.1, 1.2, 1.4, 2.0, 3.0])
            dist = settings.radius(sid) * ratio
            heard[sid] = settings.ref_power - 10 * settings.attenuation * math.log10(dist) - settings.offset(sid)
        outcomes = [_run_coordinator_area_rule(heard, scanners, settings, incumbent=sid) for sid in heard]
        outcomes.append(_run_coordinator_area_rule(heard, scanners, settings))
        if at.holding_area(heard, scanners, settings) is None:
            assert all(o is None for o in outcomes), (heard, settings)
            released_everywhere += 1
        else:
            assert any(o is not None for o in outcomes), (heard, settings)
            held_somewhere += 1
    assert held_somewhere > 50
    assert released_everywhere > 50


def test_holding_area_counts_the_exit_margin():
    """Beyond the radius but inside the margin: may be kept (counts as misplaced); beyond the margin: unplaced."""
    scanners = {"a": at.Scanner("a", "Kitchen")}
    settings = at.Settings(-55.0, 3.0, 6.0, {})
    cut = settings.rssi_cutoff()
    margin = 10 * 3.0 * math.log10(at.COORDINATOR_EXIT_RADIUS_FACTOR)
    assert at.predict_area({"a": cut - 1}, scanners, settings)[0] is None  # cannot enter
    assert at.holding_area({"a": cut - 1}, scanners, settings) == "Kitchen"  # but may be kept
    assert at.holding_area({"a": cut - margin - 0.1}, scanners, settings) is None


def test_margin_matches_coordinator_hysteresis():
    # pdiff 0.30 -> distance ratio 1.353 -> about 3.9 dB at attenuation 3
    assert at.margin_db(3.0) == pytest.approx(3.94, abs=0.02)


def test_cutoff_is_max_radius():
    s = at.Settings(-55.0, 3.0, 6.0, {})
    assert at.rssi_to_metres(s.rssi_cutoff(), s.ref_power, s.attenuation) == pytest.approx(6.0)


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def test_aggregate_median_and_min_samples():
    samples = [at.Sample("lm", "a", r, t) for t, r in enumerate([-60, -61, -90, -59, -60])]
    samples += [at.Sample("lm", "b", -70, 0), at.Sample("lm", "b", -71, 1)]  # too few
    med = at.aggregate(samples, min_samples=5)
    assert med == {"lm": {"a": -60.0}}  # the -90 outlier does not move the median


def test_aggregate_window():
    samples = [at.Sample("lm", "a", -50, t) for t in range(5)] + [at.Sample("lm", "a", -80, t) for t in range(10, 20)]
    assert at.aggregate(samples, min_samples=5, window=(10, 19)) == {"lm": {"a": -80.0}}


# --------------------------------------------------------------------------
# Tuning behaviour
# --------------------------------------------------------------------------


def test_hot_scanner_that_steals_rooms_gets_fixed():
    """One proxy reads 12 dB too strong and pulls neighbours' devices into its room."""
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=4, seed=3, spread=3.0)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT)
    assert report.scored == 16
    assert report.right_before < report.scored  # the hot scanner really is stealing rooms
    assert report.right_after > report.right_before
    assert report.recommend
    # The fix is to turn the hot scanner down relative to the others.
    others = [report.proposed.offset(s.id) for s in SCANNERS if s.id != "scanner_office"]
    assert report.proposed.offset("scanner_office") < min(others)
    assert f"{report.right_after} of 16 known devices land in the right room (was {report.right_before} of 16)" in (
        report.summary
    )
    assert report.fixes
    # Every offset stays within the bound.
    assert all(abs(v) <= at.DEFAULT_OFFSET_LIMIT for v in report.proposed.offsets.values())


def test_good_settings_are_left_alone():
    landmarks, samples = make_house({}, per_room=3, seed=5, spread=1.5)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT)
    assert report.right_before == report.scored == 12
    assert not report.recommend
    assert "already as good as we can get" in report.summary
    assert report.proposed.offsets == {} or all(v == 0 for v in report.proposed.offsets.values())


def test_leave_one_out_never_tunes_on_the_scored_landmark():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=3, seed=3)
    calls: list[set[str]] = []

    def spy_fit(medians, train, scanners, current, options):
        train = list(train)
        calls.append({lm.id for lm in train})
        assert set(medians) <= {lm.id for lm in train}  # no data from anyone else leaks in
        return at.fit(medians, train, scanners, current, options)

    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT, fit_func=spy_fit)
    all_ids = {lm.id for lm in landmarks}
    assert calls[0] == all_ids  # the final proposal uses everyone
    loo_calls = calls[1:]
    assert len(loo_calls) == report.scored
    for lm, used in zip([r.landmark for r in report.results if r.scored], loo_calls):
        assert lm.id not in used
        assert used == all_ids - {lm.id}


def test_one_odd_landmark_cannot_justify_a_change():
    """
    Only one landmark is wrong and nothing else supports moving any offset.
    Fitting on it would 'fix' it in-sample, but leave-one-out must not count that.
    """
    scanners = [at.Scanner("a", "Kitchen"), at.Scanner("b", "Office")]
    medians = {
        "k1": {"a": -55, "b": -75},
        "k2": {"a": -57, "b": -74},
        "o1": {"a": -76, "b": -56},
        "o2": {"a": -73, "b": -58},
        "stray": {"a": -66, "b": -60},  # in the Kitchen, but Office hears it better
    }
    landmarks = [at.Landmark(i, "Office" if i.startswith("o") else "Kitchen") for i in medians]
    report = at.evaluate(landmarks, scanners, None, DEFAULT, medians=medians)
    assert report.right_before == 4
    # In-sample, a fit would fix 'stray' by pushing a up / b down:
    in_sample = at.fit(medians, landmarks, scanners, DEFAULT)
    assert at.predict_area(medians["stray"], {s.id: s for s in scanners}, in_sample)[0] == "Kitchen"
    # ...but scored fairly it does not improve, so nothing is recommended.
    odd = next(r for r in report.results if r.landmark.id == "stray")
    assert not odd.after_ok
    assert report.right_after <= report.right_before
    assert not report.recommend


def test_landmarks_in_rooms_without_a_scanner_are_listed_not_scored():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=3, seed=3)
    extra = at.Landmark("hall_lamp", "Hallway", "Hall lamp")
    samples += [at.Sample("hall_lamp", "scanner_kitchen", -65, t) for t in range(10)]
    seen_training: list[set[str]] = []

    def spy_fit(medians, train, scanners, current, options):
        train = list(train)
        seen_training.append({lm.id for lm in train})
        return at.fit(medians, train, scanners, current, options)

    no_radius = at.TuneOptions(tune_radius=False)
    report = at.evaluate([*landmarks, extra], SCANNERS, samples, DEFAULT, no_radius, fit_func=spy_fit)
    res = next(r for r in report.results if r.landmark.id == "hall_lamp")
    assert not res.scored
    assert res.reason == "no scanner in this room"
    assert report.scored == len(landmarks)
    assert all("hall_lamp" not in s for s in seen_training)
    assert "1 other device could not be checked (no receiver in the room)." in report.summary
    assert res.outsider
    assert report.per_area()["Hallway"]["scored"] == 0


def test_unheard_and_roomless_landmarks_are_not_scored():
    landmarks, samples = make_house({}, per_room=3, seed=5)
    report = at.evaluate(
        [*landmarks, at.Landmark("ghost", "Kitchen"), at.Landmark("nowhere", None)], SCANNERS, samples, DEFAULT
    )
    reasons = {r.landmark.id: r.reason for r in report.results if not r.scored}
    assert reasons == {"ghost": "not heard (or too few readings)", "nowhere": "no room set"}


def test_too_few_landmarks_means_no_recommendation():
    scanners = [at.Scanner("a", "Kitchen"), at.Scanner("b", "Office")]
    medians = {"k1": {"a": -70, "b": -60}, "o1": {"a": -75, "b": -58}}
    landmarks = [at.Landmark("k1", "Kitchen"), at.Landmark("o1", "Office")]
    report = at.evaluate(landmarks, scanners, None, DEFAULT, medians=medians)
    assert not report.recommend
    assert report.summary.startswith("Only 2 known devices could be checked")


def test_offsets_stay_bounded_even_for_a_wildly_hot_scanner():
    landmarks, samples = make_house({"scanner_office": 40.0}, per_room=3, seed=3)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT)
    for value in at.fit(at.aggregate(samples), landmarks, SCANNERS, DEFAULT).offsets.values():
        assert -15 <= value <= 15
    assert all(-15 <= v <= 15 for v in report.proposed.offsets.values())


def test_current_offset_outside_bound_is_still_allowed():
    s = at.Settings(-55.0, 3.0, 20.0, {"scanner_kitchen": 22})
    landmarks, samples = make_house({"scanner_kitchen": -22.0}, per_room=2, seed=9)
    fitted = at.fit(at.aggregate(samples), landmarks, SCANNERS, s)
    assert -15 <= fitted.offset("scanner_kitchen") <= 22


def test_out_of_radius_devices_pull_offsets_up():
    """Small max_radius: quiet devices fall outside it and get no room."""
    settings = at.Settings(-55.0, 3.0, 2.0, {})
    landmarks, samples = make_house({}, per_room=3, seed=11, spread=2.0)
    report = at.evaluate(landmarks, SCANNERS, samples, settings)
    assert report.right_before < report.scored  # some own-room readings fall outside the 2 m radius
    assert report.recommend
    assert report.right_after > report.right_before
    assert sum(report.proposed.offsets.values()) > 0


def test_ref_power_only_tuned_when_asked():
    settings = at.Settings(-55.0, 3.0, 2.0, {})
    landmarks, samples = make_house({}, per_room=3, seed=11, spread=2.0)
    plain = at.fit(at.aggregate(samples), landmarks, SCANNERS, settings)
    assert plain.ref_power == settings.ref_power
    opts = at.TuneOptions(tune_ref_power=True)
    tuned = at.fit(at.aggregate(samples), landmarks, SCANNERS, settings, opts)
    assert abs(tuned.ref_power - settings.ref_power) <= opts.ref_power_limit


def test_format_table_lists_rooms_and_offsets():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=4, seed=3, spread=3.0)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT)
    table = at.format_table(report, {s.id: s.name for s in SCANNERS})
    assert "| Kitchen |" in table
    assert "| Office proxy |" in table


def test_export_round_trip_replays_the_same():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=4, seed=3, spread=3.0)
    current = at.Settings(-55.0, 3.0, 20.0, {"scanner_kitchen": 0})
    data = at.export_dict(landmarks, SCANNERS, current, samples, {"Kitchen": "Kitchen"})
    import json

    lms, scs, settings, smp = at.load_export(json.loads(json.dumps(data)))
    original = at.evaluate(landmarks, SCANNERS, samples, current)
    replayed = at.evaluate(lms, scs, smp, settings)
    assert (replayed.right_before, replayed.right_after, replayed.recommend) == (
        original.right_before,
        original.right_after,
        original.recommend,
    )
    assert replayed.proposed.offsets == original.proposed.offsets


def test_load_export_rejects_other_files():
    with pytest.raises(ValueError):
        at.load_export({"format": "something-else"})


# --------------------------------------------------------------------------
# A receiver directly under a room that has none (e.g. a basement receiver under a study)
# --------------------------------------------------------------------------


def _under_study_house(study_level: float):
    """
    Three receivers: Basement, Kitchen, Hall. The Study sits right above the
    Basement and has no receiver; the Basement receiver hears study devices at
    about `study_level` dBm. Basement devices themselves are heard at about -60.
    """
    scanners = [
        at.Scanner("b", "Basement", "Basement"),
        at.Scanner("k", "Kitchen", "Kitchen"),
        at.Scanner("h", "Hall", "Hall"),
    ]
    medians = {
        "basement_1": {"b": -58, "k": -85, "h": -80},
        "basement_2": {"b": -62, "k": -88, "h": -79},
        "basement_3": {"b": -60, "k": -84},
        "kitchen_1": {"k": -55, "b": -82, "h": -75},
        "kitchen_2": {"k": -60, "b": -84, "h": -72},
        "hall_1": {"h": -57, "k": -74, "b": -80},
        "hall_2": {"h": -61, "k": -76, "b": -78},
        "st_1": {"b": study_level, "h": -86},
        "st_2": {"b": study_level - 3, "h": -84},
        "st_3": {"b": study_level - 1, "k": -89},
    }
    rooms = {"b": "Basement", "k": "Kitchen", "h": "Hall", "s": "Study"}
    landmarks = [at.Landmark(i, rooms[i[0]], i) for i in medians]
    return scanners, medians, landmarks


def test_receiver_under_a_room_without_one_is_explained_and_range_trimmed():
    """Study devices weaker than the basement's own: a smaller basement radius leaves them unplaced."""
    scanners, medians, landmarks = _under_study_house(-74)
    settings = at.Settings(-55.0, 3.0, 20.0, {})
    report = at.evaluate(landmarks, scanners, None, settings, medians=medians)
    assert report.outsiders == 3
    assert report.misplaced_before == 3  # all three study devices show up as Basement today
    assert report.right_before == report.scored == 7
    assert report.right_after == 7  # never at the cost of a device in a room with a receiver
    assert report.misplaced_after < report.misplaced_before
    assert report.recommend
    assert report.proposed.radius("b") < 20.0
    # Trims are gentle. The basement needs 3 m: study devices read about 4.3 m
    # from it, and the receiver holding a device keeps it out to max radius x
    # the exit margin (1.3), so 4 m would still let it hold them once captured.
    assert report.proposed.radius("b") >= 3.0
    assert all(report.proposed.radius(s) >= 4.0 for s in ("k", "h"))
    # The trimmed radius still keeps every basement device in the basement.
    scanner_map = {s.id: s for s in scanners}
    for lm in landmarks:
        if lm.area == "Basement":
            assert at.predict_area(medians[lm.id], scanner_map, report.proposed)[0] == "Basement"


def test_summary_counts_each_unchecked_device_once():
    """Devices in rooms without a receiver get their own sentence; "other devices" must not count them again."""
    scanners, medians, landmarks = _under_study_house(-74)
    settings = at.Settings(-55.0, 3.0, 20.0, {})
    report = at.evaluate(landmarks, scanners, None, settings, medians=medians)
    assert report.recommend
    assert "Of 3 devices in rooms without a receiver" in report.summary
    assert "could not be checked" not in report.summary  # every chosen device is already accounted for

    extra = [at.Landmark("ghost", "Kitchen", "ghost"), at.Landmark("nowhere", None, "nowhere")]
    report = at.evaluate([*landmarks, *extra], scanners, None, settings, medians=medians)
    assert "Of 3 devices in rooms without a receiver" in report.summary
    assert "2 other devices could not be checked (no room set or not heard)." in report.summary


def test_receiver_under_a_louder_room_says_only_a_new_receiver_helps():
    """Study devices heard MORE strongly than the basement's own devices: nothing can separate them."""
    scanners, medians, landmarks = _under_study_house(-51)
    settings = at.Settings(-55.0, 3.0, 6.0, {})
    report = at.evaluate(landmarks, scanners, None, settings, medians=medians, area_names={"Study": "Study"})
    assert report.misplaced_before == 3
    assert report.right_after >= report.right_before  # basement devices never sacrificed
    assert report.misplaced_after == 3  # no radius keeps basement devices and drops study ones
    note = next(n for n in report.notes if n.startswith("Devices in Study"))
    assert "heard best by the Basement receiver in Basement" in note
    assert "Add a receiver in Study, or lower that receiver's max radius" in note
    assert "no setting can tell them apart" in note
    table = at.format_table(report)
    assert "| Basement |" in table
    assert "Devices in Study" in table


def test_radius_tuning_can_be_switched_off():
    scanners, medians, landmarks = _under_study_house(-74)
    report = at.evaluate(
        landmarks,
        scanners,
        None,
        at.Settings(-55.0, 3.0, 20.0, {}),
        at.TuneOptions(tune_radius=False),
        medians=medians,
    )
    assert report.proposed.radii == {}
    assert not report.recommend


def test_receiver_with_no_own_room_evidence_is_never_trimmed():
    """If no device in the receiver's own room is heard by it, nothing protects that room: leave its range alone."""
    scanners, medians, landmarks = _under_study_house(-74)
    for lm_id in ("basement_1", "basement_2", "basement_3"):
        medians[lm_id] = {k: v for k, v in medians[lm_id].items() if k != "b"}
    report = at.evaluate(landmarks, scanners, None, at.Settings(-55.0, 3.0, 20.0, {}), medians=medians)
    assert report.proposed.radius("b") == 20.0
    assert any("was never heard by the receiver in its own room" in n for n in report.notes)


def test_trimmed_radius_never_below_floor():
    scanners, medians, landmarks = _under_study_house(-74)
    fitted = at.fit(medians, landmarks, scanners, at.Settings(-55.0, 3.0, 20.0, {}))
    assert all(r >= min(at.RADIUS_STEPS) for r in fitted.radii.values())


def test_notes_never_blame_a_receiver_without_a_room():
    """A misplaced device is explained by the receiver that holds it, not a louder one with no room."""
    scanners = {
        "attic": at.Scanner("attic", None, "Attic proxy"),  # no room, hears it loudest
        "kitchen": at.Scanner("kitchen", "Kitchen", "Kitchen proxy"),
    }
    settings = at.Settings(ref_power=-55, attenuation=3, max_radius=20)
    result = at.LandmarkResult(
        landmark=at.Landmark("dev1", "Pantry", "Test Tag"),
        scored=True,
        reason="",
        after_ok=False,
        heard_by={"attic": -50.0, "kitchen": -65.0},
        outsider=True,
    )
    report = at.TuneReport(
        current=settings,
        proposed=settings,
        results=[result],
        scored=1,
        right_before=0,
        right_after=0,
        recommend=False,
        summary="",
    )
    notes = at.explain(report, scanners, settings, {"Kitchen": "Kitchen", "Pantry": "Pantry"})
    text = " ".join(notes)
    assert "Kitchen proxy" in text
    assert "Attic proxy" not in text
    assert "(no room)" not in text

"""
Room auto-tuning for Roomwise ("rooms that tune themselves").

This module is deliberately pure Python with no Home Assistant imports, so the
exact same code can be replayed offline against captured data.

How Roomwise picks a room (see coordinator._refresh_area_by_min_distance):

    distance(device, scanner) = 10 ** ((ref_power - (rssi + offset[scanner])) / (10 * attenuation))

    The device goes to the area of the nearest scanner that has an area and whose
    distance is within that scanner's max radius (plus hysteresis when switching).
    The scanner that already holds the device keeps it until the device reads
    beyond max radius x COORDINATOR_EXIT_RADIUS_FACTOR (or stays beyond the
    plain max radius for 30 s): entering needs the plain radius, leaving needs
    the wider one.

Consequences that shape the tuner:

1. ref_power and attenuation are global, so they never change *which* scanner is
   nearest. Only the per-scanner rssi offsets change the ranking. Global values
   only move the "too far away, no room" cut-off, which in rssi terms is
   ``ref_power - 10 * attenuation * log10(max_radius)``. Adding the same number
   to every offset is exactly the same as lowering ref_power by that number.
2. Attenuation cannot be identified from "which room is this device in" data
   alone (it trades off against ref_power), so it is never tuned here.
3. A device in a room with no scanner can never be placed correctly. The best
   Roomwise can do is leave it unplaced instead of putting it in the wrong room,
   which a smaller max radius on the scanner that over-hears it can achieve
   (for example a receiver directly below another room).

So the tuner fits per-scanner offsets (and, if asked, ref_power and per-scanner
max radius) so that each known stationary device ("landmark") has its own-room
scanner nearest by a margin and inside range, and it judges the result with
leave-one-landmark-out scoring: a landmark is never scored with settings that
were tuned using that landmark.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from statistics import median

# The coordinator's hysteresis lets a challenger win outright only when its
# distance differs from the incumbent's by more than this percentage
# difference (pdiff_outright in coordinator._refresh_area_by_min_distance).
COORDINATOR_PDIFF_OUTRIGHT = 0.30
# The coordinator's exit margin: the scanner that holds a device keeps it until
# the device reads beyond its max radius times this (AREA_EXIT_RADIUS_FACTOR in
# const.py). So a device is only reliably left unplaced when every scanner with
# an area reads it beyond that wider distance.
COORDINATOR_EXIT_RADIUS_FACTOR = 1.3

DEFAULT_MIN_SAMPLES = 5  # fewer readings than this for a pair and we ignore the pair
DEFAULT_OFFSET_LIMIT = 15  # dB, tuned offsets stay within +/- this (current value always allowed)
DEFAULT_REF_POWER_STEP_LIMIT = 10  # dB either side of current ref_power, when tuning it
DEFAULT_RADIUS_MARGIN_DB = 2.0  # how far inside the max radius an own-room reading should be
DEFAULT_REGULARISATION = 0.01  # cost per dB^2 of moving an offset away from its current value
DEFAULT_MIN_LANDMARKS = 3  # fewer scored landmarks than this and we refuse to recommend
RADIUS_STEPS = (2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0)  # metres, per-scanner choices
RADIUS_CHANGE_COST = 0.01  # small preference for leaving a scanner's radius alone (or trimming gently)
WRONG_ROOM_WEIGHT = 10.0  # a wrong room costs far more than any margin shortfall
OUTSIDER_WEIGHT = 1.0  # a device in a room with no scanner placed in some other room
OUTSIDER_IN_RANGE_WEIGHT = 0.1  # per receiver that has such a device in range
# Leave-one-out needs one full fit per scored device. Above this many devices
# the "after" score uses k-fold instead (DEFAULT_FOLDS fits), which keeps the
# work bounded on a big install.
DEFAULT_LOO_MAX_LANDMARKS = 20
DEFAULT_FOLDS = 10


class AutotuneCancelledError(Exception):
    """The run was stopped (the dialog was closed) before the tuner finished."""


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Landmark:
    """A stationary device whose room is known."""

    id: str
    area: str | None
    name: str = ""


@dataclass(frozen=True)
class Scanner:
    """A Bluetooth receiver (proxy) and the room it is in."""

    id: str
    area: str | None
    name: str = ""


@dataclass(frozen=True, slots=True)
class Sample:
    """One received advert: landmark heard by scanner at rssi, time t (seconds)."""

    landmark: str
    scanner: str
    rssi: float
    t: float = 0.0


@dataclass
class Settings:
    """The calibration values that decide distance and therefore room."""

    ref_power: float
    attenuation: float
    max_radius: float
    offsets: dict[str, float] = field(default_factory=dict)
    radii: dict[str, float] = field(default_factory=dict)  # per-scanner max radius, else max_radius

    def offset(self, scanner_id: str) -> float:
        return self.offsets.get(scanner_id, 0)

    def radius(self, scanner_id: str) -> float:
        # Same rule as the coordinator: a per-scanner value never exceeds the global one.
        return min(self.radii.get(scanner_id, self.max_radius), self.max_radius)

    def copy(self) -> Settings:
        return Settings(self.ref_power, self.attenuation, self.max_radius, dict(self.offsets), dict(self.radii))

    def rssi_cutoff(self, scanner_id: str | None = None) -> float:
        """Lowest offset-adjusted rssi that is still within range (of this scanner)."""
        radius = self.max_radius if scanner_id is None else self.radius(scanner_id)
        return self.ref_power - 10 * self.attenuation * math.log10(radius)


@dataclass
class LandmarkResult:
    """Outcome for one landmark."""

    landmark: Landmark
    scored: bool
    reason: str  # why not scored, or "" when scored
    before_area: str | None = None
    after_area: str | None = None  # leave-one-out prediction
    before_ok: bool = False
    after_ok: bool = False
    heard_by: dict[str, float] = field(default_factory=dict)  # scanner -> median rssi
    outsider: bool = False  # in a room with no scanner: "ok" means not put in a wrong room


@dataclass
class TuneReport:
    """Everything the UI (or an offline replay) needs to show and decide."""

    current: Settings
    proposed: Settings
    results: list[LandmarkResult]
    scored: int
    right_before: int
    right_after: int  # leave-one-out
    recommend: bool
    summary: str
    tuned_ref_power: bool = False
    outsiders: int = 0  # devices in rooms with no scanner, heard by at least one scanner
    misplaced_before: int = 0  # of those, put in some other room
    misplaced_after: int = 0  # leave-one-out
    notes: list[str] = field(default_factory=list)

    @property
    def regressions(self) -> list[LandmarkResult]:
        return [r for r in self.results if r.scored and r.before_ok and not r.after_ok]

    @property
    def fixes(self) -> list[LandmarkResult]:
        return [r for r in self.results if r.scored and not r.before_ok and r.after_ok]

    def per_area(self) -> dict[str, dict[str, int]]:
        """Per room: landmarks, scored, right before, right after."""
        table: dict[str, dict[str, int]] = {}
        for res in self.results:
            row = table.setdefault(
                res.landmark.area or "", {"landmarks": 0, "scored": 0, "right_before": 0, "right_after": 0}
            )
            row["landmarks"] += 1
            if res.scored:
                row["scored"] += 1
                row["right_before"] += int(res.before_ok)
                row["right_after"] += int(res.after_ok)
        return table


# --------------------------------------------------------------------------
# Distance and room rule (must match the integration)
# --------------------------------------------------------------------------


def rssi_to_metres(rssi: float, ref_power: float, attenuation: float) -> float:
    """
    Mirror of util.rssi_to_metres (tests assert they agree).

    Kept here, not imported, so this module can run without the integration
    package (which imports Home Assistant) for offline replays.
    """
    return 10 ** ((ref_power - rssi) / (10 * attenuation))


def estimated_distance(settings: Settings, scanner_id: str, rssi: float) -> float:
    """Distance exactly as BermudaAdvert._update_raw_distance computes it."""
    return rssi_to_metres(rssi + settings.offset(scanner_id), settings.ref_power, settings.attenuation)


def margin_db(attenuation: float) -> float:
    """
    The rssi gap that equals the coordinator's outright-switch threshold.

    A percentage difference of p between two distances is a distance ratio of
    r = (2 + p) / (2 - p); in rssi terms that is 10 * attenuation * log10(r).
    For the default attenuation of 3 this is about 3.9 dB.
    """
    ratio = (2 + COORDINATOR_PDIFF_OUTRIGHT) / (2 - COORDINATOR_PDIFF_OUTRIGHT)
    return 10 * attenuation * math.log10(ratio)


def predict_area(
    heard: dict[str, float], scanners: dict[str, Scanner], settings: Settings
) -> tuple[str | None, str | None]:
    """
    Steady-state room rule: nearest scanner that has an area and is within its max radius.

    Returns (area, scanner_id). A tie between scanners in *different* areas is
    reported as (None, None): the coordinator would keep whichever it had, so
    it is not reliably right.
    """
    best_dist = math.inf
    best: list[str] = []
    for scanner_id, rssi in heard.items():
        scanner = scanners.get(scanner_id)
        if scanner is None or scanner.area is None:
            continue
        dist = estimated_distance(settings, scanner_id, rssi)
        if dist > settings.radius(scanner_id):
            continue
        if math.isclose(dist, best_dist, rel_tol=1e-9):
            best.append(scanner_id)
        elif dist < best_dist:
            best_dist = dist
            best = [scanner_id]
    if not best:
        return None, None
    areas = {scanners[s].area for s in best}
    if len(areas) > 1:
        return None, None
    return scanners[best[0]].area, best[0]


def holding_area(heard: dict[str, float], scanners: dict[str, Scanner], settings: Settings) -> str | None:
    """
    The room the coordinator may keep a device in, given the exit margin.

    Used for devices in a room with no scanner, which should be left unplaced.
    The coordinator lets a scanner keep a device until it reads beyond max
    radius x COORDINATOR_EXIT_RADIUS_FACTOR, and a device near the edge does
    get captured now and then (a noise dip inside the plain radius). So the
    device is only reliably unplaced when no scanner with an area reads it
    within that wider distance. Returns the area of the nearest such scanner
    (a tie goes to the lowest scanner id), or None when it is reliably unplaced.
    """
    found: tuple[float, str] | None = None
    for scanner_id, rssi in heard.items():
        scanner = scanners.get(scanner_id)
        if scanner is None or scanner.area is None:
            continue
        dist = estimated_distance(settings, scanner_id, rssi)
        if dist > settings.radius(scanner_id) * COORDINATOR_EXIT_RADIUS_FACTOR:
            continue
        if found is None or (dist, scanner_id) < found:
            found = (dist, scanner_id)
    return None if found is None else scanners[found[1]].area


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def aggregate(
    samples: Iterable[Sample],
    min_samples: int = DEFAULT_MIN_SAMPLES,
    window: tuple[float, float] | None = None,
) -> dict[str, dict[str, float]]:
    """Median rssi per (landmark, scanner); pairs with too few readings are dropped."""
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sample in samples:
        if sample.rssi is None:
            continue
        if window is not None and not window[0] <= sample.t <= window[1]:
            continue
        buckets[(sample.landmark, sample.scanner)].append(sample.rssi)
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (landmark, scanner), values in buckets.items():
        if len(values) >= min_samples:
            out[landmark][scanner] = float(median(values))
    return dict(out)


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


@dataclass
class TuneOptions:
    """Knobs for the fit. Defaults are what the integration uses."""

    min_samples: int = DEFAULT_MIN_SAMPLES
    offset_limit: float = DEFAULT_OFFSET_LIMIT
    regularisation: float = DEFAULT_REGULARISATION
    radius_margin_db: float = DEFAULT_RADIUS_MARGIN_DB
    margin_db: float | None = None  # None: derive from attenuation (coordinator hysteresis)
    tune_ref_power: bool = False
    ref_power_limit: float = DEFAULT_REF_POWER_STEP_LIMIT
    tune_radius: bool = True  # per-scanner max radius, only ever made smaller than the global one
    min_landmarks: int = DEFAULT_MIN_LANDMARKS
    max_passes: int = 20
    loo_max_landmarks: int = DEFAULT_LOO_MAX_LANDMARKS  # above this, k-fold instead of leave-one-out
    folds: int = DEFAULT_FOLDS
    should_stop: Callable[[], bool] | None = None  # polled during the work; True raises AutotuneCancelledError

    def check_stop(self) -> None:
        if self.should_stop is not None and self.should_stop():
            raise AutotuneCancelledError


def _landmark_cost(
    heard: dict[str, float],
    own_area: str,
    scanners: dict[str, Scanner],
    settings: Settings,
    margin: float,
    radius_margin: float,
) -> float:
    """Cost for one training landmark in a room that has a scanner (lower is better)."""
    own = -math.inf
    own_cutoff = math.inf
    other = -math.inf
    for scanner_id, rssi in heard.items():
        scanner = scanners.get(scanner_id)
        if scanner is None or scanner.area is None:
            continue
        adj = rssi + settings.offset(scanner_id)
        if scanner.area == own_area:
            if adj > own:
                own, own_cutoff = adj, settings.rssi_cutoff(scanner_id)
        else:
            other = max(other, adj)
    if own == -math.inf:
        return 0.0  # own room never heard it: nothing any setting can do
    area, _ = predict_area(heard, scanners, settings)
    cost = 0.0 if area == own_area else WRONG_ROOM_WEIGHT
    if other > -math.inf:
        cost += max(0.0, margin - (own - other))
    cost += max(0.0, own_cutoff + radius_margin - own)
    return cost


def _outsider_cost(heard: dict[str, float], scanners: dict[str, Scanner], settings: Settings) -> float:
    """A device in a room with no scanner should be left unplaced rather than put in a wrong room."""
    cost = 0.0 if holding_area(heard, scanners, settings) is None else OUTSIDER_WEIGHT
    # A little extra per receiver that could still hold it, so trimming one
    # receiver at a time can make progress when several over-hear the room.
    for scanner_id, rssi in heard.items():
        scanner = scanners.get(scanner_id)
        if scanner is not None and scanner.area is not None:
            limit = settings.radius(scanner_id) * COORDINATOR_EXIT_RADIUS_FACTOR
            if estimated_distance(settings, scanner_id, rssi) <= limit:
                cost += OUTSIDER_IN_RANGE_WEIGHT
    return cost


def _total_cost(
    training: dict[str, tuple[dict[str, float], str]],
    outsiders: dict[str, dict[str, float]],
    scanners: dict[str, Scanner],
    settings: Settings,
    current: Settings,
    options: TuneOptions,
    margin: float,
) -> float:
    cost = 0.0
    for heard, area in training.values():
        cost += _landmark_cost(heard, area, scanners, settings, margin, options.radius_margin_db)
    for heard in outsiders.values():
        cost += _outsider_cost(heard, scanners, settings)
    reg = options.regularisation
    for scanner_id in scanners:
        cost += reg * (settings.offset(scanner_id) - current.offset(scanner_id)) ** 2
        if settings.radius(scanner_id) != current.radius(scanner_id):
            # prefer the gentlest trim that works: cost grows with how much range is taken away
            cost += RADIUS_CHANGE_COST * (1 + abs(math.log2(current.radius(scanner_id) / settings.radius(scanner_id))))
    cost += reg * (settings.ref_power - current.ref_power) ** 2
    return cost


def _offset_range(current: float, limit: float) -> list[float]:
    lo = min(-limit, current)
    hi = max(limit, current)
    values = list(range(math.ceil(lo), math.floor(hi) + 1))
    if current not in values:
        values.append(current)
    return sorted(values)


def fit(  # noqa: C901 - coordinate descent in two stages, kept in one place on purpose
    medians: dict[str, dict[str, float]],
    landmarks: Iterable[Landmark],
    scanners: Iterable[Scanner],
    current: Settings,
    options: TuneOptions | None = None,
) -> Settings:
    """
    Fit per-scanner offsets (and optionally ref_power and per-scanner radius) to the given landmarks.

    Only the landmarks passed in are used. Landmarks in a room that has a
    scanner must land in it; landmarks in a room without one should not be put
    in any room (used only when tuning radius). Coordinate descent over 1 dB
    steps plus a "move every offset together" step; deterministic.
    """
    options = options or TuneOptions()
    scanners = list(scanners)
    scanner_map = {s.id: s for s in scanners}
    scanner_areas = {s.area for s in scanners if s.area is not None}
    margin = options.margin_db if options.margin_db is not None else margin_db(current.attenuation)
    training: dict[str, tuple[dict[str, float], str]] = {}
    outsiders: dict[str, dict[str, float]] = {}
    for lm in landmarks:
        if lm.area is None or lm.id not in medians:
            continue
        if lm.area in scanner_areas:
            training[lm.id] = (medians[lm.id], lm.area)
        elif options.tune_radius:
            outsiders[lm.id] = medians[lm.id]
    best = current.copy()
    for scanner_id in scanner_map:
        best.offsets.setdefault(scanner_id, current.offset(scanner_id))
    if not training:
        return best

    def cost_of(settings: Settings) -> float:
        return _total_cost(training, {}, scanner_map, settings, current, options, margin)

    best_cost = cost_of(best)
    ranges = {sid: _offset_range(current.offset(sid), options.offset_limit) for sid in sorted(scanner_map)}
    radius_choices = sorted({current.max_radius, *(r for r in RADIUS_STEPS if r < current.max_radius)})

    def try_trial(trial: Settings) -> bool:
        nonlocal best, best_cost
        cost = cost_of(trial)
        if cost < best_cost - 1e-9:
            best, best_cost = trial, cost
            return True
        return False

    for _ in range(options.max_passes):
        improved = False
        # one scanner at a time
        for scanner_id, values in ranges.items():
            options.check_stop()
            for value in values:
                if value == best.offsets[scanner_id]:
                    continue
                trial = best.copy()
                trial.offsets[scanner_id] = value
                improved |= try_trial(trial)
        # all scanners together (moves the range cut-off without changing ranking)
        for shift in range(-int(options.offset_limit), int(options.offset_limit) + 1):
            if shift == 0:
                continue
            trial = best.copy()
            ok = True
            for scanner_id, values in ranges.items():
                newval = trial.offsets[scanner_id] + shift
                if not values[0] <= newval <= values[-1]:
                    ok = False
                    break
                trial.offsets[scanner_id] = newval
            if ok:
                improved |= try_trial(trial)
        if options.tune_ref_power:
            lim = int(options.ref_power_limit)
            for step in range(-lim, lim + 1):
                value = current.ref_power + step
                if value == best.ref_power:
                    continue
                trial = best.copy()
                trial.ref_power = value
                improved |= try_trial(trial)
        if not improved:
            break

    # Stage 2: with offsets fixed, trim per-scanner radius so devices in rooms
    # without a receiver are left unplaced, never at the cost of an in-room device.
    # Kept separate so the outsiders cannot drag the offsets around.
    if options.tune_radius and outsiders:
        stage1 = best
        best_cost = _total_cost(training, outsiders, scanner_map, best, stage1, options, margin)
        # Only trim a receiver that hears at least one device in its own room: that
        # device is what stops the trim from cutting the receiver's own room off.
        trimmable = sorted(
            {
                sid
                for heard, area in training.values()
                for sid in heard
                if sid in scanner_map and scanner_map[sid].area == area
            }
        )
        for _ in range(options.max_passes):
            improved = False
            for scanner_id in trimmable:
                options.check_stop()
                for radius in radius_choices:
                    if radius == best.radius(scanner_id):
                        continue
                    trial = best.copy()
                    if radius == current.max_radius:
                        trial.radii.pop(scanner_id, None)
                    else:
                        trial.radii[scanner_id] = radius
                    cost = _total_cost(training, outsiders, scanner_map, trial, stage1, options, margin)
                    if cost < best_cost - 1e-9:
                        best, best_cost, improved = trial, cost, True
            if not improved:
                break
    return best


# --------------------------------------------------------------------------
# Scoring and decision
# --------------------------------------------------------------------------

FitFunc = Callable[..., Settings]


def _judged_area(res: LandmarkResult, scanner_map: dict[str, Scanner], settings: Settings) -> str | None:
    """Device in a room with a scanner: where it goes. Outsider: where it may be kept (see holding_area)."""
    if res.outsider:
        return holding_area(res.heard_by, scanner_map, settings)
    return predict_area(res.heard_by, scanner_map, settings)[0]


def evaluate(
    landmarks: Iterable[Landmark],
    scanners: Iterable[Scanner],
    samples: Iterable[Sample] | None,
    current: Settings,
    options: TuneOptions | None = None,
    medians: dict[str, dict[str, float]] | None = None,
    fit_func: FitFunc | None = None,
    area_names: dict[str, str] | None = None,
) -> TuneReport:
    """
    Tune and judge. Before = current settings. After = leave-one-landmark-out.

    Either pass raw samples or pre-aggregated medians. With more than
    options.loo_max_landmarks devices to judge, "after" is scored by k-fold
    (options.folds fits, each leaving out a whole group) to bound the work.
    Raises AutotuneCancelledError when options.should_stop() turns True.
    """
    options = options or TuneOptions()
    fit_func = fit_func or fit
    names = area_names or {}
    landmarks = list(landmarks)
    scanners = list(scanners)
    scanner_map = {s.id: s for s in scanners}
    scanner_areas = {s.area for s in scanners if s.area is not None}
    if medians is None:
        medians = aggregate(samples or [], min_samples=options.min_samples)

    results: list[LandmarkResult] = []
    usable: list[Landmark] = []  # everything the fit may learn from
    for lm in landmarks:
        heard = {s: r for s, r in medians.get(lm.id, {}).items() if s in scanner_map}
        if lm.area is None:
            results.append(LandmarkResult(lm, False, "no room set", heard_by=heard))
        elif not heard:
            results.append(LandmarkResult(lm, False, "not heard (or too few readings)", heard_by=heard))
        elif lm.area not in scanner_areas:
            results.append(LandmarkResult(lm, False, "no scanner in this room", heard_by=heard, outsider=True))
            if options.tune_radius:
                usable.append(lm)
        else:
            results.append(LandmarkResult(lm, True, "", heard_by=heard))
            usable.append(lm)

    options.check_stop()
    proposed = fit_func({lm.id: medians[lm.id] for lm in usable}, usable, scanners, current, options)

    judged: list[LandmarkResult] = []
    for res in results:
        if not (res.scored or res.outsider):
            continue
        res.before_area = _judged_area(res, scanner_map, current)
        if res.outsider and not options.tune_radius:
            res.after_area = res.before_area
        else:
            judged.append(res)

    # Groups of devices to leave out together: one each (leave-one-out), or
    # k interleaved folds when there are many.
    folded = len(judged) > max(options.loo_max_landmarks, 1)
    if folded:
        folds = max(2, options.folds)
        groups = [judged[i::folds] for i in range(folds)]
    else:
        groups = [[res] for res in judged]
    for group in groups:
        if not group:
            continue
        options.check_stop()
        left_out = {res.landmark.id for res in group}
        training = [other for other in usable if other.id not in left_out]
        loo_settings = fit_func(
            {other.id: medians[other.id] for other in training}, training, scanners, current, options
        )
        for res in group:
            res.after_area = _judged_area(res, scanner_map, loo_settings)

    for res in results:
        if not (res.scored or res.outsider):
            continue
        lm = res.landmark
        if res.outsider:
            res.before_ok = res.before_area is None
            res.after_ok = res.after_area is None
        else:
            res.before_ok = res.before_area == lm.area
            res.after_ok = res.after_area == lm.area

    scored = [r for r in results if r.scored]
    outs = [r for r in results if r.outsider]
    n = len(scored)
    before = sum(r.before_ok for r in scored)
    after = sum(r.after_ok for r in scored)
    mis_before = sum(not r.before_ok for r in outs)
    mis_after = sum(not r.after_ok for r in outs)
    unscored = len(results) - n
    changed = _settings_changed(current, proposed)

    if n < options.min_landmarks:
        recommend = False
        summary = (
            f"Only {n} known device{'s' if n != 1 else ''} could be checked "
            f"(at least {options.min_landmarks} are needed), so no changes are suggested."
        )
    elif changed and (after > before or (after == before and mis_after < mis_before)):
        recommend = True
        summary = f"{after} of {n} known devices land in the right room (was {before} of {n})."
        if outs and mis_after != mis_before:
            summary += (
                f" Of {len(outs)} devices in rooms without a receiver, {mis_after} are put in the wrong room"
                f" (was {mis_before})."
            )
    else:
        recommend = False
        summary = (
            f"{before} of {n} known devices land in the right room. "
            "Your current settings are already as good as we can get from this data."
        )
    if unscored:
        summary += (
            f" {unscored} other device{'s' if unscored != 1 else ''} could not be checked"
            " (no receiver in the room, no room set, or not heard)."
        )

    if folded:
        summary += (
            f" Because many devices were chosen, each was checked with settings tuned without its group of"
            f" about {max(1, round(len(judged) / max(2, options.folds)))} devices."
        )

    final = proposed if recommend else current.copy()
    report = TuneReport(
        current=current,
        proposed=final,
        results=results,
        scored=n,
        right_before=before,
        right_after=after,
        recommend=recommend,
        summary=summary,
        tuned_ref_power=options.tune_ref_power,
        outsiders=len(outs),
        misplaced_before=mis_before,
        misplaced_after=mis_after,
    )
    report.notes = explain(report, scanner_map, final, names)
    return report


def explain(report: TuneReport, scanners: dict[str, Scanner], settings: Settings, names: dict[str, str]) -> list[str]:
    """
    Plain-language notes on what tuning cannot fix.

    The main case: devices in a room with no receiver that one receiver hears
    (often through a floor or wall), sometimes more strongly than that
    receiver's own room. Offsets cannot fix that; only a receiver in that room,
    or a smaller range on the receiver that over-hears it.
    """

    def nm(area: str | None) -> str:
        return names.get(area, area) if area else "(no room)"

    notes: list[str] = []
    by_room: dict[str, list[LandmarkResult]] = defaultdict(list)
    for res in report.results:
        if res.outsider:
            by_room[res.landmark.area].append(res)
    own_best: dict[str, float] = {}  # scanner -> strongest reading of a device in its own room
    for res in report.results:
        if res.scored:
            for sid, rssi in res.heard_by.items():
                if scanners[sid].area == res.landmark.area:
                    own_best[sid] = max(own_best.get(sid, -math.inf), rssi)
    for room, rows in sorted(by_room.items(), key=lambda kv: nm(kv[0])):
        misplaced = [r for r in rows if not r.after_ok]
        if not misplaced:
            continue
        loudest: dict[str, float] = {}
        for r in misplaced:
            sid = max(r.heard_by, key=lambda s: r.heard_by[s] + settings.offset(s))
            loudest[sid] = max(loudest.get(sid, -math.inf), r.heard_by[sid])
        for sid, rssi in sorted(loudest.items()):
            receiver = scanners[sid].name or sid
            text = (
                f"Devices in {nm(room)} are heard best by the {receiver} receiver in {nm(scanners[sid].area)}, "
                f"probably through a wall or floor, so they will show as {nm(scanners[sid].area)}. "
                f"Add a receiver in {nm(room)}, or lower that receiver's max radius."
            )
            if sid in own_best and rssi > own_best[sid]:
                text += (
                    f" They are heard more strongly ({rssi:g} dBm) than devices in its own room"
                    f" ({own_best[sid]:g} dBm), so no setting can tell them apart; only a receiver in"
                    f" {nm(room)} can."
                )
            notes.append(text)
    for res in report.results:
        if res.scored and not res.after_ok and res.heard_by:
            own = [s for s in res.heard_by if scanners[s].area == res.landmark.area]
            if not own:
                notes.append(
                    f"{res.landmark.name or res.landmark.id} in {nm(res.landmark.area)} was never heard by the "
                    "receiver in its own room in this data."
                )
    return notes


def _settings_changed(a: Settings, b: Settings) -> bool:
    if a.ref_power != b.ref_power or a.attenuation != b.attenuation:
        return True
    keys = set(a.offsets) | set(b.offsets)
    if any(a.offset(k) != b.offset(k) for k in keys):
        return True
    keys = set(a.radii) | set(b.radii)
    return any(a.radius(k) != b.radius(k) for k in keys)


def format_table(report: TuneReport, scanner_names: dict[str, str] | None = None) -> str:
    """Markdown tables: per room, then setting changes, then notes."""
    lines = ["| Room | Devices checked | Right now | Right after tuning |", "|---|---:|---:|---:|"]
    for area, row in sorted(report.per_area().items()):
        if row["scored"] == 0:
            lines.append(f"| {area or '(none)'} | 0 of {row['landmarks']} | - | - |")
        else:
            lines.append(f"| {area} | {row['scored']} | {row['right_before']} | {row['right_after']} |")
    if report.recommend:
        names = scanner_names or {}
        lines += ["", "| Scanner | Offset now | Proposed | Max radius now | Proposed |", "|---|---:|---:|---:|---:|"]
        for sid in sorted(report.proposed.offsets):
            lines.append(
                f"| {names.get(sid, sid)} | {report.current.offset(sid):g} | {report.proposed.offset(sid):g}"
                f" | {report.current.radius(sid):g} | {report.proposed.radius(sid):g} |"
            )
        if report.tuned_ref_power and report.proposed.ref_power != report.current.ref_power:
            lines.append(f"| (reference power) | {report.current.ref_power:g} | {report.proposed.ref_power:g} | | |")
    elif report.current.radii:
        # Not changing anything, but say which receivers already have a smaller range.
        names = scanner_names or {}
        lines += [
            "",
            f"Range limits saved earlier (the global max radius is {report.current.max_radius:g} m):",
            "",
            "| Scanner | Max radius |",
            "|---|---:|",
        ]
        lines += [f"| {names.get(sid, sid)} | {report.current.radius(sid):g} |" for sid in sorted(report.current.radii)]
    if report.notes:
        lines += [""] + [f"- {note}" for note in report.notes]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Sample export / import (for offline replay of a live run)
# --------------------------------------------------------------------------

EXPORT_FORMAT = "roomwise-autotune-samples/1"


def export_dict(
    landmarks: Iterable[Landmark],
    scanners: Iterable[Scanner],
    settings: Settings,
    samples: Iterable[Sample],
    area_names: dict[str, str] | None = None,
) -> dict:
    """Everything needed to replay a run offline, as plain JSON-able data."""
    return {
        "format": EXPORT_FORMAT,
        "settings": {
            "ref_power": settings.ref_power,
            "attenuation": settings.attenuation,
            "max_radius": settings.max_radius,
            "offsets": dict(settings.offsets),
            "radii": dict(settings.radii),
        },
        "landmarks": [{"id": lm.id, "area": lm.area, "name": lm.name} for lm in landmarks],
        "scanners": [{"id": s.id, "area": s.area, "name": s.name} for s in scanners],
        "area_names": dict(area_names or {}),
        "samples": [[s.landmark, s.scanner, s.rssi, s.t] for s in samples],
    }


def load_export(data: dict) -> tuple[list[Landmark], list[Scanner], Settings, list[Sample]]:
    """Inverse of export_dict: (landmarks, scanners, settings, samples)."""
    if data.get("format") != EXPORT_FORMAT:
        msg = f"Not a Roomwise auto-tune export: {data.get('format')!r}"
        raise ValueError(msg)
    st = data["settings"]
    settings = Settings(
        float(st["ref_power"]),
        float(st["attenuation"]),
        float(st["max_radius"]),
        dict(st.get("offsets", {})),
        dict(st.get("radii", {})),
    )
    landmarks = [Landmark(x["id"], x.get("area"), x.get("name", "")) for x in data["landmarks"]]
    scanners = [Scanner(x["id"], x.get("area"), x.get("name", "")) for x in data["scanners"]]
    samples = [Sample(lm, sc, float(rssi), float(t)) for lm, sc, rssi, t in data["samples"]]
    return landmarks, scanners, settings, samples

"""Auto-tune size limits: bounded readings, bounded fair-test work, cancellation, export off the loop."""

from __future__ import annotations

import asyncio
import threading
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import autotune as at
from custom_components.roomwise import autotune_ha

from .test_autotune import DEFAULT, SCANNERS, make_house


class FakeCoordinator:
    """Just what SampleCollector reads: devices with adverts, and a listener hook."""

    def __init__(self, landmarks: int, scanners: int) -> None:
        self.scanner_ids = [f"aa:aa:aa:aa:{s // 256:02x}:{s % 256:02x}" for s in range(scanners)]
        self.landmark_ids = [f"11:11:11:11:{i // 256:02x}:{i % 256:02x}" for i in range(landmarks)]
        self.devices = {
            lid: SimpleNamespace(
                adverts={sid: SimpleNamespace(scanner_address=sid, rssi=-60, stamp=0.0) for sid in self.scanner_ids}
            )
            for lid in self.landmark_ids
        }
        self.listeners = []

    def async_add_listener(self, cb):
        self.listeners.append(cb)
        return lambda: self.listeners.remove(cb)

    def tick(self, stamp: float) -> None:
        """Every device heard once more by every receiver."""
        for dev in self.devices.values():
            for adv in dev.adverts.values():
                adv.stamp = stamp
                adv.rssi = -60 - int(stamp) % 7
        for cb in list(self.listeners):
            cb()


# --------------------------------------------------------------------------
# Collector bounds
# --------------------------------------------------------------------------


def test_collector_keeps_a_bounded_sample_of_each_pair_over_the_whole_run():
    coord = FakeCoordinator(landmarks=1, scanners=1)
    collector = autotune_ha.SampleCollector(coord, coord.landmark_ids, max_per_pair=50)
    collector.start()
    for stamp in range(1, 1001):
        coord.tick(float(stamp))
    collector.stop()
    kept = collector.samples
    assert len(kept) == 50
    assert collector.stored == 50
    assert not collector.capped
    stamps = sorted(s.t for s in kept)
    # spread over the whole window, not just the first or last readings
    assert stamps[0] < 250
    assert stamps[-1] > 750


def test_collector_overall_cap_sets_capped():
    coord = FakeCoordinator(landmarks=5, scanners=2)  # 10 pairs
    collector = autotune_ha.SampleCollector(coord, coord.landmark_ids, max_per_pair=600, max_total=55)
    collector.start()
    for stamp in range(1, 101):
        coord.tick(float(stamp))
    collector.stop()
    assert collector.stored == 55
    assert len(collector.samples) == 55
    assert collector.capped


def test_worst_case_memory_stays_small():
    """The most a user can pick (60 devices) with 20 receivers, past every limit: under 50 MB incl. the export."""
    coord = FakeCoordinator(landmarks=autotune_ha.MAX_AUTOTUNE_LANDMARKS, scanners=20)
    tracemalloc.start()
    try:
        collector = autotune_ha.SampleCollector(coord, coord.landmark_ids)
        collector.start()
        stamp = 0.0
        while not collector.capped:
            stamp += 1.0
            coord.tick(stamp)
        for _ in range(20):  # keep going: memory must not grow past the cap
            stamp += 1.0
            coord.tick(stamp)
        collector.stop()
        assert collector.stored == autotune_ha.MAX_TOTAL_SAMPLES
        export = at.export_dict([], [], DEFAULT, collector.samples, {})
        assert len(export["samples"]) == autotune_ha.MAX_TOTAL_SAMPLES
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 50 * 1024 * 1024, f"peak {peak / 1e6:.1f} MB"


# --------------------------------------------------------------------------
# Bounded fair-test work
# --------------------------------------------------------------------------


def test_many_devices_use_k_fold_with_a_bounded_number_of_fits():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=8, seed=5)  # 32 devices
    calls: list[set[str]] = []

    def spy_fit(medians, train, scanners, current, options):
        train = list(train)
        calls.append({lm.id for lm in train})
        return at.fit(medians, train, scanners, current, options)

    opts = at.TuneOptions(loo_max_landmarks=20, folds=10)
    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT, opts, fit_func=spy_fit)
    assert len(calls) == 1 + 10  # one final fit, one per fold, instead of 1 + 32
    all_ids = {lm.id for lm in landmarks}
    assert calls[0] == all_ids
    left_out = [all_ids - used for used in calls[1:]]
    # every scored device is left out of exactly one fit, and judged by that fit
    assert sorted(i for group in left_out for i in group) == sorted(all_ids)
    assert report.scored == 32
    assert report.right_after > report.right_before
    assert "group" in report.summary


def test_few_devices_still_use_leave_one_out():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=3, seed=3)  # 12 devices
    count = 0

    def spy_fit(medians, train, scanners, current, options):
        nonlocal count
        count += 1
        return at.fit(medians, train, scanners, current, options)

    report = at.evaluate(landmarks, SCANNERS, samples, DEFAULT, fit_func=spy_fit)
    assert count == 1 + report.scored == 13


def test_fit_and_evaluate_stop_when_asked():
    landmarks, samples = make_house({"scanner_office": 12.0}, per_room=3, seed=3)
    medians = at.aggregate(samples)
    checks = 0

    def stop_after_three():
        nonlocal checks
        checks += 1
        return checks > 3

    with pytest.raises(at.AutotuneCancelledError):
        at.fit(medians, landmarks, SCANNERS, DEFAULT, at.TuneOptions(should_stop=stop_after_three))
    with pytest.raises(at.AutotuneCancelledError):
        at.evaluate(landmarks, SCANNERS, samples, DEFAULT, at.TuneOptions(should_stop=lambda: True))


# --------------------------------------------------------------------------
# HA side: cancellation and export off the event loop
# --------------------------------------------------------------------------


async def test_closing_the_dialog_stops_the_tuner_job(hass: HomeAssistant):
    coord = FakeCoordinator(landmarks=2, scanners=2)
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def slow_evaluate(landmarks, scanners, samples, settings, options=None, **kwargs):
        started.set()
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < 10:
                options.check_stop()
                time.sleep(0.01)
            outcome["result"] = "ran to the end"
        except at.AutotuneCancelledError:
            outcome["result"] = "stopped"
        outcome["elapsed"] = time.monotonic() - t0
        finished.set()
        raise at.AutotuneCancelledError

    landmarks = [at.Landmark(lid, "Kitchen") for lid in coord.landmark_ids]
    scanners = [at.Scanner(sid, "Kitchen") for sid in coord.scanner_ids]
    with patch.object(autotune_ha, "evaluate", side_effect=slow_evaluate):
        task = hass.async_create_task(autotune_ha.async_run_autotune(hass, coord, {}, 0, landmarks, scanners))
        assert await hass.async_add_executor_job(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await hass.async_add_executor_job(finished.wait, 5)
    assert outcome["result"] == "stopped"
    assert outcome["elapsed"] < 5


async def test_export_is_built_off_the_event_loop(hass: HomeAssistant, tmp_path):
    coord = FakeCoordinator(landmarks=2, scanners=2)
    threads = []
    real_export = autotune_ha.export_dict

    def spy_export(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            threads.append("event loop")
        except RuntimeError:
            threads.append("executor")
        return real_export(*args, **kwargs)

    landmarks = [at.Landmark(lid, "Kitchen") for lid in coord.landmark_ids]
    scanners = [at.Scanner(sid, "Kitchen") for sid in coord.scanner_ids]
    path = tmp_path / "export.json"
    with patch.object(autotune_ha, "export_dict", side_effect=spy_export):
        await autotune_ha.async_run_autotune(hass, coord, {}, 0, landmarks, scanners, {}, str(path))
    assert threads == ["executor"]
    assert path.exists()


async def test_capped_run_says_so_in_the_result(hass: HomeAssistant):
    coord = FakeCoordinator(landmarks=2, scanners=2)
    landmarks = [at.Landmark(lid, "Kitchen") for lid in coord.landmark_ids]
    scanners = [at.Scanner(sid, "Kitchen") for sid in coord.scanner_ids]
    coord.tick(1.0)  # 4 fresh readings waiting, before anyone listens
    with patch.object(autotune_ha, "MAX_TOTAL_SAMPLES", 3):
        report = await autotune_ha.async_run_autotune(hass, coord, {}, 0, landmarks, scanners)
    assert "memory limit" in report.summary


# --------------------------------------------------------------------------
# Form limits
# --------------------------------------------------------------------------


async def _submit_first_step(hass: HomeAssistant, entry: MockConfigEntry, selected: int, minutes: float):
    cands = [
        autotune_ha.Candidate(f"11:22:33:44:{i // 256:02x}:{i % 256:02x}", f"Dev {i}", None, False) for i in range(70)
    ]
    found = autotune_ha.Discovery(scanners=[at.Scanner("aa:aa:aa:aa:aa:01", "kitchen", "Kitchen proxy")])
    with (
        patch("custom_components.roomwise.autotune_flow.discover", return_value=found),
        patch("custom_components.roomwise.autotune_flow.candidates", return_value=cands),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={"next_step_id": "autotune"}
        )
        return await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                "autotune_selected": [c.address for c in cands[:selected]],
                "autotune_minutes": minutes,
                "autotune_export": False,
            },
        )


async def test_form_refuses_too_many_devices(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _submit_first_step(hass, setup_bermuda_entry, autotune_ha.MAX_AUTOTUNE_LANDMARKS + 1, 5)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "autotune"
    assert result["errors"] == {"base": "autotune_too_many_devices"}


async def test_form_refuses_too_much_listening(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _submit_first_step(hass, setup_bermuda_entry, 50, 30)  # 1500 device-minutes
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "autotune"
    assert result["errors"] == {"base": "autotune_too_long"}


async def test_form_accepts_a_normal_run(hass: HomeAssistant, setup_bermuda_entry: MockConfigEntry):
    result = await _submit_first_step(hass, setup_bermuda_entry, 40, 10)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "autotune_rooms"


def test_error_texts_match_the_limits():
    import json
    from pathlib import Path

    strings = json.loads((Path(autotune_ha.__file__).parent / "translations" / "en.json").read_text(encoding="utf-8"))
    errors = strings["options"]["error"]
    assert str(autotune_ha.MAX_AUTOTUNE_LANDMARKS) in errors["autotune_too_many_devices"]
    assert str(autotune_ha.MAX_AUTOTUNE_DEVICE_MINUTES) in errors["autotune_too_long"]

"""A failing update cycle must not turn every Bluetooth advert into another failing update."""

from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.core import HomeAssistant

import custom_components.roomwise.coordinator as coordinator_module


async def test_advert_burst_after_a_failed_update_tries_once_per_interval(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, caplog
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._waitingfor_load_manufacturer_ids = False
    now = [10_000.0]
    calls = []

    def broken_update():
        calls.append(now[0])
        raise RuntimeError("simulated bug in the update cycle")

    with (
        patch.object(coordinator_module, "monotonic_time_coarse", side_effect=lambda: now[0]),
        patch.object(coordinator, "update_metadevices", side_effect=broken_update),
        caplog.at_level(logging.ERROR),
    ):
        # A burst of adverts (busy installs see hundreds a second) within one interval.
        for _ in range(200):
            coordinator.async_handle_advert(None, None)  # must not raise into the Bluetooth stack
        assert len(calls) == 1
        assert coordinator.update_in_progress is False

        # The failure is logged once, not once per advert.
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("custom_components")]
        assert len(errors) == 1
        assert errors[0].exc_info is not None

        # Once the interval has passed it tries again (once).
        now[0] += 3
        for _ in range(200):
            coordinator.async_handle_advert(None, None)
        assert len(calls) == 2


async def test_advert_still_triggers_an_update_when_none_has_run(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    """The advert callback still seeds updates while no entity is driving the coordinator."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._waitingfor_load_manufacturer_ids = False
    with patch.object(coordinator, "_async_update_data_internal") as update:
        coordinator.stamp_last_update = 0
        coordinator.async_handle_advert(None, None)
        update.assert_called_once()

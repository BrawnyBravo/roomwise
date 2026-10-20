"""Leftovers from the integration template are gone, and the start-up banner logs once."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

import custom_components.roomwise.const as const
from custom_components.roomwise.entity import BermudaEntity


def test_template_leftovers_removed():
    assert not hasattr(BermudaEntity, "device_state_attributes")
    assert not hasattr(const, "ATTRIBUTION")


async def test_startup_banner_logs_once_per_run(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, caplog):
    caplog.set_level(logging.INFO, logger="custom_components.roomwise")
    caplog.clear()
    assert await hass.config_entries.async_reload(setup_bermuda_entry.entry_id)
    assert await hass.config_entries.async_reload(setup_bermuda_entry.entry_id)
    assert const.STARTUP_MESSAGE.strip().splitlines()[0] not in caplog.text

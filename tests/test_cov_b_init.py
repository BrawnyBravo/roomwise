"""Setup failure, migration and device-removal edge cases of the integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.roomwise import (
    async_migrate_entry,
    async_remove_config_entry_device,
)
from custom_components.roomwise.const import CONF_DEVICES, DOMAIN, NAME
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

from .const import MOCK_CONFIG


async def test_setup_is_retried_when_the_first_refresh_reports_failure(hass: HomeAssistant, monkeypatch):
    """A refresh that does not raise but leaves the update flagged as failed still means "not ready"."""

    async def failing_refresh(self):
        self.last_update_success = False

    monkeypatch.setattr(BermudaDataUpdateCoordinator, "async_refresh", failing_refresh)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state == ConfigEntryState.SETUP_RETRY


async def test_current_version_needs_no_migration(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is True


async def test_version_three_placeholder_migration_moves_unique_ids(hass: HomeAssistant):
    """The (not yet used) version 3 branch shifts the entry and entity ids and asks for a reload."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", title=NAME, version=3, unique_id="aa:bb:cc:dd:ee:01"
    )
    entry.add_to_hass(hass)
    entity = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, "aa:bb:cc:dd:ee:01_area", config_entry=entry, suggested_object_id="cov_b_area"
    )

    assert await async_migrate_entry(hass, entry) is False

    assert entry.unique_id == "aa:bb:cc:dd:ee:04"
    assert er.async_get(hass).async_get(entity.entity_id).unique_id == "aa:bb:cc:dd:ee:04_area"


async def test_deleting_a_device_that_is_not_one_of_ours_is_allowed(hass: HomeAssistant, bypass_get_data, caplog):
    entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, options={CONF_DEVICES: ["AA:BB:CC:DD:EE:01"]}, entry_id="test", title=NAME
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("some_other_domain", "stale")}, name="Stale Thing"
    )
    assert await async_remove_config_entry_device(hass, entry, stale) is True
    assert "Didn't find address for Stale Thing" in caplog.text
    # Nothing was dropped from the configured devices.
    assert entry.options[CONF_DEVICES] == ["AA:BB:CC:DD:EE:01"]

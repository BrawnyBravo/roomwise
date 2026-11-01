"""A proxy that goes quiet for a long time raises a repair, which clears once it is heard again."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.bluetooth import BaseHaRemoteScanner
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.roomwise.coordinator as coordinator_module
from custom_components.roomwise.const import (
    DOMAIN,
    REPAIR_SCANNER_SILENT,
    REPAIR_SCANNER_WITHOUT_AREA,
    SCANNER_SILENT_AFTER,
    SCANNER_SILENT_CHECK_INTERVAL,
)

STARTED = 1_000.0
MINUTES = str(SCANNER_SILENT_AFTER // 60)


class _Scanner(SimpleNamespace):
    """Scanner stub. Hashable by address, as the real BermudaDevice is."""

    def __hash__(self):
        return hash(self.address)


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test can move forward."""
    now = [STARTED]
    monkeypatch.setattr(coordinator_module, "monotonic_time_coarse", lambda: now[0])
    return now


@pytest.fixture
def coordinator(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, clock):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator.stamp_started = STARTED
    coordinator.stamp_last_silent_check = 0
    return coordinator


def _add_scanner(coordinator, name, address, last_seen):
    scanner = _Scanner(name=name, address=address, last_seen=last_seen)
    coordinator._scanners.add(scanner)
    coordinator._scanner_list.add(address)
    return scanner


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, REPAIR_SCANNER_SILENT)


def _listed(hass):
    return _issue(hass).translation_placeholders


def _check_at(coordinator, clock, stamp):
    clock[0] = stamp
    coordinator._async_check_silent_scanners()


async def test_quiet_proxy_raises_repair_and_clears_when_heard(hass: HomeAssistant, coordinator, clock):
    proxy = _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=STARTED + 10)

    # Quiet for exactly the threshold: not yet.
    _check_at(coordinator, clock, STARTED + 10 + SCANNER_SILENT_AFTER)
    assert _issue(hass) is None

    # Quiet for longer.
    _check_at(coordinator, clock, clock[0] + SCANNER_SILENT_CHECK_INTERVAL)
    issue = _issue(hass)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert issue.translation_key == REPAIR_SCANNER_SILENT
    assert issue.translation_placeholders == {"minutes": MINUTES, "scannerlist": "- Living Room Proxy\n"}

    # The proxy is heard again: the repair goes.
    proxy.last_seen = clock[0] + 5
    _check_at(coordinator, clock, clock[0] + SCANNER_SILENT_CHECK_INTERVAL)
    assert _issue(hass) is None


async def test_repair_is_not_rewritten_while_the_same_proxies_stay_quiet(hass: HomeAssistant, coordinator, clock):
    _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=STARTED)
    _check_at(coordinator, clock, STARTED + 40 * 60)
    assert _issue(hass) is not None

    with patch.object(coordinator_module.ir, "async_create_issue") as create:
        for minute in range(1, 120):
            _check_at(coordinator, clock, STARTED + (40 + minute) * 60)
        create.assert_not_called()


async def test_no_false_alarm_after_startup(hass: HomeAssistant, coordinator, clock):
    # Never heard since Roomwise started.
    _add_scanner(coordinator, "Kitchen Proxy", "aa:bb:cc:dd:ee:02", last_seen=0)

    _check_at(coordinator, clock, STARTED + 29 * 60)
    assert _issue(hass) is None

    # Once Roomwise has been running for more than the threshold, it counts as quiet.
    _check_at(coordinator, clock, STARTED + 31 * 60)
    assert _listed(hass)["scannerlist"] == "- Kitchen Proxy\n"


async def test_reload_keeps_a_live_repair_and_its_ignore(hass: HomeAssistant, coordinator, clock):
    proxy = _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=STARTED)
    _check_at(coordinator, clock, STARTED + 40 * 60)
    assert _issue(hass) is not None
    ir.async_get(hass).async_ignore(DOMAIN, REPAIR_SCANNER_SILENT, True)

    # Roomwise reloads: fresh state, and a proxy it has not caught up on yet.
    coordinator.stamp_started = clock[0]
    coordinator.stamp_last_silent_check = 0
    coordinator._scanners_silent = None
    proxy.last_seen = 0

    # During the grace period nothing is touched, not even deleted.
    with patch.object(coordinator_module.ir, "async_delete_issue") as delete:
        for minute in range(1, SCANNER_SILENT_AFTER // 60 + 1):
            _check_at(coordinator, clock, coordinator.stamp_started + minute * 60)
        delete.assert_not_called()
    assert _issue(hass).dismissed_version is not None

    # After the grace period the same proxy is still quiet: the ignore is kept.
    _check_at(coordinator, clock, coordinator.stamp_started + SCANNER_SILENT_AFTER + 1)
    assert _listed(hass)["scannerlist"] == "- Living Room Proxy\n"
    assert _issue(hass).dismissed_version is not None


async def test_only_the_quiet_proxy_is_listed(hass: HomeAssistant, coordinator, clock):
    now = STARTED + 2 * 3600
    _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=now - 5)
    _add_scanner(coordinator, "Workshop Proxy", "aa:bb:cc:dd:ee:03", last_seen=now - 40 * 60)

    _check_at(coordinator, clock, now)
    assert _listed(hass)["scannerlist"] == "- Workshop Proxy\n"


async def test_check_runs_at_most_once_a_minute(hass: HomeAssistant, coordinator, clock):
    proxy = _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=STARTED)
    now = STARTED + 40 * 60
    _check_at(coordinator, clock, now)
    assert _issue(hass) is not None

    # Heard again, but the next check is not due yet: nothing is re-examined.
    proxy.last_seen = now + 1
    _check_at(coordinator, clock, now + SCANNER_SILENT_CHECK_INTERVAL - 1)
    assert _issue(hass) is not None
    assert coordinator.stamp_last_silent_check == now

    # A minute after the last check, it is looked at again.
    _check_at(coordinator, clock, now + SCANNER_SILENT_CHECK_INTERVAL)
    assert _issue(hass) is None


async def test_update_cycle_runs_the_check(hass: HomeAssistant, coordinator, clock):
    _add_scanner(coordinator, "Living Room Proxy", "aa:bb:cc:dd:ee:01", last_seen=STARTED)
    clock[0] = STARTED + 40 * 60
    coordinator._waitingfor_load_manufacturer_ids = False
    coordinator._async_update_data_internal()
    assert coordinator.stamp_last_silent_check == clock[0]
    assert _issue(hass) is not None


# A proxy dropping off the network, through the real scanner purge path.

PROXY = "aa:bb:cc:dd:ee:20"
OTHER = "aa:bb:cc:dd:ee:40"


def _ha_scanner(address, name):
    scanner = MagicMock(spec=BaseHaRemoteScanner)
    scanner.source = address.upper()
    scanner.name = name
    scanner.time_since_last_detection.return_value = 1.0
    scanner.discovered_device_timestamps = {}
    scanner.discovered_devices_and_advertisement_data = {}
    return scanner


async def _register(hass: HomeAssistant, address, name):
    """A proxy registered by the Bluetooth integration."""
    entry = MockConfigEntry(domain="bluetooth")
    entry.add_to_hass(hass)
    devreg = dr.async_get(hass)
    return devreg, devreg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, address.upper())},
        name=name,
    )


async def test_proxy_that_unregisters_is_still_reported(hass: HomeAssistant, coordinator, clock):
    devreg, proxy_entry = await _register(hass, PROXY, "Living Room Proxy")
    await _register(hass, OTHER, "Workshop Proxy")
    current = [_ha_scanner(PROXY, "Living Room Proxy"), _ha_scanner(OTHER, "Workshop Proxy")]
    manager = MagicMock()
    manager.async_current_scanners.side_effect = lambda: list(current)

    with patch.object(coordinator, "_manager", manager):
        coordinator._rebuild_scanner_list(force=True)
        proxy = coordinator.devices[PROXY]
        other = coordinator.devices[OTHER]
        assert proxy.entry_id == proxy_entry.id
        proxy.last_seen = other.last_seen = STARTED + 60 * 60

        # The proxy drops off the network: HA Bluetooth unregisters its scanner.
        current.pop(0)
        coordinator._rebuild_scanner_list()
        assert PROXY not in coordinator.scanner_list
        other.last_seen = STARTED + 100 * 60

        # It is still watched, and reported once it has been quiet long enough.
        _check_at(coordinator, clock, STARTED + 89 * 60)
        assert _issue(hass) is None
        _check_at(coordinator, clock, STARTED + 91 * 60)
        assert _listed(hass)["scannerlist"] == "- Living Room Proxy\n"

        # It comes back: it registers again and is heard, so the repair goes.
        current.insert(0, _ha_scanner(PROXY, "Living Room Proxy"))
        coordinator._rebuild_scanner_list()
        assert PROXY in coordinator.scanner_list
        proxy.last_seen = STARTED + 100 * 60
        _check_at(coordinator, clock, STARTED + 101 * 60)
        assert _issue(hass) is None

        # It drops off again and is quiet, then the owner deletes it from Home Assistant.
        current.pop(0)
        coordinator._rebuild_scanner_list()
        other.last_seen = STARTED + 140 * 60
        _check_at(coordinator, clock, STARTED + 140 * 60)
        assert _listed(hass)["scannerlist"] == "- Living Room Proxy\n"
        devreg.async_remove_device(proxy_entry.id)
        await hass.async_block_till_done()
        _check_at(coordinator, clock, STARTED + 141 * 60)
        assert _issue(hass) is None


async def test_removing_the_integration_clears_its_repairs(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    for issue_id in (REPAIR_SCANNER_SILENT, REPAIR_SCANNER_WITHOUT_AREA):
        ir.async_create_issue(
            hass, DOMAIN, issue_id, translation_key=issue_id, severity=ir.IssueSeverity.WARNING, is_fixable=False
        )

    await hass.config_entries.async_remove(setup_bermuda_entry.entry_id)
    await hass.async_block_till_done()

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, REPAIR_SCANNER_SILENT) is None
    assert registry.async_get_issue(DOMAIN, REPAIR_SCANNER_WITHOUT_AREA) is None

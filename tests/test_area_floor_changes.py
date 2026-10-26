"""Rooms and floors added or renamed after start-up are picked up without a restart."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.dispatcher import async_dispatcher_send

import custom_components.roomwise.sensor as sensor
from custom_components.roomwise.const import SIGNAL_DEVICE_NEW


async def test_first_floor_and_room_changes_reach_the_coordinator(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    assert coordinator.have_floors is False
    coordinator._scanner_init_pending = False

    ground = fr.async_get(hass).async_create("Ground Floor")
    await hass.async_block_till_done()
    living = ar.async_get(hass).async_create("Living Room")
    ar.async_get(hass).async_update(living.id, floor_id=ground.floor_id)
    await hass.async_block_till_done()
    assert coordinator.have_floors is True
    assert coordinator._scanner_init_pending is True  # proxies re-read their rooms on the next update

    coordinator._scanner_init_pending = False
    ar.async_get(hass).async_update(living.id, name="Lounge")
    await hass.async_block_till_done()
    assert coordinator._scanner_init_pending is True


async def test_floor_sensors_are_added_when_the_first_floor_appears(hass: HomeAssistant):
    listeners = []
    coordinator = SimpleNamespace(
        get_scanners=[],
        scanner_list=[],
        have_floors=False,
        sensor_created=MagicMock(),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    with ExitStack() as stack:
        for name in dir(sensor):
            if name.startswith("Bermuda") and isinstance(getattr(sensor, name), type):
                stack.enter_context(
                    patch.object(sensor, name, lambda _c, _e, address=None, scanner=None, kind=name: (kind, address))
                )
        await sensor.async_setup_entry(hass, entry, lambda entities, *_: added.extend(entities))
        async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, "aa:bb:cc:dd:ee:ff")
        await hass.async_block_till_done()
        assert ("BermudaSensorFloor", "aa:bb:cc:dd:ee:ff") not in added

        coordinator.have_floors = True
        for listener in listeners:
            listener()
            listener()
        assert added.count(("BermudaSensorFloor", "aa:bb:cc:dd:ee:ff")) == 1

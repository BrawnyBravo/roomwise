"""Area Last Seen restores its last room, and an unknown last state stays unknown."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.sensor import RestoreSensor
from homeassistant.helpers.entity import Entity

from custom_components.roomwise.sensor import BermudaSensorAreaLastSeen


@pytest.mark.parametrize(("restored", "expected"), [("Living Room", "Living Room"), (None, None)])
async def test_area_last_seen_restore(restored, expected):
    device = SimpleNamespace(area_last_seen=None, name="Phone")
    coordinator = MagicMock(devices={"aa:bb:cc:dd:ee:ff": device})
    entity = BermudaSensorAreaLastSeen(coordinator, MagicMock(), "aa:bb:cc:dd:ee:ff")
    with (
        patch.object(Entity, "async_added_to_hass", AsyncMock()),
        patch.object(RestoreSensor, "async_added_to_hass", AsyncMock()),
        patch.object(
            RestoreSensor, "async_get_last_sensor_data", AsyncMock(return_value=SimpleNamespace(native_value=restored))
        ),
    ):
        await entity.async_added_to_hass()
    assert device.area_last_seen == expected
    assert entity.native_value == expected

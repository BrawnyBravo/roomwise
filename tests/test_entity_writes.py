"""Entities only write their state when something visible changed."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.roomwise.sensor import BermudaSensor

DEVICE = "aa:bb:cc:dd:ee:ff"


async def test_unchanged_state_is_not_written_again(hass: HomeAssistant):
    device = SimpleNamespace(
        name="Phone",
        address=DEVICE,
        address_type="bd_addr_other",
        area_name="Living Room",
        area_id="living_room",
        area_icon="mdi:sofa",
        floor_id=None,
        floor_name=None,
        floor_level=None,
        adverts={},
        ref_power_changed=0,
        unique_id=DEVICE,
    )
    coordinator = MagicMock(devices={DEVICE: device}, hass=hass)
    entity = BermudaSensor(coordinator, MagicMock(), DEVICE)
    entity.hass = hass
    entity.devreg_init_done = True
    with (
        patch.object(BermudaSensor, "device_entry", None),
        patch.object(BermudaSensor, "async_write_ha_state") as write,
    ):
        for _ in range(5):
            entity._handle_coordinator_update()
        assert write.call_count == 1

        device.area_name = "Office"
        entity._handle_coordinator_update()
        entity._handle_coordinator_update()
        assert write.call_count == 2

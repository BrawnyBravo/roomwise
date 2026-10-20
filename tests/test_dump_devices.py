"""The dump_devices action: admin only, redacted by default."""

from __future__ import annotations

import json

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized

from custom_components.roomwise.const import DOMAIN

MAC = "c1:22:33:44:55:66"


async def _dump(hass, context, **data):
    return await hass.services.async_call(
        DOMAIN, "dump_devices", data, blocking=True, return_response=True, context=context
    )


async def test_non_admin_users_cannot_dump_devices(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, hass_read_only_user
):
    with pytest.raises(Unauthorized):
        await _dump(hass, Context(user_id=hass_read_only_user.id))


async def test_dump_is_redacted_unless_asked_otherwise(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, hass_admin_user
):
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._get_or_create_device(MAC)
    admin = Context(user_id=hass_admin_user.id)

    redacted = json.dumps(await _dump(hass, admin), default=str).lower()
    assert MAC not in redacted
    assert MAC.replace(":", "") not in redacted

    raw = json.dumps(await _dump(hass, admin, redact=False), default=str).lower()
    assert MAC in raw

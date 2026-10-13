"""A ref_power change must bypass the sensor cache straight away."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.roomwise import entity as entity_module
from custom_components.roomwise.entity import BermudaEntity

NOW = 10_000.0


def test_cache_is_bypassed_right_after_a_ref_power_change(monkeypatch):
    """The check compared against now + 2 s, so it could never pass."""
    monkeypatch.setattr(entity_module, "monotonic_time_coarse", lambda: NOW)
    entity = SimpleNamespace(
        bermuda_update_interval=10,
        bermuda_last_stamp=NOW,  # cache is fresh
        bermuda_last_state=1.0,
        _device=SimpleNamespace(ref_power_changed=NOW),
    )
    # A rising distance would normally be held back by the cache.
    assert BermudaEntity._cached_ratelimit(entity, 2.0) == 2.0
    # Long after the change, the cache holds a rising value back again.
    entity._device.ref_power_changed = NOW - 60
    assert BermudaEntity._cached_ratelimit(entity, 3.0) == 2.0

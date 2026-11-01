"""Clock lookup shared by the coordinator mixins."""

from __future__ import annotations

import sys

_COORDINATOR_MODULE = f"{__package__}.coordinator"


def monotonic_time_coarse() -> float:
    """
    Return coordinator.monotonic_time_coarse(), looked up on every call.

    Tests patch the clock as custom_components.roomwise.coordinator.monotonic_time_coarse;
    reading it from there keeps those patches working for code in the mixin modules.
    """
    return sys.modules[_COORDINATOR_MODULE].monotonic_time_coarse()

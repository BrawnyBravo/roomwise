"""Loading the Bluetooth manufacturer-name tables."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(name="skip_yaml_data_load", autouse=True)
def skip_yaml_data_load():
    """Override the conftest fixture: this module tests the real loader."""
    return


async def test_manufacturer_yaml_is_parsed_off_the_event_loop():
    """The 200 KB YAML tables must be parsed in an executor, not on the event loop."""
    executor_calls = []

    async def fake_executor_job(func, *args):
        executor_calls.append(func)
        # Parsing is allowed in here (this is "the executor").
        with patch.object(yaml, "safe_load", _real_safe_load), patch.object(yaml, "load", _real_load):
            return func(*args)

    _real_safe_load = yaml.safe_load
    _real_load = yaml.load

    def loop_parse(*_args, **_kwargs):
        raise AssertionError("YAML parsed on the event loop")

    coordinator = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(path=lambda rel: str(REPO_ROOT / rel)),
            async_add_executor_job=fake_executor_job,
        ),
        _waitingfor_load_manufacturer_ids=True,
    )

    with patch.object(yaml, "safe_load", loop_parse), patch.object(yaml, "load", loop_parse):
        await BermudaDataUpdateCoordinator.async_load_manufacturer_ids(coordinator)

    assert executor_calls, "no executor job was used"
    assert coordinator._waitingfor_load_manufacturer_ids is False
    assert coordinator.member_uuids[0xFEFF]  # first entry in the member table
    assert coordinator.company_uuids[0x004C] == "Apple, Inc."


def test_manufacturer_yaml_uses_the_c_loader_when_available():
    """CSafeLoader is several times faster than the pure-Python loader."""
    from custom_components.roomwise import coordinator as coordinator_module

    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert coordinator_module.YAML_LOADER is expected

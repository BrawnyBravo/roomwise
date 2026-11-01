"""Coverage for BermudaIrkManager: defensive checks, older Home Assistant, diagnostics."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import custom_components.roomwise.bermuda_irk as bermuda_irk
from custom_components.roomwise.bermuda_irk import BermudaIrkManager
from custom_components.roomwise.const import IrkTypes

from .test_irk_privacy import IRK, OTHER_IRK, resolvable_address


def test_a_key_that_cannot_make_a_cipher_is_reported_not_trusted(caplog):
    caplog.set_level(logging.ERROR)
    manager = BermudaIrkManager()
    with (
        patch.object(bermuda_irk, "get_cipher_for_irk", return_value=None),
        patch.object(bermuda_irk, "resolve_private_address", return_value=False),
    ):
        result = manager._validate_mac_irk("4a:11:22:33:44:55", IRK, None)
    assert "this is a bug" in caplog.text
    assert result == IrkTypes.NO_KNOWN_IRK_MATCH.value


def test_saving_a_different_key_than_the_one_resolved_is_reported(caplog):
    caplog.set_level(logging.ERROR)
    manager = BermudaIrkManager()
    manager.add_irk(IRK)
    address = resolvable_address(IRK)
    with patch.object(manager, "_update_saved_mac", return_value=OTHER_IRK):
        manager._validate_mac_irk(address, IRK, None)
    assert "Something went wrong saving macirk" in caplog.text


def test_older_home_assistant_builds_the_bluetooth_device_with_a_signal_value():
    manager = BermudaIrkManager()
    callback = MagicMock()
    manager.register_irk_callback(callback, IRK)
    fake_device = MagicMock()
    with (
        patch.object(bermuda_irk, "MAJOR_VERSION", 2025),
        patch.object(bermuda_irk, "MINOR_VERSION", 7),
        patch.object(bermuda_irk, "BLEDevice", return_value=fake_device) as ble_device,
    ):
        manager.fire_callbacks(IRK, "4a:11:22:33:44:55")
    ble_device.assert_called_once_with("4a:11:22:33:44:55", "", None, 0)
    callback.assert_called_once()
    assert callback.call_args.args[0].address == "4a:11:22:33:44:55"


def test_diagnostics_list_resolved_and_unmatched_addresses_but_not_unevaluated_ones():
    manager = BermudaIrkManager()
    manager.add_irk(IRK)
    resolved = resolvable_address(IRK)
    assert manager.check_mac(resolved) == IRK
    assert manager.check_mac("4a:00:00:00:00:01") == IrkTypes.NO_KNOWN_IRK_MATCH.value
    assert manager._validate_mac_irk("c1:22:33:44:55:66", IRK, None) == IrkTypes.NOT_RESOLVABLE_ADDRESS.value

    out = manager.async_diagnostics_no_redactions()

    assert out["irks"] == [IRK.hex()]
    assert set(out["macs"]) == {resolved, "4a:00:00:00:00:01"}
    assert out["macs"][resolved]["irk"] == IRK.hex()
    assert out["macs"]["4a:00:00:00:00:01"]["irk"] == IrkTypes.NO_KNOWN_IRK_MATCH.name

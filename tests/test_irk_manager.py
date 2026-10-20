"""BermudaIrkManager: address-type labels, caching, re-checks, expiry and callbacks."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import custom_components.roomwise.bermuda_irk as bermuda_irk
from custom_components.roomwise.bermuda_irk import BermudaIrkManager
from custom_components.roomwise.const import PRUNE_TIME_KNOWN_IRK, IrkTypes

from .test_irk_privacy import IRK, OTHER_IRK, resolvable_address


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("4a:11:22:33:44:55", IrkTypes.NO_KNOWN_IRK_MATCH),  # resolvable private, no key matches
        ("7f:11:22:33:44:55", IrkTypes.NO_KNOWN_IRK_MATCH),
        ("c1:22:33:44:55:66", IrkTypes.NOT_RESOLVABLE_ADDRESS),  # random static
        ("f1:22:33:44:55:66", IrkTypes.NOT_RESOLVABLE_ADDRESS),
        ("31:22:33:44:55:66", IrkTypes.NOT_RESOLVABLE_ADDRESS),  # non-resolvable private
    ],
)
def test_address_type_labels(address, expected):
    manager = BermudaIrkManager()
    manager.add_irk(IRK)
    assert manager._validate_mac_irk(address, IRK, None) == expected.value


def test_resolves_caches_and_rechecks_on_new_key():
    manager = BermudaIrkManager()
    address = resolvable_address(IRK)
    assert manager.check_mac(address) == IrkTypes.NO_KNOWN_IRK_MATCH.value
    assert manager.add_irk(IRK) == [address]  # the new key re-checks addresses already seen
    assert manager.check_mac(address) == IRK
    assert address in manager.known_macs()
    assert manager.add_irk(IRK) == []  # adding the same key again does nothing


def test_callbacks_fire_for_existing_and_new_addresses_and_unsubscribe():
    manager = BermudaIrkManager()
    first = resolvable_address(IRK)
    manager.add_irk(IRK)
    manager.check_mac(first)
    callback = MagicMock()
    unsubscribe = manager.register_irk_callback(callback, IRK)
    assert callback.call_count == 1  # told about the address already known
    manager.check_mac(resolvable_address(IRK, b"\x55\x66\x77"))
    assert callback.call_count == 2
    unsubscribe()
    manager.check_mac(resolvable_address(IRK, b"\x66\x77\x88"))
    assert callback.call_count == 2
    assert IRK not in manager._irk_callbacks


def test_prune_expires_old_addresses():
    manager = BermudaIrkManager()
    now = [1000.0]
    with patch.object(bermuda_irk, "monotonic_time_coarse", side_effect=lambda: now[0]):
        manager.check_mac(resolvable_address(OTHER_IRK))
        now[0] += PRUNE_TIME_KNOWN_IRK + 1
        manager.async_prune()
    assert manager.known_macs(resolved=False) == {}

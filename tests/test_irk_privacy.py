"""Identity resolving keys (IRKs) never reach the log, at any level."""

from __future__ import annotations

import logging

from bluetooth_data_tools import get_cipher_for_irk
from homeassistant.core import HomeAssistant

from custom_components.roomwise.bermuda_irk import BermudaIrkManager
from custom_components.roomwise.util import irk_tag

# A made-up test key (not a real device's).
IRK = bytes.fromhex("5eedc0de0123456789abcdef01234567")
OTHER_IRK = bytes.fromhex("feedface00112233445566778899aabb")


def resolvable_address(irk: bytes, prand: bytes = b"\x4a\x11\x22") -> str:
    """Build a resolvable private address for irk, the way a phone does."""
    encryptor = get_cipher_for_irk(irk).encryptor()
    ct = encryptor.update(b"\x00" * 13 + prand) + encryptor.finalize()
    return ":".join(f"{b:02x}" for b in prand + ct[13:])


def _assert_no_key_material(text: str, *irks: bytes):
    for irk in irks:
        assert irk.hex() not in text.lower()
        assert irk.hex()[:4] not in text.lower()  # not even a prefix
        assert repr(irk) not in text


def test_irk_tag_is_short_stable_and_not_the_key():
    assert irk_tag(IRK) == irk_tag(IRK.hex())
    assert irk_tag(IRK) != irk_tag(OTHER_IRK)
    assert IRK.hex()[:4] not in irk_tag(IRK)


def test_irk_manager_logs_no_key_material(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.roomwise")
    manager = BermudaIrkManager()
    address = resolvable_address(IRK)
    unknown = resolvable_address(OTHER_IRK, b"\x55\x66\x77")

    manager.check_mac(unknown)  # no keys yet
    manager.add_irk(IRK)
    assert manager.check_mac(address) == IRK  # resolves (debug logs on the way)
    manager.check_mac(address)  # cached
    manager.add_irk(OTHER_IRK)  # re-checks the unknown address, which now resolves
    manager.add_macirk("4a:00:00:00:00:01", IRK)  # does not resolve: logs a warning
    manager._validate_mac_irk(address, IRK, None)  # no prepared cipher
    manager._update_saved_mac(address, OTHER_IRK)  # "re-saving" path

    assert caplog.records, "expected the code paths above to log"
    _assert_no_key_material(caplog.text, IRK, OTHER_IRK)
    assert irk_tag(IRK) in caplog.text  # the safe tag is what identifies it


async def test_private_ble_device_logs_and_name_hold_no_key_material(
    hass: HomeAssistant, bypass_get_data, setup_bermuda_entry, caplog
):
    caplog.set_level(logging.DEBUG, logger="custom_components.roomwise")
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    device = coordinator._get_or_create_device(IRK.hex())
    device.create_sensor = True

    # Not yet named by the Private BLE Device integration.
    assert IRK.hex() not in device.name
    assert IRK.hex() not in repr(device)

    # Its rotating address is seen and resolved.
    source = coordinator._get_or_create_device(resolvable_address(IRK))
    assert source.address in device.metadevice_sources

    _assert_no_key_material(caplog.text, IRK)

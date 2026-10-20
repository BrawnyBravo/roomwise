"""Per-proxy "Distance to" sensors: one proxy without a network MAC does not block the others."""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

import custom_components.roomwise.sensor as sensor
from custom_components.roomwise.const import SCANNER_NETWORK_MAC_WAIT, SIGNAL_DEVICE_NEW

DEVICE = "aa:bb:cc:dd:ee:ff"
NOW = 10_000.0


def _scanner(address, *, remote, wifi_mac=None):
    return SimpleNamespace(address=address, is_remote_scanner=remote, address_wifi_mac=wifi_mac, first_seen=NOW)


async def test_proxy_without_network_mac_does_not_block_the_others(hass: HomeAssistant):
    usb = _scanner("aa:bb:cc:dd:ee:01", remote=False)
    esphome = _scanner("aa:bb:cc:dd:ee:02", remote=True, wifi_mac="aa:bb:cc:dd:ee:00")
    no_mac = _scanner("aa:bb:cc:dd:ee:03", remote=True)  # integration that registers no network MAC
    listeners = []
    coordinator = SimpleNamespace(
        get_scanners=[usb, esphome, no_mac],
        scanner_list=[usb.address, esphome.address, no_mac.address],
        have_floors=False,
        sensor_created=MagicMock(),
        async_add_listener=lambda cb: listeners.append(cb) or (lambda: None),
    )
    entry = MagicMock(runtime_data=SimpleNamespace(coordinator=coordinator))
    added = []
    now = [NOW]

    def made(kind):
        return lambda _c, _e, address=None, scanner=None: (kind, address, scanner)

    with ExitStack() as stack:
        for name in dir(sensor):
            if name.startswith("Bermuda") and isinstance(getattr(sensor, name), type):
                stack.enter_context(patch.object(sensor, name, made(name)))
        stack.enter_context(patch.object(sensor, "monotonic_time_coarse", side_effect=lambda: now[0], create=True))

        await sensor.async_setup_entry(hass, entry, lambda entities, *_: added.extend(entities))
        async_dispatcher_send(hass, SIGNAL_DEVICE_NEW, DEVICE)
        await hass.async_block_till_done()

        def per_proxy():
            return {e[2] for e in added if e[0] == "BermudaSensorScannerRange"}

        # The ready proxies get their sensors straight away.
        assert per_proxy() == {usb.address, esphome.address}

        # The proxy without a network MAC gets its sensors once the wait is over.
        for listener in listeners:
            listener()
        assert no_mac.address not in per_proxy()
        now[0] += SCANNER_NETWORK_MAC_WAIT + 1
        for listener in listeners:
            listener()
        assert per_proxy() == {usb.address, esphome.address, no_mac.address}

        # Nothing is created twice.
        ranges = [e for e in added if e[0] == "BermudaSensorScannerRange"]
        assert len(ranges) == 3


def test_distance_sensor_id_is_fixed_when_created():
    """A network MAC registered later must not change an existing entity's unique id."""
    scanner = SimpleNamespace(address="aa:bb:cc:dd:ee:03", address_wifi_mac=None, name="Proxy")
    device = SimpleNamespace(unique_id=DEVICE, name="Phone", ref_power=0)
    coordinator = MagicMock(devices={DEVICE: device, scanner.address: scanner})
    entity = sensor.BermudaSensorScannerRange(coordinator, MagicMock(), DEVICE, scanner.address)
    before = entity.unique_id
    scanner.address_wifi_mac = "aa:bb:cc:dd:ee:00"
    assert entity.unique_id == before

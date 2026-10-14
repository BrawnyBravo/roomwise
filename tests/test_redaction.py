"""Diagnostics / dump_devices redaction: correctness, speed, and staying off the event loop."""

from __future__ import annotations

import random
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.roomwise.bermuda_device import BermudaDevice
from custom_components.roomwise.const import CONF_DEVICES
from custom_components.roomwise.coordinator import BermudaDataUpdateCoordinator
from custom_components.roomwise.diagnostics import async_get_config_entry_diagnostics

KNOWN_A = "aa:bb:cc:dd:ee:01"
KNOWN_B = "11:22:33:44:55:66"
UNKNOWN = "de:ad:be:ef:00:99"


def _redactor(devices=(), scanners=(), configured=()):
    """A stand-in coordinator carrying just what the redaction code reads."""
    stub = SimpleNamespace(
        redactions={},
        stamp_redactions_expiry=None,
        scanner_list=list(scanners),
        options={CONF_DEVICES: [c.upper() for c in configured]},
        devices={a: SimpleNamespace(address_type="bd_addr_other") for a in devices},
        # Read by the pre-fix implementation only (kept so the "before" run is a fair test).
        _redact_generic_re=re.compile(r"(?P<start>[0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2}[:_-]){4}(?P<end>[0-9A-Fa-f]{2})"),
        _redact_generic_sub=r"\g<start>:xx:xx:xx:xx:\g<end>",
    )
    stub.redaction_list_update = lambda: BermudaDataUpdateCoordinator.redaction_list_update(stub)
    return stub


def _redact(stub, data):
    # Bind the real method to the stub; works whether or not it uses helpers.
    stub.redact_data = lambda d, first_recursion=True: BermudaDataUpdateCoordinator.redact_data(
        stub, d, first_recursion
    )
    return stub.redact_data(data)


def _all_forms(mac):
    bare = mac.replace(":", "")
    return [mac, mac.upper(), bare, bare.upper(), mac.replace(":", "-"), mac.replace(":", "_"), mac.replace(":", ".")]


def test_every_address_in_a_string_is_redacted():
    """Several addresses in one string must all be redacted, in every format.

    The old code restarted from the original string for each replacement, so only
    the last known address was replaced; a bare (no-separator) known address then
    survived in full, and unknown bare or dotted addresses were never caught.
    """
    stub = _redactor(devices=[KNOWN_A], scanners=[KNOWN_B])
    for known_a in _all_forms(KNOWN_A):
        for known_b in _all_forms(KNOWN_B):
            for unknown in _all_forms(UNKNOWN):
                text = f"Device {known_a} seen by {known_b}, also {unknown}."
                out = _redact(stub, text)
                low = out.lower()
                for mac in (KNOWN_A, KNOWN_B, UNKNOWN):
                    for form in _all_forms(mac):
                        assert form.lower() not in low, (text, out)
                # Known addresses keep their stable pseudonyms.
                assert "::OTHER_MAC_" in out
                assert "::SCANNER_" in out


def test_redaction_keeps_the_rest_of_the_text_unchanged():
    """Only the address tokens change; the surrounding text keeps its case."""
    stub = _redactor(devices=[KNOWN_A])
    out = _redact(stub, f"Seen By Proxy {KNOWN_A.upper()} At RSSI -60")
    assert out.startswith("Seen By Proxy aa::OTHER_MAC_")
    assert out.endswith("::01 At RSSI -60")


def test_redaction_covers_keys_nested_containers_and_irk_like_tokens():
    """Dict keys, lists, tuples and 32-hex (IRK-like) tokens are redacted too."""
    irk = "0123456789abcdef0123456789abcdef"
    stub = _redactor(devices=[KNOWN_A])
    data = {
        f"{KNOWN_A}__{KNOWN_B}": [KNOWN_A.upper(), (UNKNOWN,), {"irk": irk.upper()}],
        "count": 3,
    }
    out = repr(_redact(stub, data)).lower()
    for mac in (KNOWN_A, KNOWN_B, UNKNOWN):
        assert mac not in out
        assert mac.replace(":", "") not in out
    assert irk not in out
    assert "'count': 3" in out


def _synthetic_dump(n_devices, n_scanners=3):
    """Dump shaped like dump_devices + bt_manager diagnostics for n devices."""
    rng = random.Random(1)

    def mac():
        return ":".join(f"{rng.randint(0, 255):02x}" for _ in range(6))

    scanners = [mac() for _ in range(n_scanners)]
    devices = [mac() for _ in range(n_devices)]
    dump = {}
    for d in devices:
        entry = {f"field_{k}": f"value text {k}" for k in range(25)}
        entry.update({f"num_{k}": 1.5 for k in range(15)})
        entry["name"] = f"roomwise_{d.replace(':', '_')}"
        entry["address"] = d
        adverts = {}
        for s in scanners:
            adv = {f"a_{k}": f"txt {k}" for k in range(12)}
            adv.update({f"h_{k}": [1.0] * 10 for k in range(6)})
            adv["scanner_address"] = s
            adv["device_address"] = d
            adv["_device"] = f"name [{d}]"
            adverts[f"{d}__{s}"] = adv
        entry["adverts"] = adverts
        dump[d] = entry
    bt = {
        s: {
            "discovered": [
                {
                    "address": d.upper(),
                    "name": d.upper(),
                    "details": {"source": s.upper()},
                    "m": {"76": "02" + "ab" * 20},
                }
                for d in devices
            ]
        }
        for s in scanners
    }
    return scanners, devices, {"devices": dump, "bt_manager": bt}


def test_redacting_1000_devices_is_fast_and_leak_free():
    """1000 devices redact in well under a second (was ~50 s: quadratic in device count)."""
    scanners, devices, data = _synthetic_dump(1000)
    stub = _redactor(devices=devices, scanners=scanners, configured=devices[:10])

    start = time.perf_counter()
    out = _redact(stub, data)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"redaction took {elapsed:.2f} s"
    text = repr(out).lower()
    leaks = [d for d in devices + scanners if d in text or d.replace(":", "") in text]
    assert leaks == []


def test_device_dump_omits_options_and_live_stamps():
    """Each device's dump must not embed the whole options dict or the live stamps table."""
    coordinator = MagicMock()
    coordinator.options = {CONF_DEVICES: [KNOWN_A.upper()], "rssi_offsets": {KNOWN_B: 3}}
    coordinator.hass_version_min_2025_4 = True
    device = BermudaDevice(KNOWN_B, coordinator)
    device.stamps = {KNOWN_A.upper(): 123.0, UNKNOWN.upper(): 456.0}

    out = device.to_dict()

    assert "options" not in out
    assert out["stamps"] == 2  # just the count


async def test_diagnostics_redaction_runs_in_the_executor(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    """The redaction work must run in an executor job, not on the event loop."""
    coordinator = setup_bermuda_entry.runtime_data.coordinator
    coordinator._get_or_create_device(KNOWN_A)

    real_job = hass.async_add_executor_job
    jobs = []

    def spy(func, *args):
        jobs.append(func)
        return real_job(func, *args)

    with (
        patch.object(hass, "async_add_executor_job", side_effect=spy),
        patch.object(coordinator, "redact_data", side_effect=AssertionError("redacted on the event loop")),
    ):
        diag = await async_get_config_entry_diagnostics(hass, setup_bermuda_entry)

    assert jobs, "no executor job was used"
    text = repr(diag).lower()
    assert KNOWN_A not in text
    assert KNOWN_A.replace(":", "") not in text
    assert "devices" in diag
    assert "bt_manager" in diag


def _advertisement_data(rssi=-60):
    """A real bleak AdvertisementData (a NamedTuple), as found in HA's Bluetooth diagnostics."""
    from bleak.backends.scanner import AdvertisementData

    return AdvertisementData(
        local_name=f"Sensor {KNOWN_A}",
        manufacturer_data={76: b"\x02\x15"},
        service_data={"0000fe9f-0000-1000-8000-00805f9b34fb": b"\x00"},
        service_uuids=["0000fe9f-0000-1000-8000-00805f9b34fb"],
        tx_power=None,
        rssi=rssi,
        platform_data=(KNOWN_A.upper(), {"source": KNOWN_B}),
    )


def test_snapshot_and_redaction_keep_namedtuples_intact():
    """NamedTuples (bleak AdvertisementData) must be rebuilt field by field, not from one iterable."""
    from typing import NamedTuple

    from custom_components.roomwise.util import redact_addresses, snapshot_data

    class Pair(NamedTuple):
        address: str
        rssi: int

    advert = _advertisement_data()
    data = {
        "all_history": [{"address": KNOWN_A.upper(), "advertisement": advert}],
        "pair": Pair(KNOWN_A, -70),
        "plain": (KNOWN_A, 1),
        "set": frozenset({KNOWN_A}),
    }

    snap = snapshot_data(data)
    assert snap == data
    assert type(snap["all_history"][0]["advertisement"]) is type(advert)
    assert type(snap["pair"]) is Pair

    out = redact_addresses(snap, {})
    red_advert = out["all_history"][0]["advertisement"]
    assert type(red_advert) is type(advert)
    assert red_advert.rssi == -60
    assert red_advert.manufacturer_data == {76: b"\x02\x15"}
    assert KNOWN_A not in red_advert.local_name.lower()
    assert type(out["pair"]) is Pair
    assert out["pair"].rssi == -70
    assert type(out["plain"]) is tuple
    text = repr(out).lower()
    assert KNOWN_A not in text
    assert KNOWN_A.replace(":", "") not in text
    assert KNOWN_B not in text


async def test_diagnostics_with_bluetooth_advert_history(hass: HomeAssistant, bypass_get_data, setup_bermuda_entry):
    """Download diagnostics works when HA's Bluetooth manager holds real advert history."""
    from bleak.backends.device import BLEDevice
    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.components.bluetooth.api import _get_manager

    coordinator = setup_bermuda_entry.runtime_data.coordinator
    advert = _advertisement_data()
    info = BluetoothServiceInfoBleak(
        name=advert.local_name,
        address=KNOWN_A.upper(),
        rssi=advert.rssi,
        manufacturer_data=advert.manufacturer_data,
        service_data=advert.service_data,
        service_uuids=advert.service_uuids,
        source=KNOWN_B.upper(),
        device=BLEDevice(KNOWN_A.upper(), advert.local_name, {"source": KNOWN_B.upper()}),
        advertisement=advert,
        connectable=False,
        time=time.monotonic(),
        tx_power=None,
    )
    _get_manager(hass).scanner_adv_received(info)

    bt_diags = await coordinator._manager.async_diagnostics()  # noqa: SLF001
    assert bt_diags["all_history"], "advert was not recorded by the Bluetooth manager"

    diag = await async_get_config_entry_diagnostics(hass, setup_bermuda_entry)

    history = diag["bt_manager"]["all_history"]
    assert history
    assert history[0]["advertisement"].rssi == -60
    text = repr(diag).lower()
    assert KNOWN_A not in text
    assert KNOWN_A.replace(":", "") not in text

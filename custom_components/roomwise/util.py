"""General helper utilities for Roomwise."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .const import OPTION_DEFAULTS, OPTION_RANGES


@lru_cache(64)
def mac_math_offset(mac, offset=0) -> str | None:
    """
    Perform addition/subtraction on a MAC address.

    With a MAC address in xx:xx:xx:xx:xx:xx format,
    add the offset (which may be negative) to the
    last octet, and return the full new MAC.
    If the resulting octet is outside of 00-FF then
    the function returns None.
    """
    if mac is None:
        return None
    octet = mac[-2:]
    try:
        octet_int = bytes.fromhex(octet)[0]
    except ValueError:
        return None
    if 0 <= (octet_new := octet_int + offset) <= 255:
        return f"{mac[:-3]}:{(octet_new):02x}"
    return None


@lru_cache(1024)
def mac_norm(mac: str) -> str:
    """
    Format the mac address string for entry into dev reg.

    What is returned is always lowercased, regardless of
    detected form.
    If mac is an identifiable MAC-address, it's returned
    in the xx:xx:xx:xx:xx:xx form.

    This is copied from the HA device_registry's
    format_mac, but with a bigger lru cache and some
    tweaks, since we're often dealing with many addresses.
    """
    to_test = mac

    if len(to_test) == 17:
        if to_test.count(":") == 5:
            return to_test.lower()
        if to_test.count("-") == 5:
            return to_test.replace("-", ":").lower()
        if to_test.count("_") == 5:
            return to_test.replace("_", ":").lower()

    elif len(to_test) == 14 and to_test.count(".") == 2:
        to_test = to_test.replace(".", "")

    if len(to_test) == 12:
        # no : included
        return ":".join(to_test.lower()[i : i + 2] for i in range(0, 12, 2))

    # Not sure how formatted, return original
    return mac.lower()


@lru_cache(2048)
def mac_explode_formats(mac: str) -> set[str]:
    """
    Take a formatted mac address and return the formats
    likely to be found in our device info, adverts etc
    by replacing ":" with each of "", "-", "_", ".".
    """
    altmacs = set()
    altmacs.add(mac)
    for newsep in ["", "-", "_", "."]:
        altmacs.add(mac.replace(":", newsep))
    return altmacs


_ADDRESS_LIKE_NAME = re.compile(r"(?:[a-z0-9_]*_)?(?:[0-9a-f]{2}[:_.-]?){5}[0-9a-f]{2}")


def name_is_address(name: str | None) -> bool:
    """
    True if a name is empty or just an address, so not something to show a person.

    Catches "aa:bb:cc:00:00:01" (a device named after its network address by,
    say, a router integration) and fallbacks like "roomwise_aa_bb_cc_00_00_01".
    """
    if not name:
        return True
    return _ADDRESS_LIKE_NAME.fullmatch(str(name).strip().lower()) is not None


def mac_redact(mac: str, tag: str | None = None) -> str:
    """Remove the centre octets of a MAC and optionally replace with a tag."""
    if tag is None:
        tag = ":"
    return f"{mac[:2]}::{tag}::{mac[-2:]}"


@lru_cache(1024)
def rssi_to_metres(rssi, ref_power=None, attenuation=None):
    """
    Convert instant rssi value to a distance in metres.

    Based on the information from
    https://mdpi-res.com/d_attachment/applsci/applsci-10-02003/article_deploy/applsci-10-02003.pdf?version=1584265508

    attenuation:    a factor representing environmental attenuation
                    along the path. Will vary by humidity, terrain etc.
    ref_power:      db. measured rssi when at 1m distance from rx. The will
                    be affected by both receiver sensitivity and transmitter
                    calibration, antenna design and orientation etc.
    """
    if ref_power is None:
        return False
        # ref_power = self.ref_power
    if attenuation is None:
        return False
        # attenuation= self.attenuation

    return 10 ** ((ref_power - rssi) / (10 * attenuation))


def option_in_range(key: str, value: Any) -> bool:
    """True if value is a number inside the allowed range for this global option."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    low, high = OPTION_RANGES.get(key, (None, None))
    return (low is None or value >= low) and (high is None or value <= high)


def sanitize_options(options: dict) -> list[str]:
    """
    Replace saved global option values that are out of range with their defaults, in place.

    The options form refuses such values, but one saved before it did (or edited
    by hand) must not crash the distance maths or auto-tune. Returns the keys
    that were replaced.
    """
    fixed = []
    for key, default in OPTION_DEFAULTS.items():
        if key in options and not option_in_range(key, options[key]):
            options[key] = default
            fixed.append(key)
    return fixed


@lru_cache(256)
def clean_charbuf(instring: str | None) -> str:
    """
    Some people writing C on bluetooth devices seem to
    get confused between char arrays, strings and such. This
    function takes a potentially dodgy charbuf from a bluetooth
    device and cleans it of leading/trailing cruft
    and returns what's left, up to the first null, if any.

    If given None it returns an empty string.
    Characters trimmed are space, tab, CR, LF, NUL.
    """
    if instring is not None:
        return instring.strip(" \t\r\n\x00").split("\0")[0]
    return ""


# One pass finds every address-like token in a string: a MAC with any one of the
# separators ":", "-", "_" or "." (used consistently), a 32-hex IRK or iBeacon UUID
# (optionally followed by an iBeacon "_major_minor"), or a bare 12-hex MAC. Tokens
# must not be part of a longer run of hex digits.
_REDACT_TOKEN_RE = re.compile(
    r"(?<![0-9a-f])(?:"
    r"(?P<mac>[0-9a-f]{2}(?P<sep>[:._-])(?:[0-9a-f]{2}(?P=sep)){4}[0-9a-f]{2})"
    r"|(?P<hex32>[0-9a-f]{32})(?P<suffix>_[0-9a-f]*_[0-9a-f]*)?"
    r"|(?P<bare>[0-9a-f]{12})"
    r")(?![0-9a-f])",
    re.IGNORECASE,
)


def _rebuild_sequence(original: tuple | set | frozenset, items: list[Any]) -> Any:
    """
    Build a container of the same kind as original from already-copied items.

    NamedTuples (for example bleak's AdvertisementData, which HA's Bluetooth
    diagnostics contain) take one positional argument per field, not a single
    iterable. Any other tuple subclass we cannot rebuild becomes a plain tuple.
    """
    kind = type(original)
    if hasattr(original, "_fields"):
        return kind(*items)
    if kind in (tuple, set, frozenset):
        return kind(items)
    try:
        return kind(items)
    except TypeError:
        return frozenset(items) if isinstance(original, (set, frozenset)) else tuple(items)


def snapshot_data(data: Any) -> Any:
    """
    Copy the containers (dict, list, tuple, set) of a data structure.

    Leaves are shared, containers are new. Use on the event loop before handing
    the data to an executor job, so the job never iterates a live dict or set
    that the loop is changing.
    """
    if isinstance(data, dict):
        return {k: snapshot_data(v) for k, v in data.items()}
    if isinstance(data, list):
        return [snapshot_data(v) for v in data]
    if isinstance(data, (tuple, set, frozenset)):
        return _rebuild_sequence(data, [snapshot_data(v) for v in data])
    return data


def redact_addresses(data: Any, redactions: dict[str, str]) -> Any:
    """
    Return a copy of data with every MAC address, IRK and iBeacon id redacted.

    redactions maps lower-case address strings (in every format we emit) to
    stable pseudonyms. Each address-like token found in a string is looked up in
    it; tokens not in the table are still redacted generically. The cost is
    linear in the size of the data (one regex pass per string plus a dict lookup
    per token), regardless of how many entries the table holds.

    Pure function with no shared state, so it can run in an executor job.
    """

    def _token(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        if (fix := redactions.get(token)) is not None:
            return fix
        if (hex32 := match.group("hex32")) is not None:
            suffix = match.group("suffix") or ""
            if (fix := redactions.get(hex32.lower())) is not None:
                return f"{fix}{suffix}"
            # Unknown IRK / UUID: keep the first 4 chars, as the table entries do.
            return f"{token[:4]}::REDACTED{suffix}"
        # Unknown MAC (any format).
        mac = match.group("mac") or match.group("bare")
        return f"{mac[:2].lower()}:xx:xx:xx:xx:{mac[-2:].lower()}"

    def _redact(item: Any) -> Any:
        if isinstance(item, str):
            if (fix := redactions.get(item.lower())) is not None:
                # Whole-string match (also covers table entries that are not
                # address-shaped, which the token pattern cannot find).
                return fix
            return _REDACT_TOKEN_RE.sub(_token, item)
        if isinstance(item, dict):
            return {_redact(k): _redact(v) for k, v in item.items()}
        if isinstance(item, list):
            return [_redact(v) for v in item]
        if isinstance(item, (tuple, set, frozenset)):
            return _rebuild_sequence(item, [_redact(v) for v in item])
        return _redact_object(item)

    def _redact_object(item: Any) -> Any:
        if item is None or isinstance(item, (bool, int, float, bytes, bytearray)):
            return item
        # Any other object (for example a bleak BLEDevice in HA's Bluetooth
        # diagnostics) is written to the diagnostics file as its repr(). If
        # that repr holds an address, hand back the redacted text instead.
        try:
            text = repr(item)
        except Exception:  # noqa: BLE001 - a broken __repr__ must not stop diagnostics
            return item
        redacted = _REDACT_TOKEN_RE.sub(_token, text)
        return redacted if redacted != text else item

    return _redact(data)

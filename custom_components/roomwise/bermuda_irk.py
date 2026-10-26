"""BermudaIrkManager for handling IRK to MAC mappings."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from math import floor
from typing import TYPE_CHECKING, NamedTuple

from bleak.backends.device import BLEDevice
from bluetooth_data_tools import get_cipher_for_irk, monotonic_time_coarse, resolve_private_address
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION

from .const import _LOGGER, DOMAIN, PRUNE_TIME_KNOWN_IRK, IrkTypes
from .util import irk_tag

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers import Cipher
    from homeassistant.components.bluetooth import BluetoothCallback

type Cancellable = Callable[[], None]


def _result_label(value: bytes) -> str:
    """Log label for a check result: the IrkTypes name, or a tag for a real IRK (never the key)."""
    for irk_type in IrkTypes:
        if value == irk_type.value:
            return irk_type.name
    return irk_tag(value)


class ResolvableMAC(NamedTuple):
    """Stores a mac address along with its IRK and expiry time."""

    mac: str
    expires: int
    irk: bytes


class BermudaIrkManager:
    """
    Manager for IRK resolution in Bermuda.

    - add_irk() as each IRK is learned
    - check_mac() whenever (results are cached)
    """

    def __init__(self, in_use: Callable[[], Iterable[str]] | None = None) -> None:
        # Optional: returns the addresses still being heard, which async_prune keeps.
        self._in_use = in_use
        self._irks: dict[bytes, Cipher] = {}
        self._macs: dict[str, ResolvableMAC] = {}
        self._irk_callbacks: dict[bytes, list[BluetoothCallback]] = {}

    def add_irk(self, irk: bytes) -> list[str]:
        """Adds an IRK to the internal list. Returns matching MACs, if any."""
        macs = []
        if irk not in self._irks:
            # Save new irk and cipher
            self._irks[irk] = cipher = get_cipher_for_irk(irk)
            # Check any previously unknown MACs for matches and update them.

            macs.extend(
                macirk.mac
                for macirk in self._macs.values()
                if (
                    macirk.irk in IrkTypes.unresolved()
                    and self._validate_mac_irk(macirk.mac, irk, cipher) not in IrkTypes.unresolved()
                )
            )

            _LOGGER.debug("New IRK %s matches %d of %d existing MACs", irk_tag(irk), len(macs), len(self._macs))
        return macs

    def known_macs(self, resolved=True) -> dict[str, ResolvableMAC]:
        """
        Returns a list of ResolvableMAC tuples.

        By default only the resolved MACs will be returned, but setting
        resolved=False will return all learned MACs.
        """
        if resolved:
            return {macirk.mac: macirk for macirk in self._macs.values() if macirk.irk not in IrkTypes.unresolved()}
        # otherwise, all of 'em
        return self._macs.copy()

    def async_prune(self, in_use: Iterable[str] | None = None):
        """
        Check for expired MACs and expunge them.

        We cannot know if an old MAC will return, so we keep them around for the
        max permissable time (according to the Bluetooth spec), then let them go.

        Addresses in in_use (still being heard) get a fresh expiry instead: an
        address heard for longer than the expiry is still the device's current
        one, and a Private BLE device added later must be able to match it.
        """
        nowstamp = monotonic_time_coarse()
        new_expiry = floor(nowstamp + PRUNE_TIME_KNOWN_IRK)
        if in_use is None:
            in_use = self._in_use() if self._in_use is not None else ()
        for address in in_use:
            if (macirk := self._macs.get(address)) is not None:
                self._macs[address] = macirk._replace(expires=new_expiry)
        expired = [macirk.mac for macirk in self._macs.values() if macirk.expires < nowstamp]
        for address in expired:
            del self._macs[address]
        expired_count = len(expired)
        _LOGGER.debug("BermudaIrks expired %d of %d MACs from cache", expired_count, expired_count + len(self._macs))

    def check_mac(self, address: str) -> bytes:
        """
        Checks if the MAC is a match against any known IRKs.

        Returns either a known IRK or one of IrkTypes.
        """
        # Already exists?
        if macirk := self._macs.get(address):
            return macirk.irk
        # Do the math
        return self._validate_mac(address)

    def add_macirk(self, address: str, irk: bytes) -> bytes:
        """Insert a new IRK and MAC that have already been validated."""
        self.add_irk(irk)
        result = self.check_mac(address)
        if result in IrkTypes.unresolved():
            _LOGGER.warning(
                "New Mac and IRK (%s, %s) from add_macirk do not resolve, result %s",
                address,
                irk_tag(irk),
                _result_label(result),
            )
        return result

    def _validate_mac(self, address: str) -> bytes:
        """
        Validate MAC against all known IRKs.

        Returns the IRK if found, otherwise an IrkType
        """
        for irk, cipher in self._irks.items():
            result = self._validate_mac_irk(address, irk, cipher)
            if result == irk:
                return irk
        # Failed to match anything, we should save it so we know.
        return self._update_saved_mac(address, IrkTypes.NO_KNOWN_IRK_MATCH.value)

    def _validate_mac_irk(self, address: str, irk: bytes, cipher: Cipher | None) -> bytes:
        """
        Checks address against a given IRK.

        Returns the matching IRK on success, or an IrkType
        """
        if not cipher:
            cipher = self._irks.get(irk, get_cipher_for_irk(irk))
            if cipher is None:
                _LOGGER.error(
                    "_validate_mac_irk called without prepared cipher for %s %s - this is a bug", address, irk_tag(irk)
                )
        if resolve_private_address(cipher, address):
            _LOGGER.debug(
                "######======---- Found new valid MAC for irk %s - %s. Sending callbacks", irk_tag(irk), address
            )
            result = self._update_saved_mac(address, irk)
            if result != irk:
                _LOGGER.error(
                    "Something went wrong saving macirk: %s %s is not irk %s",
                    address,
                    _result_label(result),
                    irk_tag(irk),
                )
            self.fire_callbacks(irk, address)
            return result
        # A resolvable private address has 0b01 as its two top bits (first hex
        # digit 4-7); "& 0x04" also matched random static addresses (C-F).
        if (int(address[0:2], 16) >> 6) == 0b01:
            _LOGGER.debug("IRK does not resolve %s with %s", address, irk_tag(irk))
            return self._update_saved_mac(address, IrkTypes.NO_KNOWN_IRK_MATCH.value)
        else:
            return self._update_saved_mac(address, IrkTypes.NOT_RESOLVABLE_ADDRESS.value)

    def _update_saved_mac(self, address: str, irk: bytes) -> bytes:
        """Save an IRK result against the given MAC."""
        if (macirk := self._macs.get(address, None)) is None:
            # No existing, save anew.
            expiry = floor(monotonic_time_coarse() + PRUNE_TIME_KNOWN_IRK)
            self._macs[address] = ResolvableMAC(address, expiry, irk)
            _LOGGER.debug("Saved NEW Macirk pair: %s %s", address, _result_label(irk))
            return irk

        if macirk.irk != irk:
            _LOGGER.debug(
                "RE-saving macirk for mac %s, old irk %s, new irk %s",
                address,
                _result_label(macirk.irk),
                _result_label(irk),
            )
            # Replace the entry with a new macirk.
            self._macs[address] = ResolvableMAC(address, macirk.expires, irk)
            return irk
        else:
            _LOGGER.debug("No change to macirk %s %s", macirk.mac, _result_label(macirk.irk))
            return irk

    def fire_callbacks(self, irk, mac) -> None:
        """
        Fire all callbacks for the given irk advising it of the MAC.

        This generates a bogus BluetoothServiceInfoBleak and Change event
        so that we can fake the PrivateBleDevice callbacks for an easy win.
        """
        # Create bare-shell classes to satisfy the callback signature
        # v1.0.1 Jul 1 2025 restores kwargs (but not rssi)
        # v1.0.0 jun 29 2025 (pretty sure) removes rssi and kwargs from BLEDevice.__init__()
        #
        # TODO: Set HA_MINVER to 2025.8 at some point, and remove the bogus rssi param.
        # FIXME: Ideally though, fix this so we aren't using bt's callbacks.

        # HA version when BLEDevice went from 4+ params to 3 (bleak 1.0.0, 1.0.1)
        if MAJOR_VERSION > 2025 or (MAJOR_VERSION == 2025 and MINOR_VERSION >= 8):
            bledevice = BLEDevice(mac, "", None)  # type: ignore
        else:
            # Include the rssi if we are on an older release.
            bledevice = BLEDevice(mac, "", None, 0)  # type: ignore
        service_info = BluetoothServiceInfoBleak("", mac, 0, {}, {}, [], DOMAIN, bledevice, None, False, False, 0)

        if callbacks := self._irk_callbacks.get(irk):
            for cb in callbacks:
                cb(service_info, BluetoothChange.ADVERTISEMENT)

    def register_irk_callback(self, callback: BluetoothCallback, irk: bytes) -> Cancellable:
        """
        Register to receive a callback when a newly resolved mac is available.

        The callback will be called for each mac that newly matches an IRK, either
        because the MAC is new, the IRK is new, or the callback is new.

        Returns a callback that can be used to cancel the registration.
        """
        # Register the irk if we haven't already
        self.add_irk(irk)

        # Register the new callback
        callbacks = self._irk_callbacks.setdefault(irk, [])
        callbacks.append(callback)

        # Call it for any existing MACs that match
        for macirk in self._macs.values():
            if macirk.irk == irk:
                self.fire_callbacks(irk, macirk.mac)

        def _unsubscribe() -> None:
            callbacks.remove(callback)
            if not callbacks:
                self._irk_callbacks.pop(irk, None)

        return _unsubscribe

    def async_diagnostics_no_redactions(self):
        """Return diagnostic info. Make sure to run redactions over the results."""
        nowstamp = monotonic_time_coarse()
        macs = {}
        for macirk in self._macs.values():
            if macirk.irk not in [IrkTypes.ADRESS_NOT_EVALUATED.value, IrkTypes.NOT_RESOLVABLE_ADDRESS.value]:
                if macirk.irk == IrkTypes.NO_KNOWN_IRK_MATCH.value:
                    irkout = IrkTypes.NO_KNOWN_IRK_MATCH.name
                else:
                    irkout = macirk.irk.hex()
                macs[macirk.mac] = {"irk": irkout, "expires_in": floor(macirk.expires - nowstamp)}

        return {
            "irks": [irk.hex() for irk in self._irks],
            "macs": macs,
        }

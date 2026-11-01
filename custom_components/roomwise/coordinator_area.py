"""Room selection for each device, used by the coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .const import (
    AREA_EXIT_RADIUS_FACTOR,
    AREA_EXIT_SECONDS,
    AREA_MAX_AD_AGE,
    CONF_MAX_RADIUS,
    CONF_SCANNER_MAX_RADIUS,
    DEFAULT_MAX_RADIUS,
)
from .coordinator_clock import monotonic_time_coarse

if TYPE_CHECKING:
    from .bermuda_advert import BermudaAdvert
    from .bermuda_device import BermudaDevice


class AreaMixin:
    """Coordinator methods that pick the area (room) for each device."""

    def resolve_area_name(self, area_id) -> str | None:
        """
        Given an area_id, return the current area name.

        Will return None if the area id does *not* resolve to a single
        known area name.
        """
        areas = self.ar.async_get_area(area_id)
        if hasattr(areas, "name"):
            return getattr(areas, "name", "invalid_area")
        return None

    def _refresh_areas_by_min_distance(self):
        """Set area for ALL devices based on closest beacon."""
        for device in self.devices.values():
            if (
                # device.is_scanner is not True  # exclude scanners.
                device.create_sensor  # include any devices we are tracking
                # or device.metadevice_type in METADEVICE_SOURCETYPES  # and any source devices for PBLE, ibeacon etc
            ):
                self._refresh_area_by_min_distance(device)

    @dataclass
    class AreaTests:
        """
        Holds the results of Area-based tests.

        Likely to become a stand-alone class for performing the whole area-selection
        process.
        """

        device: str = ""
        scannername: tuple[str, str] = ("", "")
        areas: tuple[str, str] = ("", "")
        pcnt_diff: float = 0  # distance percentage difference.
        same_area: bool = False  # The old scanner is in the same area as us.
        # last_detection: tuple[float, float] = (0, 0)  # bt manager's last_detection field. Compare with ours.
        last_ad_age: tuple[float, float] = (0, 0)  # seconds since we last got *any* ad from scanner
        this_ad_age: tuple[float, float] = (0, 0)  # how old the *current* advert is on this scanner
        distance: tuple[float, float] = (0, 0)
        hist_min_max: tuple[float, float] = (0, 0)  # min/max distance from history
        # velocity: tuple[float, float] = (0, 0)
        # last_closer: tuple[float, float] = (0, 0)  # since old was closer and how long new has been closer
        reason: str | None = None  # reason/result

        def sensortext(self) -> str:
            """Return a text summary suitable for use in a sensor entity."""
            out = ""
            for var, val in vars(self).items():
                out += f"{var}|"
                if isinstance(val, tuple):
                    for v in val:
                        if isinstance(v, float):
                            out += f"{v:.2f}|"
                        else:
                            out += f"{v}"
                    # out += "\n"
                elif var == "pcnt_diff":
                    out += f"{val:.3f}"
                else:
                    out += f"{val}"
                out += "\n"
            return out[:255]

        def __str__(self) -> str:
            """
            Create string representation for easy debug logging/dumping
            and potentially a sensor for logging Area decisions.
            """
            out = ""
            for var, val in vars(self).items():
                out += f"** {var:20} "
                if isinstance(val, tuple):
                    for v in val:
                        if isinstance(v, float):
                            out += f"{v:.2f} "
                        else:
                            out += f"{v} "
                    out += "\n"
                elif var == "pcnt_diff":
                    out += f"{val:.3f}\n"
                else:
                    out += f"{val}\n"
            return out

    def _refresh_area_by_min_distance(self, device: BermudaDevice):
        """Very basic Area setting by finding closest proxy to a given device."""
        # The current area_scanner (which might be None) is the one to beat.
        incumbent: BermudaAdvert | None = device.area_advert

        _max_radius = self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS)
        _scanner_radius = self.options.get(CONF_SCANNER_MAX_RADIUS) or {}  # set by auto-tune
        nowstamp = monotonic_time_coarse()

        def _range_limit(advert: BermudaAdvert) -> float:
            # A per-receiver limit can only shrink the range: if the global max
            # radius was lowered after auto-tune saved a larger value, the global wins.
            return min(_scanner_radius.get(advert.scanner_address, _max_radius), _max_radius)

        def _beyond_range(advert: BermudaAdvert) -> bool:
            return advert.rssi_distance is not None and advert.rssi_distance > _range_limit(advert)

        # The current room's receiver must still be in range. Without this, a
        # device that once came within range of a receiver (a noise dip near a
        # trimmed receiver's cut-off) would stay assigned to it for as long as no
        # other receiver hears it, ignoring both the global and the per-receiver
        # max radius.
        #
        # It gets an exit margin, though: challengers must be within the plain
        # limit to win, but the incumbent is only released once it reads beyond
        # limit x AREA_EXIT_RADIUS_FACTOR, or has stayed beyond the plain limit
        # for AREA_EXIT_SECONDS. A hard cut-off at the same distance both ways
        # made devices whose smoothed readings sit near the edge flip between
        # the room and "unplaced" hundreds of times an hour. While it is held
        # inside that margin the incumbent is "weak": any valid challenger takes
        # over without the usual switching hysteresis, so the margin only ever
        # delays "unplaced", never a move to a receiver that is in range.
        incumbent_held = False
        if incumbent is not None and _beyond_range(incumbent):
            beyond = device.area_beyond_range
            if beyond is None or beyond[0] != incumbent.scanner_address:
                beyond = (incumbent.scanner_address, nowstamp)
            if (
                incumbent.rssi_distance > _range_limit(incumbent) * AREA_EXIT_RADIUS_FACTOR
                or nowstamp - beyond[1] >= AREA_EXIT_SECONDS
            ):
                incumbent = None
            else:
                incumbent_held = True
        device.area_beyond_range = beyond if incumbent_held else None

        tests = self.AreaTests()
        tests.device = device.name

        for challenger in device.adverts.values():
            # Check each scanner and any time one is found to be closer / better than
            # the existing closest_scanner, replace it. At the end we should have the
            # right one. In theory.
            #
            # Note that rssi_distance is smoothed/filtered, and might be None if the last
            # reading was old enough that our algo decides it's "away".
            #
            # Every loop, every test is just a two-way race.

            # Is the challenger an invalid contender?
            if (
                # no competing against ourselves...
                incumbent is challenger  # no competing against ourselves.
            ):
                continue

            # No winning with stale adverts. If we didn't win back when it was fresh,
            # we've no business winning now. This guards against a single advert
            # being reported by two proxies at slightly different times, and the area
            # switching to the later one after the reading times out on the first.
            # The timeout value is fairly arbitrary, if it's too small then we risk
            # ignoring valid reports from slow proxies (or if our processing loop is
            # delayed / lengthened). Too long and we add needless jumping around for a
            # device that isn't actually being actively detected.
            if challenger.stamp < nowstamp - AREA_MAX_AD_AGE:
                # our ad is too old.
                continue

            # If we are too far away or don't have an area, we cannot win...
            if challenger.rssi_distance is None or _beyond_range(challenger) or challenger.area_id is None:
                continue

            # At this point the challenger is a vaild contender...

            # An incumbent kept only by the exit margin yields to any valid challenger.
            if incumbent_held:
                tests.scannername = (incumbent.name, challenger.name)
                tests.areas = (incumbent.area_name or "", challenger.area_name or "")
                tests.distance = (incumbent.rssi_distance, challenger.rssi_distance)
                tests.reason = "WIN against an incumbent beyond its max radius"
                incumbent = challenger
                incumbent_held = False
                continue

            # Is the incumbent a valid contender?

            # If closest scanner lacks critical data, we win.
            if (
                incumbent is None or incumbent.rssi_distance is None or incumbent.area_id is None
                # Extra checks that are redundant but make linting easier later...
                # or closest_advert.hist_distance_by_interval is None
            ):
                # Default Instawin!
                incumbent = challenger
                continue

            # NOTE:
            # From here on in, don't award a win directly. Instead award a loss if the new scanner is
            # not a contender, but otherwise build a set of test scores and make a determination at the
            # end.

            # If we ARE NOT ACTUALLY CLOSER(!) we can not win.
            if incumbent.rssi_distance < challenger.rssi_distance:
                # we are not even closer!
                continue

            tests.reason = None  # ensure we don't trigger logging if no decision was made.
            tests.same_area = incumbent.area_id == challenger.area_id
            tests.areas = (incumbent.area_name or "", challenger.area_name or "")
            tests.scannername = (incumbent.name, challenger.name)
            tests.distance = (incumbent.rssi_distance, challenger.rssi_distance)
            # tests.velocity = (
            #     next((val for val in closest_scanner.hist_velocity), 0),
            #     next((val for val in scanner.hist_velocity), 0),
            # )

            # How recently have we heard from the scanners themselves (not just for this device's adverts)?
            tests.last_ad_age = (
                nowstamp - incumbent.scanner_device.last_seen,
                nowstamp - challenger.scanner_device.last_seen,
            )

            # How old are the ads?
            tests.this_ad_age = (
                nowstamp - incumbent.stamp,
                nowstamp - challenger.stamp,
            )

            # Calculate the percentage difference between the challenger and incumbent's distances
            _pda = challenger.rssi_distance
            _pdb = incumbent.rssi_distance
            tests.pcnt_diff = abs(_pda - _pdb) / ((_pda + _pdb) / 2)

            # Same area. Confirm freshness and distance.
            if (
                tests.same_area
                and (tests.this_ad_age[0] > tests.this_ad_age[1] + 1)
                and tests.distance[0] >= tests.distance[1]
            ):
                tests.reason = "WIN awarded for same area, newer, closer advert"
                incumbent = challenger
                continue

            # Hysteresis.
            # If our worst reading in max_seconds is still closer than the incumbent's **best** reading
            # in that time, and we are over a PD threshold, we win.
            #
            min_history = 3  # we must have at least this much history
            history_window = 5  # the time period to compare between us and incumbent
            pdiff_outright = 0.30  # Percentage difference to win outright / instantly
            pdiff_historical = 0.15  # Percentage difference required to win on historical test
            if len(challenger.hist_distance_by_interval) > min_history:  # we have enough history, let's go..
                # (the closest the incumbent has been, the **furthest** we have been) in that time
                tests.hist_min_max = (
                    min(incumbent.hist_distance_by_interval[:history_window]),
                    max(challenger.hist_distance_by_interval[:history_window]),
                )
                if (
                    tests.hist_min_max[1] < tests.hist_min_max[0]
                    and tests.pcnt_diff > pdiff_historical  # and we're significantly closer.
                ):
                    tests.reason = "WIN on historical min/max"
                    incumbent = challenger
                    continue

            if tests.pcnt_diff < pdiff_outright:
                # Didn't make the cut. We're not "different enough" given how
                # recently the previous nearest was updated.
                tests.reason = "LOSS - failed on percentage_difference"
                continue

            # If we made it through all of that, we're winning, so far!
            tests.reason = "WIN by not losing!"

            incumbent = challenger

        if device.area_advert is not incumbent and tests.reason is not None:
            device.diag_area_switch = tests.sensortext()

        # Apply the newly-found closest scanner (or apply None if we didn't find one)
        device.apply_scanner_selection(incumbent)

"""Options-flow steps for "Auto-tune rooms". Mixed into the options flow handler."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .autotune import Landmark, format_table
from .autotune_ha import (
    CONF_AUTOTUNE_LANDMARKS,
    DEFAULT_AUTOTUNE_MINUTES,
    EXPORT_FILENAME,
    MAX_AUTOTUNE_DEVICE_MINUTES,
    MAX_AUTOTUNE_LANDMARKS,
    MAX_AUTOTUNE_MINUTES,
    async_run_autotune,
    candidates,
    discover,
    settings_to_options,
)
from .const import CONF_MAX_RADIUS, CONF_RSSI_OFFSETS, CONF_SCANNER_MAX_RADIUS, DEFAULT_MAX_RADIUS

if TYPE_CHECKING:
    import asyncio

    from .autotune import TuneReport
    from .autotune_ha import Candidate, Discovery

_LOGGER = logging.getLogger(__name__)

CONF_AUTOTUNE_MINUTES = "autotune_minutes"
CONF_AUTOTUNE_APPLY = "autotune_apply"
CONF_AUTOTUNE_SELECTED = "autotune_selected"
CONF_AUTOTUNE_EXPORT = "autotune_export"
CONF_RESET_RADIUS = "reset_scanner_max_radius"
CONF_RESET_LANDMARKS = "reset_landmarks"
CONF_RESET_OFFSETS = "reset_rssi_offsets"


class AutotuneOptionsMixin:
    """Adds the auto-tune steps. Expects self.coordinator, self.options, self.hass and _update_options()."""

    _autotune_minutes: float = DEFAULT_AUTOTUNE_MINUTES
    _autotune_export: bool = False
    _autotune_task: asyncio.Task | None = None
    _autotune_report: TuneReport | None = None
    _autotune_found: Discovery | None = None
    _autotune_candidates: dict[str, Candidate] | None = None
    _autotune_selected: list[str] | None = None
    _autotune_landmarks: list[Landmark] | None = None

    async def async_step_autotune(self, user_input: dict[str, Any] | None = None):
        """Step 1: tick the devices that never move; choose how long to listen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            minutes = float(user_input[CONF_AUTOTUNE_MINUTES])
            selected = [a.lower() for a in user_input.get(CONF_AUTOTUNE_SELECTED, [])]
            # Bound the work: memory for the readings and CPU for the fair test.
            if len(selected) > MAX_AUTOTUNE_LANDMARKS:
                errors["base"] = "autotune_too_many_devices"
            elif len(selected) * minutes > MAX_AUTOTUNE_DEVICE_MINUTES:
                errors["base"] = "autotune_too_long"
            else:
                self._autotune_minutes = minutes
                self._autotune_export = bool(user_input.get(CONF_AUTOTUNE_EXPORT, False))
                self._autotune_selected = selected
                return await self.async_step_autotune_rooms()

        if self._autotune_found is None or user_input is None:
            self._autotune_found = discover(self.hass, self.coordinator)
            cands = candidates(self.coordinator, self._autotune_found, self.options.get(CONF_AUTOTUNE_LANDMARKS, {}))
            self._autotune_candidates = {c.address: c for c in cands}
        found = self._autotune_found
        cands = list((self._autotune_candidates or {}).values())
        if user_input is not None:
            ticked = [a.lower() for a in user_input.get(CONF_AUTOTUNE_SELECTED, [])]
            minutes_default = float(user_input[CONF_AUTOTUNE_MINUTES])
        else:
            ticked = [c.address for c in cands if c.suggested]
            minutes_default = DEFAULT_AUTOTUNE_MINUTES
        scanner_rooms = sorted({found.area_names.get(s.area, s.area) for s in found.scanners if s.area})
        return self.async_show_form(
            step_id="autotune",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_AUTOTUNE_SELECTED, default=ticked): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=c.address, label=c.label) for c in cands],
                            multiple=True,
                        )
                    ),
                    vol.Required(CONF_AUTOTUNE_MINUTES, default=minutes_default): vol.All(
                        vol.Coerce(float), vol.Range(min=1, max=MAX_AUTOTUNE_MINUTES)
                    ),
                    vol.Optional(CONF_AUTOTUNE_EXPORT, default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "candidate_count": str(len(cands)),
                "suggested_count": str(sum(c.suggested for c in cands)),
                "scanner_count": str(len(found.scanners)),
                "scanner_rooms": ", ".join(scanner_rooms) or "-",
                "export_file": EXPORT_FILENAME,
                "max_devices": str(MAX_AUTOTUNE_LANDMARKS),
                "max_device_minutes": str(MAX_AUTOTUNE_DEVICE_MINUTES),
            },
        )

    async def async_step_autotune_rooms(self, user_input: dict[str, Any] | None = None):
        """Step 2: confirm the room of each ticked device."""
        cands = self._autotune_candidates or {}
        selected = self._autotune_selected or []
        labels = {addr: (cands[addr].label if addr in cands else addr.upper()) for addr in selected}
        if user_input is not None:
            self._autotune_landmarks = [
                Landmark(addr, user_input.get(labels[addr]) or None, labels[addr]) for addr in selected
            ]
            self._autotune_task = None
            return await self.async_step_autotune_collect()

        schema = {}
        for addr in selected:
            default = cands[addr].area_id if addr in cands else None
            key = vol.Optional(labels[addr], default=default) if default else vol.Optional(labels[addr])
            schema[key] = AreaSelector(AreaSelectorConfig())
        return self.async_show_form(
            step_id="autotune_rooms",
            data_schema=vol.Schema(schema),
            description_placeholders={"count": str(len(selected))},
        )

    async def async_step_autotune_collect(self, user_input: dict[str, Any] | None = None):
        """Listen for the chosen time, then tune."""
        if self._autotune_task is None:
            found = self._autotune_found
            areareg = ar.async_get(self.hass)
            area_names = dict(found.area_names) if found else {}
            for lm in self._autotune_landmarks or []:
                if lm.area and lm.area not in area_names and (area := areareg.async_get_area(lm.area)):
                    area_names[lm.area] = area.name
            if found is not None:
                found.area_names = area_names
            self._autotune_task = self.hass.async_create_task(
                async_run_autotune(
                    self.hass,
                    self.coordinator,
                    dict(self.options),
                    self._autotune_minutes * 60,
                    list(self._autotune_landmarks or []),
                    list(found.scanners) if found else [],
                    area_names,
                    self.hass.config.path(EXPORT_FILENAME) if self._autotune_export else None,
                ),
                "roomwise autotune",
                eager_start=False,  # always go through the progress screen
            )
        if not self._autotune_task.done():
            return self.async_show_progress(
                step_id="autotune_collect",
                progress_action="autotune_collecting",
                progress_task=self._autotune_task,
                description_placeholders={"minutes": f"{self._autotune_minutes:g}"},
            )
        try:
            self._autotune_report = self._autotune_task.result()
        except Exception:
            _LOGGER.exception("Auto-tune failed")
            return self.async_show_progress_done(next_step_id="autotune_failed")
        return self.async_show_progress_done(next_step_id="autotune_result")

    async def async_step_autotune_failed(self, user_input: dict[str, Any] | None = None):
        return self.async_abort(reason="autotune_failed")

    async def async_step_autotune_result(self, user_input: dict[str, Any] | None = None):
        """Show the result; Apply writes offsets like manual calibration does."""
        report = self._autotune_report
        if report is None:
            return self.async_abort(reason="autotune_failed")
        if user_input is not None:
            if report.recommend and user_input.get(CONF_AUTOTUNE_APPLY):
                self.options.update(settings_to_options(report, self.options))
                self.options[CONF_AUTOTUNE_LANDMARKS] = {
                    lm.id: lm.area for lm in self._autotune_landmarks or [] if lm.area
                }
                return await self._update_options()
            return self.async_abort(reason="autotune_not_applied")

        found = self._autotune_found
        scanner_names = {s.id: s.name for s in found.scanners} if found else {}
        table = format_table(report, scanner_names)
        if found:  # show room names rather than area ids
            for area_id, area_name in found.area_names.items():
                table = table.replace(f"| {area_id} |", f"| {area_name} |")
        schema = {}
        if report.recommend:
            schema[vol.Required(CONF_AUTOTUNE_APPLY, default=True)] = bool
        return self.async_show_form(
            step_id="autotune_result",
            data_schema=vol.Schema(schema),
            description_placeholders={"summary": report.summary, "table": table},
        )

    async def async_step_autotune_reset(self, user_input: dict[str, Any] | None = None):
        """Show what auto-tune has saved and undo any part of it."""
        if user_input is not None:
            if user_input.get(CONF_RESET_RADIUS):
                self.options.pop(CONF_SCANNER_MAX_RADIUS, None)
            if user_input.get(CONF_RESET_LANDMARKS):
                self.options.pop(CONF_AUTOTUNE_LANDMARKS, None)
            if user_input.get(CONF_RESET_OFFSETS):
                self.options[CONF_RSSI_OFFSETS] = {}
            return await self._update_options()

        devices = getattr(self.coordinator, "devices", {}) or {}
        global_radius = float(self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS))
        radii = self.options.get(CONF_SCANNER_MAX_RADIUS) or {}
        offsets = {k: v for k, v in (self.options.get(CONF_RSSI_OFFSETS) or {}).items() if v}

        def receiver(address: str) -> str:
            device = devices.get(address)
            return device.name if device is not None and device.name else address.upper()

        if radii:
            radius_lines = "\n".join(
                f"- {receiver(sid)}: {min(float(r), global_radius):g} m" for sid, r in sorted(radii.items())
            )
        else:
            radius_lines = "- none"
        if offsets:
            offset_lines = "\n".join(f"- {receiver(sid)}: {v:+g} dB" for sid, v in sorted(offsets.items()))
        else:
            offset_lines = "- none (all 0)"
        return self.async_show_form(
            step_id="autotune_reset",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_RESET_RADIUS, default=bool(radii)): bool,
                    vol.Optional(CONF_RESET_LANDMARKS, default=False): bool,
                    vol.Optional(CONF_RESET_OFFSETS, default=False): bool,
                }
            ),
            description_placeholders={
                "radius_lines": radius_lines,
                "global_radius": f"{global_radius:g}",
                "landmark_count": str(len(self.options.get(CONF_AUTOTUNE_LANDMARKS) or {})),
                "offset_lines": offset_lines,
            },
        )

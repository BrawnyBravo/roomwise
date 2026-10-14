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
    async_run_autotune,
    candidates,
    discover,
    settings_to_options,
)

if TYPE_CHECKING:
    import asyncio

    from .autotune import TuneReport
    from .autotune_ha import Candidate, Discovery

_LOGGER = logging.getLogger(__name__)

CONF_AUTOTUNE_MINUTES = "autotune_minutes"
CONF_AUTOTUNE_APPLY = "autotune_apply"
CONF_AUTOTUNE_SELECTED = "autotune_selected"
CONF_AUTOTUNE_EXPORT = "autotune_export"


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
        if user_input is not None:
            self._autotune_minutes = float(user_input[CONF_AUTOTUNE_MINUTES])
            self._autotune_export = bool(user_input.get(CONF_AUTOTUNE_EXPORT, False))
            self._autotune_selected = [a.lower() for a in user_input.get(CONF_AUTOTUNE_SELECTED, [])]
            return await self.async_step_autotune_rooms()

        found = discover(self.hass, self.coordinator)
        self._autotune_found = found
        cands = candidates(self.coordinator, found, self.options.get(CONF_AUTOTUNE_LANDMARKS, {}))
        self._autotune_candidates = {c.address: c for c in cands}
        scanner_rooms = sorted({found.area_names.get(s.area, s.area) for s in found.scanners if s.area})
        return self.async_show_form(
            step_id="autotune",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_AUTOTUNE_SELECTED, default=[c.address for c in cands if c.suggested]
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[SelectOptionDict(value=c.address, label=c.label) for c in cands],
                            multiple=True,
                        )
                    ),
                    vol.Required(CONF_AUTOTUNE_MINUTES, default=DEFAULT_AUTOTUNE_MINUTES): vol.All(
                        vol.Coerce(float), vol.Range(min=1, max=60)
                    ),
                    vol.Optional(CONF_AUTOTUNE_EXPORT, default=False): bool,
                }
            ),
            description_placeholders={
                "candidate_count": str(len(cands)),
                "suggested_count": str(sum(c.suggested for c in cands)),
                "scanner_count": str(len(found.scanners)),
                "scanner_rooms": ", ".join(scanner_rooms) or "-",
                "export_file": EXPORT_FILENAME,
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

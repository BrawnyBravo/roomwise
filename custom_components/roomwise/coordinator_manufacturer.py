"""Bluetooth SIG manufacturer tables and lookup for the coordinator."""

from __future__ import annotations

from pathlib import Path

import yaml

MANUFACTURER_ID_DIR = Path(__file__).parent / "manufacturer_identification"


# The C loader parses the manufacturer tables several times faster.
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _load_manufacturer_ids(member_path: str | Path, company_path: str | Path) -> tuple[dict[int, str], dict[int, str]]:
    """Read the Bluetooth SIG name tables. Blocking: run in the executor."""
    with Path(member_path).open(encoding="utf-8") as f:
        mi_yaml = yaml.load(f, Loader=YAML_LOADER)["uuids"]  # noqa: S506 - safe loader
    with Path(company_path).open(encoding="utf-8") as f:
        ci_yaml = yaml.load(f, Loader=YAML_LOADER)["company_identifiers"]  # noqa: S506 - safe loader
    return (
        {member["uuid"]: member["name"] for member in mi_yaml},
        {member["value"]: member["name"] for member in ci_yaml},
    )


class ManufacturerMixin:
    """Coordinator methods that load and look up Bluetooth manufacturer names."""

    def get_manufacturer_from_id(self, uuid: int | str) -> tuple[str, bool] | tuple[None, None]:
        """
        An opinionated Bluetooth UUID to Name mapper.

        - uuid must be four hex chars in a string, or an `int`

        Retreives the manufacturer name from the Bluetooth SIG Member UUID listing,
        using a cached copy of https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml

        HOWEVER: Bermuda adds some opinionated overrides for the benefit of user clarity:
        - Legal entity names may be overriden with well-known brand names
        - Special-use prefixes may be tagged as such (eg iBeacon etc)
        - Generics can be excluded by setting exclude_generics=True
        """
        if isinstance(uuid, str):
            uuid = int(uuid.replace(":", ""), 16)

        _generic = False
        # Because iBeacon and (soon) GFMD and AppleFindmy etc are common protocols, they
        # don't do a good job of uniquely identifying a manufacturer, so we use them
        # as fallbacks only.
        if uuid == 0x0BA9:
            # allterco robotics, aka...
            _name = "Shelly Devices"
        elif uuid == 0x004C:
            # Apple have *many* UUIDs, but since they don't OEM for others (AFAIK)
            # and only the iBeacon / FindMy adverts seem to be third-partied, match just
            # this one instead of their entire set.
            _name = "Apple Inc."
            _generic = True
        elif uuid == 0x181C:
            _name = "BTHome v1 cleartext"
            _generic = True
        elif uuid == 0x181E:
            _name = "BTHome v1 encrypted"
            _generic = True
        elif uuid == 0xFCD2:
            _name = "BTHome V2"  # Sponsored by Allterco / Shelly
            _generic = True
        elif uuid in self.member_uuids:
            _name = self.member_uuids[uuid]
            # Hardware manufacturers who OEM MAC PHYs etc, or offer the use
            # of their OUIs to third parties (specific known ones can be moved
            # to a case in the above conditions).
            if any(x in _name for x in ["Google", "Realtek"]):
                _generic = True
        elif uuid in self.company_uuids:
            _name = self.company_uuids[uuid]
            _generic = False
        else:
            return (None, None)
        return (_name, _generic)

    async def async_load_manufacturer_ids(self):
        """Import yaml files containing manufacturer name mappings."""
        try:
            # Next to this file, wherever the integration is installed (not
            # assumed to be under the config folder's custom_components).
            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/member_uuids.yaml
            member_path = MANUFACTURER_ID_DIR / "member_uuids.yaml"
            # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/company_identifiers/company_identifiers.yaml
            company_path = MANUFACTURER_ID_DIR / "company_identifiers.yaml"
            # Reading and parsing ~200 KB of YAML blocks for a noticeable time, so
            # it runs in the executor rather than on the event loop.
            self.member_uuids, self.company_uuids = await self.hass.async_add_executor_job(
                _load_manufacturer_ids, member_path, company_path
            )
        finally:
            # Ensure that an issue reading these files (which are optional, really) doesn't stop the whole show.
            self._waitingfor_load_manufacturer_ids = False

# Changelog

## Unreleased

### Fixed

- Putting a proxy in a room, moving it to another room or renaming it in Home Assistant now takes effect straight
  away. Before, Roomwise kept using the old room and name until Home Assistant restarted, so the "proxies without a
  room" repair stayed up and nobody was placed in the newly assigned room.
- If an update cycle ever fails, Roomwise no longer retries it on every Bluetooth advert (often hundreds a second),
  which could tie up a processor core and flood the log. It now retries at most once every couple of seconds and
  logs the error once.
- An iBeacon that changes its identity (major/minor, for example a beacon with a "movement" mode) now follows its
  newest identity. Before, it stayed on the oldest one it had sent recently, so the beacon you track showed as away
  while an old identity kept a location.
- Apple frames too short to be an iBeacon no longer create a shared, nameless beacon that merged unrelated devices,
  and devices that alternate an iBeacon with another payload are no longer re-matched on every packet.
- Global Options and Calibration 1 now refuse values that break the distance maths (for example an attenuation of 0,
  which stopped every device from getting a distance or room, or a max radius of 0, which made auto-tune fail after
  its full listening time). A bad value saved by an older version is replaced by the default, with a warning in the
  log. Allowed ranges: max radius at least 0.1 m, max velocity at least 0.1 m/s, away timeout at least 1 s, update
  interval at least 0 s, smoothing samples 1 to 1000, attenuation 0.5 to 10, reference power -127 to 0 dBm.
- `dump_devices` with `configured_devices` now includes Private BLE devices (phones and watches) and their current
  addresses; before, it matched them by the wrong key and left them out.
- The "area switch" diagnostic is recorded again when a device moves from one receiver to another (it was only
  updated when a device became placed or unplaced).
- Deleting a tracked device (or iBeacon) from its device page now sticks: it is also removed from Select Devices, so
  it no longer comes back with all its entities after the next restart. Private BLE devices (phones and watches)
  are tracked automatically, so the delete button now says no and the log explains to remove them in the Private
  BLE Device integration instead.
- Calibration 2 (per-proxy offsets): two proxies with the same name now get a line each (with a shortened address)
  and can be set separately. Saving no longer fails with "Unknown error" when a proxy appeared or was renamed while
  the form was open or a line was deleted (that proxy keeps its saved offset), and a value that is not a number
  shows a clear error instead.
- One proxy from an integration that does not register a network address no longer stops "Distance to <proxy>"
  sensors from being created for every tracked device. Ready proxies get them straight away; such a proxy gets its
  own after two minutes.
- "Area Last Seen" no longer shows the word "None" as a room after a restart when it had no room saved.
- The calibration steps no longer fail with "Unknown error" when the chosen device has not been heard since the
  restart.
- Calibration 1 now shows a readable message when the chosen proxy has not heard the chosen device.
- The Area, Floor, Nearest Scanner, Area Last Seen, area-switch and count sensors no longer report a made-up device
  class (`roomwise__custom_device_class`); they now have none.
- Less work on busy installs: entities no longer rewrite an unchanged state every second.
- Adding your first floor, putting a room on a floor or renaming a room now takes effect without a restart (Floor
  sensors are added for devices that did not have one).
- The `floor_level` attribute of the Area and Floor sensors now holds the floor's level (it was always empty).
- A phone or watch added as a Private BLE device is matched to the address it is using right now, even when that
  address has been in use for a long time.

### Privacy

- Identity keys (IRKs) of phones and watches are no longer written to the Home Assistant log, at any log level, not
  even in part. An IRK lets anyone who has it follow that device, and debug logs are often posted publicly. Log lines
  now carry a short tag (like `irk#3fa9c1`) that cannot be turned back into the key. A Private BLE device that has
  not been named yet is no longer named after its key either.
- The `roomwise.dump_devices` action can now only be called by Home Assistant administrators, and its output is
  redacted by default. Before, any signed-in user (or any long-lived token) could get every device's Bluetooth
  address and the identity keys of everyone's phones. Pass `redact: false` to get the raw output for your own
  troubleshooting.
- Redacted output (diagnostics and `dump_devices`) no longer includes the original value inside the placeholder for
  ids that are neither a Bluetooth address nor an iBeacon, such as an identity key saved as a configured device.

## 0.1.2 (2026-10-15)

### Added

- Roomwise now has its own icon, shown in HACS and on the Home Assistant integrations page.

### Changed

- Auto-tune is no longer labelled experimental in the docs.

## 0.1.1 (2026-10-14)

### Fixed

- Auto-tune and "Reset auto-tune settings" show each receiver by its name instead of an address. A proxy whose
  network address is also listed by another integration (a router, say) no longer takes that integration's
  address-like name; the name you gave the proxy wins, then its integration's name, then the Bluetooth scanner's
  name, and only then the address.
- The auto-tune summary no longer counts devices in rooms without a receiver twice: "other devices could not be
  checked" now counts only devices not already reported, and gives only the reasons that apply.
- No more Home Assistant deprecation warning about `device_registry.devices` (it would stop working in Home
  Assistant 2027.9), and no use of a device's `config_entries` (deprecated in 2026.10, removed in 2027.10).

## 0.1.0 (2026-10-14)

First release of Roomwise, built on [Bermuda BLE Trilateration](https://github.com/agittins/bermuda) by
Ashley Gittins ([agittins](https://github.com/agittins)), MIT licence, from Bermuda's main branch after v0.8.7.

### New

- **Auto-tune rooms** (experimental): learns per-proxy signal offsets from devices that never move and whose room
  you know, and where it helps a smaller range for one proxy, judged fairly (each device scored with settings tuned
  without it) and only offered when it puts more devices in the right room. Nothing is saved until you press Apply.
  Optional unredacted raw-readings export for troubleshooting. Size limits: up to 60 devices and 1200
  device-minutes per run, bounded memory, and closing the dialog stops the work.
- **Reset auto-tune settings** in the options menu: shows and removes per-proxy range limits, remembered devices
  and (if asked) all offsets.
- Optional per-proxy max radius (set by auto-tune), never larger than the global max radius.

### Fixed

- Max radius now also applies to the proxy a device is currently assigned to, so a device that moves out of range
  is released instead of staying in the old room while no other proxy hears it. That proxy has an exit margin: a
  device enters a room within the max radius but only leaves it beyond 1.3 times the max radius, or after 30
  seconds beyond the max radius, so a device near the edge does not flicker between the room and "unplaced".
- Download diagnostics no longer fails on installs with Bluetooth history, and redacts device addresses inside
  Bluetooth device objects too.
- Diagnostics redaction is linear in size, covers every address format, and runs off the event loop.
- No crash when a rotated private address is pruned twice (from agittins/bermuda#833 by @davidcoulson).
- No crash or off-by-one when the prune quota kicks in (from agittins/bermuda#848 by @barneyonline).
- Bluetooth address types classified by equality (from agittins/bermuda#863 by @davidcoulson), and the Bluetooth
  stack's "public address" hint is honoured.
- Distance sensors update right after a reference-power change (from agittins/bermuda#865 by @davidcoulson).
- Stale iBeacon devices nobody tracks are pruned (from agittins/bermuda#867 by @davidcoulson).
- Proxies are listed in a stable, sorted order in calibration and status screens (from agittins/bermuda#836 by
  @BrawnyBravo).
- Manufacturer tables are loaded off the event loop.
- The `roomwise.dump_devices` action is removed when the integration unloads.

### Changed

- New domain `roomwise`, its own folder, actions and entities, so it can run alongside Bermuda (only track a device
  with one of them). Not a migration: Bermuda settings, entity ids and history do not carry over.
- README and HACS info rewritten for Roomwise, with credits to Bermuda and the upstream pull request authors.

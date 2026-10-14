# Changelog

## 0.1.0 (unreleased)

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

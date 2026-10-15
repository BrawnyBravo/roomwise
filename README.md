# Roomwise

**Bluetooth room presence for [Home Assistant](https://home-assistant.io/) that tunes itself.**

Roomwise tells Home Assistant which room (Area) each of your Bluetooth devices is in, using the
[ESPHome Bluetooth Proxies](https://esphome.io/components/bluetooth_proxy.html), Shelly Gen2+ devices
or local Bluetooth adapters you already have.

Its lead feature is **Auto-tune rooms** (new in 0.1): instead of hand-adjusting
signal offsets for every proxy, Roomwise learns them from devices whose room you already know (a
smart plug, a TV, a speaker) and only offers the new settings when they put more of those devices
in the right room than before. Nothing changes unless you press Apply, and it can be undone. See
[Auto-tune rooms](#auto-tune-rooms) below. Everything else works like the Bermuda version it is
built on, including manual calibration.

[![License][license-shield]](LICENSE)
[![HomeAssistant Minimum Version][haminverbadge]][haminver]
[![hacs][hacsbadge]][hacs]

## Credits

**Roomwise is built on [Bermuda BLE Trilateration](https://github.com/agittins/bermuda) by
Ashley Gittins ([agittins](https://github.com/agittins)), MIT licence.** Almost all of the room
presence engine is Bermuda's work, and the original copyright notice is kept in [LICENSE](LICENSE).
If Roomwise is useful to you, please consider
[supporting the original author](https://github.com/sponsors/agittins).

### Fixes from upstream pull requests

Roomwise 0.1.0 includes these fixes from open pull requests on Bermuda. Thank you to their authors:

- [#833](https://github.com/agittins/bermuda/pull/833) by [@davidcoulson](https://github.com/davidcoulson):
  no crash when a rotated private address is pruned twice.
- [#848](https://github.com/agittins/bermuda/pull/848) by [@barneyonline](https://github.com/barneyonline):
  no crash (and no off-by-one) when the device-count prune quota kicks in.
- [#863](https://github.com/agittins/bermuda/pull/863) by [@davidcoulson](https://github.com/davidcoulson):
  Bluetooth address types classified by equality, not bit masks.
- [#865](https://github.com/agittins/bermuda/pull/865) by [@davidcoulson](https://github.com/davidcoulson):
  distance sensors update straight after a reference-power change.
- [#867](https://github.com/agittins/bermuda/pull/867) by [@davidcoulson](https://github.com/davidcoulson):
  stale iBeacon devices that nobody tracks are pruned.
- [#836](https://github.com/agittins/bermuda/pull/836) by [@BrawnyBravo](https://github.com/BrawnyBravo)
  (Roomwise's maintainer): proxies listed in a stable, sorted order in calibration and status screens.

The full list of changes is in [CHANGELOG.md](CHANGELOG.md).

Bermuda itself was generated from [@oncleben31](https://github.com/oncleben31)'s
[Home Assistant Custom Component Cookiecutter](https://github.com/oncleben31/cookiecutter-homeassistant-custom-component)
template, with code mainly taken from [@Ludeeus](https://github.com/ludeeus)'s
[integration_blueprint][integration_blueprint] template.

## Running alongside Bermuda

Roomwise uses its own domain (`roomwise`), folder, actions and entities, so it can be installed
at the same time as Bermuda without either one breaking. **Only track a given device with one of
them, though.**

- **Running both costs twice.** Each integration processes every Bluetooth advertisement on its
  own, so having both installed doubles that work on your Home Assistant host.
- **They share devices.** Both attach their sensors to the same Home Assistant device, so tracking
  a device in both gives you two sets of near-identical sensors (Home Assistant adds `_2` to the
  second set's entity ids).

### Switching from Bermuda is not a migration

Roomwise does not import anything from Bermuda. Your Bermuda settings, calibration (reference
power, attenuation, per-proxy offsets), selected devices, entity ids and history do **not** carry
over. Roomwise creates new entities with new ids, so automations, scripts and dashboards that use
Bermuda's entities need to be re-pointed to the Roomwise ones.

The recommended path is: install Roomwise, set it up for a device or two and compare it with
Bermuda for a while, then move your automations over and remove Bermuda.

## What it does

- Area-based device location (device-level room presence).
- Creates sensors for Area and Distance for devices you choose.
- Supports iBeacon devices, including those with randomised MAC addresses (like Android phones running the HA Companion App).
- Supports IRK (resolvable keys) via the [Private BLE Device](https://www.home-assistant.io/integrations/private_ble_device/) core integration. Once your iOS (or Android) device is set up in Private BLE Device, it can be selected in Roomwise too.
- Creates `device_tracker` entities for chosen devices, which can be linked to People for Home/Not Home tracking.
- Configurable settings for RSSI reference level, environmental attenuation, maximum tracking radius and per-proxy RSSI offsets.
- Auto-tune rooms: proposes per-proxy offsets (and, where it helps, a smaller range for a proxy) from devices that never move.
- Provides a full JSON/YAML dump of devices and their distances from each Bluetooth receiver, via the `roomwise.dump_devices` action.

## What you need

- Home Assistant 2026.9.0 or newer (tested on 2026.9.3). HACS will not install it on older versions.
- One or more devices providing Bluetooth proxy information to Home Assistant's Bluetooth integration:
  - ESPHome devices with the `bluetooth_proxy` component enabled.
  - Shelly Plus or later devices with Bluetooth proxying enabled in the Shelly integration.
  - A USB Bluetooth adapter on your Home Assistant host. This is not ideal, since these do not timestamp
    advertisement packets, but they work for simple Home/Not Home tracking and basic Area support.
- Every proxy needs an Area assigned in Home Assistant (Roomwise raises a repair if one is missing).
- Some Bluetooth LE devices you want to track: phones, watches, beacon tiles, thermometers and so on.

## Installation (HACS custom repository)

Roomwise is not in the default HACS list yet, so add it as a custom repository:

1. In Home Assistant, open **HACS**.
2. Open the three-dots menu (top right) and choose **Custom repositories**.
3. Enter `BrawnyBravo/roomwise` as the repository and choose **Integration** as the type, then **Add**.
4. Search HACS for **Roomwise**, open it and choose **Download**.
5. Restart Home Assistant.
6. Go to **Settings, Devices & Services, Add Integration** and search for **Roomwise**.

Or use this button to open the repository in HACS directly:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BrawnyBravo&repository=roomwise&category=Integration)

Once the integration is added, choose **Configure** on it in Devices & Services to pick which Bluetooth
devices to track.

### Manual installation

1. Open the folder for your Home Assistant configuration (where `configuration.yaml` lives).
2. If there is no `custom_components` folder there, create it.
3. Inside `custom_components`, create a folder called `roomwise`.
4. Copy _all_ the files from `custom_components/roomwise/` in this repository into that folder.
5. Restart Home Assistant.
6. Go to **Settings, Devices & Services, Add Integration** and search for **Roomwise**.

## Documentation and help

Until Roomwise has its own guides, Bermuda's documentation applies to almost everything here
(read "Bermuda" as "Roomwise", and `bermuda.` as `roomwise.`):

- [The Bermuda Wiki](https://github.com/agittins/bermuda/wiki/), including the
  [FAQ](https://github.com/agittins/bermuda/wiki/FAQ) and
  [Troubleshooting](https://github.com/agittins/bermuda/wiki/Troubleshooting) pages.
- The [HA Community thread for Bermuda](https://community.home-assistant.io/t/bermuda-bluetooth-ble-room-presence-and-tracking-custom-integration/625780/1).

Please report Roomwise problems on the [Roomwise issue tracker](https://github.com/BrawnyBravo/roomwise/issues),
not on Bermuda's.

## Screenshots

The screenshots below were taken with Bermuda. Roomwise looks the same apart from its name.

After installing, the integration is visible in Settings, Devices & Services.

![The integration, in Settings, Devices & Services](img/screenshots/integration.png)

Press `CONFIGURE` to see the configuration dialog. At the bottom is a field where you can choose any
Bluetooth devices the system can see. Choosing devices adds them to the configured devices list and
creates sensor entities for them.

![Configuration options flow](img/screenshots/configuration.png)

The device page shows the current sensors and other information. Some extra sensors are disabled
by default and can be enabled for more detailed data, mainly for troubleshooting.

![Device information view](img/screenshots/deviceinfo.png)

The sensor information also includes attributes such as the area name and id, and relevant MAC addresses.

![Sensor information](img/screenshots/sensor-info.png)

In Settings, People, you can use a Roomwise device tracker for any person's home/away status.

![Assigning a tracker for Person tracking](img/screenshots/person-tracker.png)

## Auto-tune rooms

Auto-tune is new in 0.1. So far it has been tested on made-up houses and one real home. It only
ever proposes changes; you decide whether to apply them, and they can be undone.

### What it does

Roomwise puts a device in the room of the nearest proxy (receiver) that is within range (the max
radius). Once a device is in a room it keeps it until it reads beyond 1.3 times the max radius, or
stays beyond the max radius for 30 seconds, so a device near the edge does not flicker between the
room and "unplaced" (while it is kept only by that margin, any other proxy within range takes it
over). Proxies
differ (antenna, case, placement), so one may "hear" devices louder than another and steal them
from the room next door. Auto-tune listens to devices that never move and whose room you know, then
works out a per-proxy signal offset (the same setting as **Calibration 2**) that puts as many of them
as possible in their own room.

If a device sits in a room that has no proxy of its own, the best Roomwise can do is leave it
unplaced rather than put it in the wrong room. Where that helps, auto-tune may also propose a
smaller **range limit for one proxy** (never larger than the global max radius, never below 2 m, and
never at the cost of a device in that proxy's own room). A typical case is a proxy directly under a
room that has none: devices upstairs are heard best by the proxy below and land in the wrong room. A
smaller range on that proxy can leave them unplaced instead. If they are louder than the proxy's own
room, no setting can tell them apart, and auto-tune says so: the fix is a proxy in that room.

Each device is judged fairly: its "after" result uses settings tuned without that device (with many
devices, without its group). Auto-tune only recommends a change when more devices land in the right
room than with your current settings (or as many, with fewer devices in proxy-less rooms misplaced).

### How to run it

1. Every proxy needs an Area, and so does each device you pick.
2. Go to **Settings, Devices & Services, Roomwise, Configure, Auto-tune rooms**.
3. Tick devices that **never move**: smart plugs, light bulbs, TVs, speakers, appliances. Devices
   Home Assistant already knows the room of are pre-ticked. Do not tick phones, watches, tags, or
   anything carried around. A few devices in each room with a proxy is ideal; up to 60 devices, and
   devices x minutes up to 1200. Some fixed devices (those whose Bluetooth address looks like one that
   could change) only appear in the list after Roomwise has heard them steadily for about 20 minutes,
   for example right after a restart of Home Assistant. If one is missing, wait and open Auto-tune
   again.
4. Confirm each device's room, then **keep the dialog open** while it listens (10 minutes is a good
   start). Closing the dialog cancels the run and changes nothing.
5. Read the result: a per-room table (right now vs right after tuning), the proposed offsets and range
   limits, and plain-language notes about rooms it cannot fix.

### What Apply saves

Apply saves exactly three things in Roomwise's options:

- **Per-proxy signal offsets** shown in the result (the same values you can edit in Calibration 2).
- **Per-proxy range limits** shown in the result. These replace any saved by an earlier run. A proxy
  shown at the global max radius has no limit. A saved limit never exceeds the global max radius,
  even if you lower the global value later.
- **The devices you picked and their rooms**, so they are ticked next time.

Nothing is saved if you close the dialog or untick Apply.

### How to undo it

Open **Configure, Reset auto-tune settings**. It lists the saved per-proxy range limits, the current
offsets and the number of remembered devices, and lets you remove the range limits, forget the
devices, and/or set every offset back to 0 (that also clears offsets you set by hand). Nothing is
ticked to start with, so opening it just to look and pressing Submit changes nothing. You can also
edit offsets directly in Calibration 2.

### The optional export file

Ticking "Also save the raw readings to a file" writes `roomwise_autotune_samples.json` to your Home
Assistant config folder, for troubleshooting or offline replay. **This file is not redacted**: it
contains the Bluetooth addresses and names of your devices and proxies, and your room names. Do not
post it publicly. Delete it when you are done.

## Using the `roomwise.dump_devices` action

Calling `roomwise.dump_devices` returns a full dump of the internal data structures Roomwise uses
to track devices and calculate their state. This helps with troubleshooting, or with very custom
needs you can solve with template sensors.

With no parameters it returns all data. The `addresses` parameter limits the result to one or more
MAC addresses (or iBeacon UUIDs). See the parameter descriptions under Developer Tools, Actions.

The format of this dump is internal and may change in any release without being treated as a
breaking change.

## Prior art

The `bluetooth_tracker` and `ble_tracker` integrations only give a home/not home answer and do not
do Area-based location. The "monitor" script and ESPresense use dedicated hardware rather than Home
Assistant's Bluetooth proxies. Bermuda (and so Roomwise) builds on the Bluetooth proxy support built
into Home Assistant.

## Contributing

Contributions are welcome. Please read the [contribution guidelines](CONTRIBUTING.md).

## Licence and third-party data

Roomwise is released under the [MIT licence](LICENSE), as is Bermuda.

The files in `custom_components/roomwise/manufacturer_identification/` (`company_identifiers.yaml`
and `member_uuids.yaml`) are copies of the Bluetooth SIG's public
[Assigned Numbers](https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/) lists,
carried over unchanged from Bermuda. They are **not** covered by the MIT licence: they are
Bluetooth SIG data, provided under the Bluetooth SIG's own terms (see the notice kept at the top of
`member_uuids.yaml` and www.bluetooth.com). Roomwise uses them only to show manufacturer names.

The Bermuda logo and icon images were removed from this repository, since they are Bermuda's
branding. The screenshots in `img/screenshots/` come from Bermuda and are covered by its MIT licence.

---

[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[haminver]: https://github.com/BrawnyBravo/roomwise/commits/main/hacs.json
[haminverbadge]: https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fgithub.com%2FBrawnyBravo%2Froomwise%2Fraw%2Fmain%2Fhacs.json&query=%24.homeassistant&style=for-the-badge&logo=homeassistant&logoColor=%2311BDF2&label=Minimum%20HA%20Version
[license-shield]: https://img.shields.io/github/license/BrawnyBravo/roomwise.svg?style=for-the-badge

[![License][license-shield]][license]
[![hacs][hacsbadge]][hacs]

# Roomwise

Bluetooth room presence for Home Assistant that tunes itself.

Roomwise tells Home Assistant which room (Area) each of your Bluetooth devices is in, using your
ESPHome Bluetooth proxies, Shelly Gen2+ devices or local Bluetooth adapters. Self-tuning rooms
(learning each proxy's signal offsets from devices whose room you already know) is coming in 0.1.

**Roomwise is built on [Bermuda BLE Trilateration](https://github.com/agittins/bermuda) by
Ashley Gittins (agittins), MIT licence.**

Roomwise can be installed alongside Bermuda, but only track a given device with one of them.
Switching from Bermuda is not a migration: settings, calibration, entity ids and history do not
carry over, and automations need re-pointing. See the README for details.

**This integration sets up the following platforms.**

| Platform         | Description                                           |
| ---------------- | ----------------------------------------------------- |
| `sensor`         | Area, distance and related sensors for each device.   |
| `device_tracker` | Home/not home tracker for each device.                |
| `number`         | Per-device calibration (reference power).             |

{% if not installed %}

## Installation

1. Click Download.
1. Restart Home Assistant.
1. Go to Settings, Devices & Services, Add Integration and search for "Roomwise".

{% endif %}

## Configuration is done in the UI

---

[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license]: https://github.com/BrawnyBravo/roomwise/blob/main/LICENSE
[license-shield]: https://img.shields.io/github/license/BrawnyBravo/roomwise.svg?style=for-the-badge

---
name: Issue
about: Report a problem with Roomwise, or ask for help getting it working
---

<!-- Before you open a new issue:

Thanks for taking the time to report a problem.

Roomwise is built on Bermuda BLE Trilateration, so much of Bermuda's
documentation also applies here. The Bermuda wiki's Troubleshooting page
covers the most common set-up questions:
https://github.com/agittins/bermuda/wiki/Troubleshooting

Please also search the existing Roomwise issues first; common questions such
as "my device bounces between rooms" may already have an answer.

Please report Roomwise problems here, not on the Bermuda tracker.

You can remove these comment blocks before submitting.
-->

## Configuration

<!-- Ideally paste screenshots of the config dialogs, from Settings -> Devices and Services -> Roomwise -> CONFIGURE,
then include the first diagnostic screen, the Global Options and Select Devices panels.
You can blur out the last 3 or 4 pairs of numbers in each MAC address for privacy reasons, but it's sometimes helpful
to leave the name and the first few digits (as it often gives a clue to the manufacturer and helps to have something
to reference in discussions).
-->

## Describe the bug

A clear and concise description of what the bug is.

## Diagnostics

In Home Assistant, go to Settings, Devices and Services, Roomwise, click the three dots and choose
"Download Diagnostics". After a short wait you will get a JSON file you can attach here.

If Roomwise has been running a long time (several days) the diagnostics might take a long time
to generate. You can try reloading Roomwise first (Settings, Devices and Services, Roomwise,
three-dots menu, Reload), then wait a few minutes, then try the download again.

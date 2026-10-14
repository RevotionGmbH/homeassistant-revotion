# Changelog

All notable changes to the Revotion Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); the version
is the release tag and matches `manifest.json` (pre-releases use PEP 440 style, e.g. `0.4.0b1`).

## 0.5.2 — 2026-10-14

### Fixed
- The GPS position no longer jumps back to an old location after Home Assistant restarts or the connection to the Revotion cloud is re-established. Since Brain firmware 2.4.0 the cloud may still hold a stale position from before the firmware update; it is now ignored as soon as the current position is known
- Compatibility with Home Assistant 2026.11: climate entities (Truma, Alde, Dometic FreshJet, Eberspächer Airtronic) use the new temperature property names, avoiding the deprecation warnings that would turn into errors with Home Assistant 2027.11 — older Home Assistant versions keep working
- Connect devices no longer produce "does not generate unique IDs" errors in the log when a data update briefly lacks one of their values
- A node that is unpaired and paired again now gets its entities back immediately instead of only after a restart
- A command refused by the Brain because an automation rule currently controls that output (Brain firmware 2.4.0) is now reported as "blocked by an automation rule" instead of an unexplained failure code

### Changed
- Setup instructions point to the current place of the configuration code in the Revotion app: System > REMOTE > Share remote access

## 0.5.1 — 2026-09-17

### Fixed
- The MQTT connection is no longer rebuilt every 10–60 minutes while a Brain is quiet. A Brain only sends updates when values change, so a parked vehicle can legitimately stay silent for hours — previously this was mistaken for a broken connection, causing dozens of reconnects per day, each followed by a full data reload from the Revotion cloud and a warning in the log. The integration now verifies a quiet connection without disconnecting and only reconnects when it is really broken
- Fewer requests to the Revotion cloud: a reconnect now triggers a single data reload instead of two, and reloads triggered in quick succession are combined
- A rate-limited or otherwise rejected request to the Revotion cloud (e.g. HTTP 429) is now handled like a temporary connection problem instead of producing an unhandled error in the log
- Compatibility with Home Assistant 2026.9: removes the deprecation warnings about device registry calls that would stop working with Home Assistant 2027.8

### Changed
- Node error-list updates (e.g. a node that is briefly unreachable) are no longer logged as warnings; changes in a node's reachability are still logged at info level
- Diagnostics include the number of connection checks performed while a Brain was quiet

## 0.5.0 — 2026-08-13

First stable release — same code as pre-release 0.5.0b0, wrapping up the
0.4.0b0–0.5.0b0 pre-release series.

What the integration offers:

- Setup via config flow: paste the access token from the Revotion app (Settings > Share Configuration)
- Brain as hub device with automatic discovery of all paired Nodes and their capabilities
- Real-time updates over MQTT with REST fallback polling
- Sensors for batteries, tank levels, temperatures, currents and more; switches (incl. multi-channel), dimmable / RGBW / tunable-white lights, two-way covers, GPS device tracker and per-node connectivity monitoring
- Revotion Connect devices: Eberspächer Airtronic, Truma Combi / CP plus, Alde heating, Dometic FreshJet and fridges, EcoFlow power stations, Thitronik alarm system, and the Victron energy family — BMV / SmartShunt, MPPT, Orion XS, Phoenix, CAN (BMS) batteries (Brain firmware 2.3.3)
- Commands are acknowledged by the Brain (firmware 2.3.3) and always send the device's complete control state, exactly like the Revotion app
- Commands are blocked while the Brain is connected via cellular (protects your SIM data plan) — control requires WiFi, monitoring works everywhere
- English and German translations

Detailed per-release changes: see the sections below or
[CHANGELOG.md](https://github.com/RevotionGmbH/homeassistant-revotion/blob/main/CHANGELOG.md).

## 0.5.0b0 — 2026-08-13

Third pre-release.

### Added
- Victron energy family (requires Brain firmware 2.3.3): BMV / SmartShunt battery monitors (incl. DC-meter mode and the remote-controllable relay), MPPT solar chargers (incl. load output control), Orion XS DC-DC chargers, Phoenix chargers and inverters, and CAN (BMS) batteries — each as its own device with tailored sensors and controls, mirroring the Revotion app's Energy widget
- Total current sensor for battery and high-current nodes (sum over all channels; Brain firmware 2.3.3)
- Dometic FreshJet: max. shore input current is now adjustable (1–15 A, on capable units)
- Level sensors expose the raw probe voltage as a diagnostic sensor (Brain firmware 2.3.3)
- Commands are now acknowledged by the Brain (firmware 2.3.3): a failed command reverts and notifies immediately instead of after 60 s, and a command for a sleeping node waits for the node's next wake-up instead of timing out

### Changed
- Electrical sensors (voltage, current, power) now default to 2 decimal places, so values are readable out of the box without adjusting each sensor's display precision by hand (you can still override it per entity)

### Fixed
- Commands now always send the device's complete control state (like the Revotion app) instead of only the changed value — previously, toggling e.g. the Truma water heater could reset other settings on the device (heating mode, target temperature, fan or sleep mode) to zero

## 0.4.0b1 — 2026-06-12

Second pre-release.

### Added
- Thitronik alarm system: panic alarm button
- Thitronik alarm system: alarm reasons are now translated (English and German)

### Changed
- Dometic FreshJet: sleep mode is now a read-only binary sensor instead of a switch (it reflects the timer-controlled state and cannot be toggled directly)
- Dometic fridges: the cooling level control is now correctly named "Cooling level" instead of "Fan speed"
- Number and select controls only appear when the device actually supports them (matching the Revotion app)
- Select options are now displayed capitalized

### Fixed
- Sensors deleted in the Revotion app are now also removed from Home Assistant

## 0.4.0b0 — 2026-06-10

Initial public pre-release.

### Added
- Setup via config flow: paste the access token from the Revotion app (Settings > Share Configuration)
- Brain as hub device with automatic discovery of all paired Nodes and their capabilities
- Real-time updates over MQTT with REST fallback polling
- Sensors for batteries, tank levels, temperatures, currents and more
- Switches (incl. multi-channel), dimmable / RGBW / tunable-white lights and two-way covers
- GPS device tracker
- Node connectivity monitoring per device
- Revotion Connect devices: Eberspächer Airtronic, Truma Combi / CP plus, Alde heating, Dometic FreshJet / fridges, EcoFlow power stations and Thitronik alarm system
- Commands are blocked while the Brain is connected via cellular (protects your SIM data plan) — control requires WiFi, monitoring works everywhere
- English and German translations

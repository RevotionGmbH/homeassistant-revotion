# Changelog

All notable changes to the Revotion Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); the version
is the release tag and matches `manifest.json` (pre-releases use PEP 440 style, e.g. `0.4.0b1`).

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

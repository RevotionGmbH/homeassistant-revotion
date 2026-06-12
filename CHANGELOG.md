# Changelog

All notable changes to the Revotion Home Assistant integration are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/); the version
is the release tag and matches `manifest.json` (pre-releases use PEP 440 style, e.g. `0.4.0b1`).

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

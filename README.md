# Revotion Home Assistant Integration

[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/HA-2025.2%2B-blue.svg)](https://www.home-assistant.io)

A custom Home Assistant integration for [Revotion](https://revotion.de) digital control systems for campers and boats. Connects Brain and Node devices natively into Home Assistant with real-time MQTT data and full entity control.

## Features

- **Real-time data** via MQTT push (REST polling as fallback)
- **Multi-Brain support** -- add multiple Brains (camper + boat)
- **Multi-channel capabilities** -- Multiwhite & Multiswitch nodes expose one entity per channel
- **GPS tracking** -- camper/boat location on the HA map
- **Dynamic discovery** -- new nodes appear automatically when paired
- **German translations** included
- **Diagnostics** download for troubleshooting

## Requirements

- Home Assistant 2025.2.0 or newer
- Revotion Brain with WiFi connection (Brain version 2.3.2 or newer)
- Revotion Flutter App (to generate the access token)

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open this repository inside HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=RevotionGmbH&repository=homeassistant-revotion&category=integration)

Or manually:

1. Open HACS in Home Assistant
2. Click the three dots menu (top right) > **Custom repositories**
3. Add `https://github.com/RevotionGmbH/homeassistant-revotion`, select category **Integration**, and click **Add**
4. Search for "Revotion" in HACS and click **Download**
5. Restart Home Assistant

### Manual

1. Copy the `custom_components/revotion/` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. In the Revotion App, go to **Settings > Share Configuration** -- a QR code is displayed
2. Scan the QR code with a QR scanner app that shows its content as text, and copy the text
   - *This workaround is only needed until the next app update, which adds a button to copy the configuration directly.*
3. In Home Assistant, go to **Settings > Devices & Services > Add Integration**
4. Search for **Revotion** and select it
5. Paste the copied JSON into the text field
6. Confirm the detected Brain name and MAC address

The integration automatically validates your token and sets up all discovered devices.

## Entities

| Capability | HA Platform | Entity Type | Description |
|------------|-------------|-------------|-------------|
| Switch | `switch` | SwitchEntity | On/off control for relay outputs |
| Two-Way | `cover` | CoverEntity | Bidirectional drive (OPEN/CLOSE/STOP) — awning/window/shutter |
| Temperature | `sensor` | SensorEntity | Temperature reading (Celsius) |
| Battery | `sensor` | SensorEntity | SOC, Voltage, Current, Temperature, Time Remaining, Time to Full, Charge Cycles |
| Level | `sensor` | SensorEntity | Tank level (Frischwasser, Grauwasser, etc.) |
| High Current | `sensor` | SensorEntity | High-current output monitoring |
| Ambient Light | `light` | LightEntity | RGBW light with brightness and color control |
| Multiwhite | `light` | LightEntity | Multi-channel light — one RGBW/brightness entity per channel |
| Multiswitch | `switch` | SwitchEntity | Multi-channel relay — one on/off entity per channel |
| Brain Online | `binary_sensor` | BinarySensorEntity | Brain connectivity status |
| GPS Position | `device_tracker` | TrackerEntity | Camper/boat location on map |

### Battery Sub-Entities

Each Battery capability creates multiple sensor entities:

| Sub-Entity | Unit | Device Class |
|------------|------|--------------|
| State of Charge | % | battery |
| Voltage | V | voltage |
| Current (per channel) | A | current |
| Temperature | C | temperature |
| Time Remaining | -- | duration |
| Time to Full | -- | duration |
| Charge Cycles | -- | -- |

### Switch Timer Attributes

Switch and light entities expose timer attributes when active:

- `timer_state`: "active" or "inactive"
- `timer_remaining`: "HH:MM:SS" countdown
- `timer_scheduled_utc`: ISO 8601 timestamp of scheduled end

## Dynamic Discovery

When you pair a new Node to your Brain, the integration automatically:
1. Detects the pair event via MQTT
2. Fetches the new Node's configuration from the Revotion API
3. Creates all corresponding devices and entities

When you unpair a Node, its entities are marked as **unavailable** (not deleted), preserving your automations and history.

## Token Renewal

Revotion tokens are valid for 180 days. When your token is about to expire (< 14 days remaining), Home Assistant shows a persistent notification. Once the token has expired, Home Assistant automatically asks for re-authentication:

1. Open the Revotion App > Settings > Share Configuration
2. Copy the configuration JSON (see [Configuration](#configuration) for the QR code workaround)
3. Paste it into the re-authentication dialog in Home Assistant

Your devices, entities, and automations are preserved.

## Diagnostics

Download diagnostics from **Settings > Devices & Services > Revotion > 3 dots > Download diagnostics**. Includes MQTT connection status, token expiry, Brain/Node inventory, firmware versions, and recent error history. Sensitive data is automatically redacted (tokens fully, MAC addresses partially).

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Entities show "unavailable" | Check if Brain is online in the Revotion App |
| No real-time updates | Verify MQTT connection in diagnostics download |
| Token expired | Follow token renewal steps above |
| New node not appearing | Wait 5 seconds after pairing, then check HA |

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

Copyright 2026 Revotion GmbH

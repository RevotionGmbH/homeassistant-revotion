"""Constants for the Revotion integration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

if TYPE_CHECKING:
    from .api_client import RevotionApiClient
    from .coordinator import RevotionCoordinator
    from .mqtt_client import RevotionMqttClient

DOMAIN = "revotion"
MANUFACTURER = "Revotion"

# Config entry data keys
CONF_TOKEN = "token"
CONF_TOKEN_EXPIRY = "token_expiry"
CONF_BRAIN_MAC = "brain_mac"
CONF_BRAIN_NAME = "brain_name"
CONF_SUBSCRIPTION_TYPE = "subscription_type"
CONF_SUBSCRIPTION_EXPIRY = "subscription_expiry"

# API
# Dedicated Home Assistant endpoint (public Let's Encrypt TLS), separate from
# the app/iot hosts so HA traffic has its own edge rules and the right cert.
API_BASE_URL = "https://api-ha.revotion.net"
API_TIMEOUT = 10

# MQTT
# HA listener (8885) on its own hostname; public Let's Encrypt server cert,
# username/password auth (no client cert). See the platform infra repo.
MQTT_HOST = "mqtt-ha.revotion.net"
MQTT_PORT = 8885

# MQTT topic patterns (use .format(mac=brain_mac))
TOPIC_DATA = "{mac}/data"
TOPIC_CONFIG = "{mac}/config"
TOPIC_STATUS = "{mac}/status"
TOPIC_GPS = "{mac}/gps"
TOPIC_ERROR = "{mac}/error"
TOPIC_PAIR = "{mac}/pair"
TOPIC_CONTROL = "{mac}/ctr"
TOPIC_CONTROL_CONFIG = "{mac}/ctr_config"
TOPIC_CONTROL_DATA = "{mac}/ctr_data"

# Persistent-notification body when a command gets no MQTT echo within the
# timeout. Shared by all three command paths (native switch, RevotionCommandMixin,
# ConnectCommandMixin) so the wording cannot drift again. Persistent
# notifications bypass the translation system, hence plain English.
COMMAND_TIMEOUT_MESSAGE = (
    "No confirmation received for **{entity}** within {timeout}s. The command may not have been executed."
)

# Firmware user-error code 0x1005: the Brain marks a node with this after
# 3 unanswered ESP-NOW status polls (2 in light sleep) and pushes the updated
# error list on {mac}/error; the node's own (empty) list replaces it once the
# node answers again. Same signal the Revotion app uses for "not connected".
ERROR_CODE_NODE_NOT_AVAILABLE = 4101

# Platforms
PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


class ConnectionInterface(IntEnum):
    """Brain network interface reported by REST 'interface' / MQTT 'intf'."""

    CELLULAR = 0
    WIFI = 1


# Maps the raw interface integer to a stable HA enum-sensor option string.
# Used by the Brain "Connection type" sensor and its state translations.
CONNECTION_INTERFACE_LABELS: dict[int, str] = {
    ConnectionInterface.CELLULAR: "cellular",
    ConnectionInterface.WIFI: "wifi",
}

# Ordered option list for the enum sensor (must match translation state keys).
CONNECTION_INTERFACE_OPTIONS: list[str] = ["cellular", "wifi"]


class CapabilityType(IntEnum):
    """Revotion capability types matching Brain firmware cap_type enum.

    Brain firmware (brain_structures.h):
        UNCONFIGURED_CAP=0, BRAIN_CAP=1, SWITCH_CAP=2, TEMP_CAP=3,
        AMB_CAP=4, BAT_CAP=5, LEVEL_CAP=6, GPS_CAP=7, HC_CAP=8,
        RESERVED=9-10, TWO_WAY_CAP=11, CONNECT_CAP=12,
        AMB_3CH_CAP=13 (Multiwhite), SW_5CH_CAP=14 (Multiswitch).

    Only the values modelled by the integration are listed as members.

    Multi-channel note: a Multiwhite (13) node carries N AMB_3CH caps and a
    Multiswitch (14) node carries N SW_5CH caps, one per channel, each with its
    own ``cap_index``. The Brain serializes them with the *same* per-channel
    payload as single-channel Ambient (4) / Switch (2) -- they share
    ``form_ambient_data_json`` / ``form_switch_data_json`` (Brain_v2_ESPNOW
    bg95_remote.c:1423). So the integration reuses RevotionAmbientLight /
    RevotionSwitch and simply maps 13->light, 14->switch.

    These values are stored in the database (Capability.capabilityType)
    and returned by the REST API in both inventory and data endpoints.
    """

    SWITCH = 2
    TEMPERATURE = 3
    AMBIENT = 4
    BATTERY = 5
    LEVEL = 6
    HIGH_CURRENT = 8
    TWO_WAY = 11
    # Connectivity gateway to a third-party device (heater, fridge, alarm, ...).
    # Polymorphic: the concrete device is identified by a numeric ConnectDevice
    # code that only arrives with the first /data message, not in the inventory.
    CONNECT = 12
    # Multi-channel variants of Ambient (4) / Switch (2). Same per-channel
    # payload as their single-channel counterparts; a node carries one cap per
    # channel (e.g. [13,13,13] = 3-channel Multiwhite, [14,14,14,14,14] =
    # 5-channel Multiswitch), each at its own cap_index 0..N-1.
    AMB_3CH = 13
    SW_5CH = 14


class ConnectDevice(IntEnum):
    """Third-party device codes carried by a Connect capability (cap 12).

    The code arrives as ``device`` inside the cap-12 /data payload (and via
    /sync), never in the REST inventory. See Ha-Integration-Docs/connect-integration.md §1.
    """

    AIRTRONIC3 = 256
    TRUMA_COMBI = 512
    TRUMA_CPP = 514
    VE_DIRECT = 768
    THITRONIK = 1024
    ECOFLOW = 1280
    ALDE = 1536
    DOMETIC_FRIDGE = 1792
    DOMETIC_FRESHJET = 1793
    DOMETIC_FRIDGE_ABS = 1794
    AUTOTERM = 2048


CAPABILITY_PLATFORM_MAP: dict[CapabilityType, Platform] = {
    CapabilityType.SWITCH: Platform.SWITCH,
    CapabilityType.TEMPERATURE: Platform.SENSOR,
    CapabilityType.AMBIENT: Platform.LIGHT,
    CapabilityType.BATTERY: Platform.SENSOR,
    CapabilityType.LEVEL: Platform.SENSOR,
    CapabilityType.TWO_WAY: Platform.COVER,
    CapabilityType.HIGH_CURRENT: Platform.SENSOR,
    # Multi-channel: each channel becomes its own light/switch entity.
    CapabilityType.AMB_3CH: Platform.LIGHT,
    CapabilityType.SW_5CH: Platform.SWITCH,
}

# Human-readable labels for capability types, used as fallback device names
# when no user-configured name is available from the sync endpoint.
CAPABILITY_TYPE_LABELS: dict[CapabilityType, str] = {
    CapabilityType.SWITCH: "Switch",
    CapabilityType.TEMPERATURE: "Temperature",
    CapabilityType.AMBIENT: "Ambient",
    CapabilityType.BATTERY: "Battery",
    CapabilityType.LEVEL: "Level",
    CapabilityType.HIGH_CURRENT: "High Current",
    CapabilityType.TWO_WAY: "Two-Way",
    CapabilityType.CONNECT: "Connect",
    CapabilityType.AMB_3CH: "Multiwhite",
    CapabilityType.SW_5CH: "Multiswitch",
}

# Plain-name labels per Connect device code, used for device/entity naming
# until a config name from the app is available. Kept locale-neutral
# (brand + product name as the manufacturer markets them).
CONNECT_DEVICE_LABELS: dict[ConnectDevice, str] = {
    ConnectDevice.AIRTRONIC3: "Eberspächer Airtronic 3",
    ConnectDevice.TRUMA_COMBI: "Truma Combi",
    ConnectDevice.TRUMA_CPP: "Truma CP+",
    ConnectDevice.VE_DIRECT: "Victron VE.Direct",
    ConnectDevice.THITRONIK: "Thitronik WiPro III",
    ConnectDevice.ECOFLOW: "EcoFlow PowerKit",
    ConnectDevice.ALDE: "Alde 3030",
    ConnectDevice.DOMETIC_FRIDGE: "Dometic Fridge (Compressor)",
    ConnectDevice.DOMETIC_FRESHJET: "Dometic FreshJet",
    ConnectDevice.DOMETIC_FRIDGE_ABS: "Dometic Fridge (Absorber)",
    ConnectDevice.AUTOTERM: "Autoterm",
}


class BatterySubEntity(StrEnum):
    """Sub-entity types for Battery capability (Type 5)."""

    SOC = "soc"
    VOLTAGE = "voltage"
    CURRENT = "current"
    TEMPERATURE = "temperature"
    TIME_REMAINING = "time_remaining"
    TIME_TO_FULL = "time_to_full"
    CHARGE_CYCLES = "charge_cycles"
    CHARGING_STAGE = "charging_stage"


@dataclass
class RevotionData:
    """Runtime data stored on ConfigEntry.runtime_data."""

    api_client: RevotionApiClient
    coordinator: RevotionCoordinator
    mqtt_client: RevotionMqttClient


type RevotionConfigEntry = ConfigEntry[RevotionData]

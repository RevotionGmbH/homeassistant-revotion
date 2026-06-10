"""Diagnostics support for the Revotion integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_BRAIN_MAC, CONF_TOKEN, RevotionConfigEntry
from .models import Brain, Node, normalize_mac

TO_REDACT = {CONF_TOKEN}

# Any MAC-shaped substring (colon/dash separated or bare 12 hex digits), for
# scrubbing free-text fields such as the error history (D-02).
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b|\b[0-9A-Fa-f]{12}\b")


def _redact_mac(mac: str) -> str:
    """Partially redact MAC: show only last 4 chars (D-02)."""
    normalized = normalize_mac(mac)
    if len(normalized) >= 4:
        return f"****{normalized[-4:]}"
    return "****"


def _redact_macs_in_text(text: str) -> str:
    """Replace every MAC-shaped substring with its partially redacted form."""
    return _MAC_RE.sub(lambda m: _redact_mac(m.group(0)), text)


def _redact_error_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Scrub MACs from the free-text fields of an error-history entry."""
    return {key: _redact_macs_in_text(value) if isinstance(value, str) else value for key, value in entry.items()}


def _serialize_capability(cap: Any) -> dict[str, Any]:
    """Serialize a single capability for diagnostics output."""
    return {
        "index": cap.capability_index,
        "type": cap.capability_type.name,
        "data": cap.data,
        "config_name": cap.config.name,
    }


def _serialize_node(node: Node) -> dict[str, Any]:
    """Serialize a node with redacted MAC for diagnostics output."""
    return {
        "mac_address": _redact_mac(node.mac_address),
        "firmware_version": node.firmware_version,
        "hardware_revision": node.hardware_revision,
        "node_number": node.node_number,
        "reachable": node.reachable,
        "capabilities": [_serialize_capability(c) for c in node.capabilities],
    }


def _serialize_brain(brain: Brain) -> dict[str, Any]:
    """Serialize the full Brain tree with redacted MACs for diagnostics."""
    return {
        "mac_address": _redact_mac(brain.mac_address),
        "name": brain.name,
        "firmware_version": brain.firmware_version,
        "hardware_revision": brain.hardware_revision,
        "is_online": brain.is_online,
        "last_connection": brain.last_connection,
        "connection_interface": brain.connection_interface,
        "variant": brain.variant,
        "nodes": [_serialize_node(n) for n in brain.nodes],
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RevotionConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    mqtt = entry.runtime_data.mqtt_client

    # entry.data carries the full Brain MAC (it doubles as the MQTT username)
    # and diagnostics dumps end up attached to public GitHub issues -- apply
    # the same partial redaction as the brain/node tree (D-02).
    entry_data = async_redact_data(dict(entry.data), TO_REDACT)
    if CONF_BRAIN_MAC in entry_data:
        entry_data[CONF_BRAIN_MAC] = _redact_mac(entry.data[CONF_BRAIN_MAC])

    return {
        "entry_data": entry_data,
        "subscription": {
            "type": entry.data.get("subscription_type"),
            "expiry": entry.data.get("subscription_expiry"),
        },
        "mqtt": {
            "connected": mqtt.is_connected,
            "host": mqtt._host,
            "port": mqtt._port,
            "message_count": mqtt.message_count,
            "reconnect_count": mqtt.reconnect_count,
            "stale_reconnect_count": mqtt.stale_reconnect_count,
            "seconds_since_last_message": mqtt.seconds_since_last_message,
        },
        "rest_polling": {
            "poll_count": coordinator.rest_poll_count,
            "error_count": coordinator.rest_poll_errors,
        },
        "brain": _serialize_brain(coordinator.data) if coordinator.data else None,
        "token_expiry": entry.data.get("token_expiry"),
        "error_history": [_redact_error_entry(e) for e in coordinator.error_history],
        "coordinator_last_update_success": coordinator.last_update_success,
    }

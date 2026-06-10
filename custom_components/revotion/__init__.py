"""Revotion Home Assistant Integration."""

from __future__ import annotations

import logging
import ssl
from datetime import UTC, datetime

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import RevotionApiClient, RevotionAuthError, RevotionConnectionError
from .const import (
    CONF_BRAIN_MAC,
    CONF_BRAIN_NAME,
    CONF_SUBSCRIPTION_EXPIRY,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
    MANUFACTURER,
    MQTT_HOST,
    MQTT_PORT,
    PLATFORMS,
    RevotionConfigEntry,
    RevotionData,
)
from .coordinator import RevotionCoordinator
from .models import Brain, format_mac_for_display, normalize_mac, sync_connect_device_names
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)

TOKEN_EXPIRY_WARNING_DAYS = 14


async def async_setup_entry(hass: HomeAssistant, entry: RevotionConfigEntry) -> bool:
    """Set up Revotion from a config entry."""
    session = async_get_clientsession(hass)
    token = entry.data[CONF_TOKEN]
    brain_mac = entry.data[CONF_BRAIN_MAC]

    # 1. Create API client and validate token
    client = RevotionApiClient(session, token, brain_mac)

    try:
        await client.async_get_brain_status(brain_mac)
    except RevotionAuthError as err:
        raise ConfigEntryAuthFailed("Token expired or invalid") from err
    except RevotionConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Revotion API: {err}") from err

    _check_token_expiry(hass, entry)
    _check_subscription_expiry(hass, entry)

    # 2. Create TLS context in executor thread (non-blocking)
    # The HA endpoints (mqtt-ha / api-ha) present public Let's Encrypt certs, so
    # we verify them fully against the system trust store: create_default_context()
    # enables certificate validation (CERT_REQUIRED) and hostname checking.
    def _create_tls_context() -> ssl.SSLContext:
        return ssl.create_default_context()

    tls_context = await hass.async_add_executor_job(_create_tls_context)

    # 3. Create MQTT client with deferred coordinator wiring
    coordinator: RevotionCoordinator | None = None

    def _on_mqtt_message(topic: str, payload: bytes) -> None:
        if coordinator is not None:
            coordinator.handle_mqtt_message(topic, payload)

    def _on_mqtt_connected() -> None:
        _LOGGER.info("MQTT connected to %s for Brain %s", MQTT_HOST, brain_mac)
        if coordinator is not None:
            hass.async_create_task(coordinator.async_on_mqtt_connected())

    def _on_mqtt_disconnected() -> None:
        _LOGGER.warning("MQTT connection lost for Brain %s, reconnecting", brain_mac)

    mqtt_client = RevotionMqttClient(
        host=MQTT_HOST,
        port=MQTT_PORT,
        username=format_mac_for_display(brain_mac),
        password=token,
        tls_context=tls_context,
        brain_mac=format_mac_for_display(brain_mac),
        on_message=_on_mqtt_message,
        on_connected=_on_mqtt_connected,
        on_disconnected=_on_mqtt_disconnected,
    )

    # 4. Create coordinator and perform first refresh
    coordinator = RevotionCoordinator(hass, entry, client, mqtt_client)
    await coordinator.async_config_entry_first_refresh()

    # 5. Register Brain as hub device (before entity platforms)
    _register_brain_device(hass, entry, coordinator.data)

    # 6. Store runtime data
    entry.runtime_data = RevotionData(
        api_client=client,
        coordinator=coordinator,
        mqtt_client=mqtt_client,
    )

    # 7. Start MQTT background task (auto-cancelled on entry unload)
    entry.async_create_background_task(
        hass,
        mqtt_client._connection_loop(),
        f"revotion_mqtt_{entry.entry_id}",
    )

    # 8. Forward to entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 9. Keep Connect node device names in sync with live app config renames
    # (has_entity_name model: the device carries the name). Runs on every
    # coordinator update -- e.g. the debounced REST re-pull after a /config event.
    def _sync_connect_device_names() -> None:
        if coordinator.data is not None:
            sync_connect_device_names(hass, coordinator.data)

    _sync_connect_device_names()  # initial pass once platforms have registered devices
    entry.async_on_unload(coordinator.async_add_listener(_sync_connect_device_names))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RevotionConfigEntry) -> bool:
    """Unload a Revotion config entry."""
    # Disconnect MQTT client
    await entry.runtime_data.mqtt_client.disconnect()
    # Background task is auto-cancelled by entry.async_create_background_task

    # Unload entity platforms
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: RevotionConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to delete a stale node device from the UI.

    HA only renders the per-device "Delete" button when this hook returns True.
    We permit removal of any *node* device whose MAC is no longer present in the
    Brain's live node inventory -- e.g. an orphan left behind when a node was
    re-paired and the Brain handed it a new MAC (the old device then lingers
    with all entities ``unavailable``).

    Guards against losing live data: the Brain hub device itself, and any node
    the Brain still reports, are never removable here -- a live device must keep
    its entities, history and automation references intact. Those only go
    ``unavailable`` on a transient LTE-M/REST hiccup and must come back cleanly.
    """
    brain_mac = normalize_mac(config_entry.data[CONF_BRAIN_MAC])
    device_macs = {ident for domain, ident in device_entry.identifiers if domain == DOMAIN}

    # Never offer to remove the Brain hub via this path.
    if brain_mac in device_macs:
        return False

    brain = config_entry.runtime_data.coordinator.data
    live_macs = {normalize_mac(node.mac_address) for node in brain.nodes} if brain else set()

    # Removable only when NONE of the device's MACs are still in the live tree.
    return device_macs.isdisjoint(live_macs)


def _register_brain_device(hass: HomeAssistant, entry: RevotionConfigEntry, brain: Brain) -> None:
    """Register Brain as hub device in HA device registry."""

    brain_mac = normalize_mac(entry.data[CONF_BRAIN_MAC])
    registry = dr.async_get(hass)
    # Use config entry name as fallback when REST API returns empty boardName
    name = brain.name or entry.data.get(CONF_BRAIN_NAME, "Brain")
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, brain_mac)},
        manufacturer=MANUFACTURER,
        model="Brain",
        name=name,
        sw_version=brain.firmware_version,
        hw_version=brain.hardware_revision if brain.hardware_revision else None,
    )


def _check_token_expiry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create persistent notification if token expires within 14 days."""
    expiry_str = entry.data.get(CONF_TOKEN_EXPIRY)
    if not expiry_str:
        return

    expiry = datetime.fromisoformat(expiry_str)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    remaining = expiry - datetime.now(tz=UTC)

    if remaining.days < TOKEN_EXPIRY_WARNING_DAYS:
        brain_name = entry.data.get(CONF_BRAIN_NAME, "Unknown")
        persistent_notification.async_create(
            hass,
            message=(
                f"Your Revotion token for **{brain_name}** "
                f"expires in {remaining.days} days.\n\n"
                "Home Assistant will ask for re-authentication once it "
                "expires. Copy the new configuration JSON from the Revotion "
                "App (Settings > Share Configuration) and paste it there."
            ),
            title="Revotion Token Expiring Soon",
            notification_id=f"revotion_token_expiry_{entry.entry_id}",
        )


def _check_subscription_expiry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create persistent notification if subscription expires within 14 days."""
    expiry_str = entry.data.get(CONF_SUBSCRIPTION_EXPIRY)
    if not expiry_str:
        return

    expiry = datetime.fromisoformat(expiry_str)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    remaining = expiry - datetime.now(tz=UTC)

    if remaining.days < TOKEN_EXPIRY_WARNING_DAYS:
        brain_name = entry.data.get(CONF_BRAIN_NAME, "Unknown")
        persistent_notification.async_create(
            hass,
            message=(
                f"Your Revotion subscription for **{brain_name}** "
                f"expires in {remaining.days} days.\n\n"
                "Renew your subscription in the Revotion App."
            ),
            title="Revotion Subscription Expiring Soon",
            notification_id=f"revotion_subscription_expiry_{entry.entry_id}",
        )

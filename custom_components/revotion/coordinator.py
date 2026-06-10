"""DataUpdateCoordinator for the Revotion integration.

Implements a push/poll hybrid pattern: MQTT messages are the primary data
channel (via async_set_updated_data), REST polling at 60s acts as fallback
when MQTT is disconnected. Manages Brain online/offline state and performs
one-time REST sync on MQTT reconnect or Brain coming online.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import RevotionApiClient, RevotionAuthError, RevotionConnectionError
from .const import (
    CONF_BRAIN_MAC,
    CONF_BRAIN_NAME,
    DOMAIN,
    ERROR_CODE_NODE_NOT_AVAILABLE,
    ConnectionInterface,
    RevotionConfigEntry,
)
from .models import Brain, CapabilityConfig, find_node, normalize_mac
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)

MAX_ERROR_HISTORY = 50


class RevotionCoordinator(DataUpdateCoordinator[Brain]):
    """Coordinator for Revotion Brain data.

    Central data hub that receives MQTT push messages (primary), falls back
    to REST polling (60s), and notifies all entity listeners when data changes.
    Also manages Brain online/offline state and performs REST sync on reconnect.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: RevotionConfigEntry,
        api_client: RevotionApiClient,
        mqtt_client: RevotionMqttClient,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance.
            entry: Config entry for this Brain.
            api_client: REST API client for fallback polling.
            mqtt_client: MQTT client for push data.

        """
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=entry,
            name=f"Revotion {entry.data[CONF_BRAIN_NAME]}",
            # MQTT push is the primary channel; REST is a safety-net poll. A full
            # inventory re-fetch also runs on MQTT (re)connect and on every
            # pair/unpair event, so a long 5 min interval is plenty -- it only
            # exists to recover anything missed while MQTT was silent.
            update_interval=timedelta(minutes=5),
        )
        self.api_client = api_client
        self.mqtt_client = mqtt_client
        self._brain_mac: str = entry.data[CONF_BRAIN_MAC]
        self._gps_data: dict[str, Any] | None = None
        self._error_history: deque[dict[str, Any]] = deque(maxlen=MAX_ERROR_HISTORY)
        self._rest_poll_count: int = 0
        self._rest_poll_errors: int = 0
        # Coalesce a burst of MQTT /config notifications into one REST re-pull.
        self._config_resync_scheduled: bool = False

    def record_error(self, error_type: str, detail: str) -> None:
        """Record an error for diagnostics (D-03)."""
        self._error_history.append(
            {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "type": error_type,
                "detail": detail,
            }
        )

    @property
    def error_history(self) -> list[dict[str, Any]]:
        """Return error history as list for diagnostics."""
        return list(self._error_history)

    @property
    def rest_poll_count(self) -> int:
        """Return total REST polls since startup."""
        return self._rest_poll_count

    @property
    def rest_poll_errors(self) -> int:
        """Return total REST poll errors since startup."""
        return self._rest_poll_errors

    @property
    def commands_allowed(self) -> bool:
        """Return True unless the Brain is connected via cellular (LTE-M).

        Commands are the only traffic this integration causes on the Brain's
        metered LTE-M SIM (inbound data is published by the Brain regardless),
        so writes are WiFi-only. Only an explicit CELLULAR report blocks --
        ``None`` (interface not yet known, e.g. right after startup) must not
        lock out controls.
        """
        if self.data is None:
            return True
        return self.data.connection_interface != ConnectionInterface.CELLULAR

    def assert_commands_allowed(self) -> None:
        """Raise ``ServiceValidationError`` if the Brain is on LTE-M.

        Called at the top of every ``ctr_data`` publish path (native switch,
        ambient light, RevotionCommandMixin, ConnectCommandMixin) so the SIM
        cost guard lives in exactly one place.
        """
        if not self.commands_allowed:
            raise ServiceValidationError(
                "Commands are only possible over WiFi.",
                translation_domain=DOMAIN,
                translation_key="commands_blocked_cellular",
            )

    async def _async_update_data(self) -> Brain:
        """Safety-net REST poll (every 5 min) of the full Brain/node tree.

        Always re-fetches over REST (the inventory is the authoritative paired-
        node list); MQTT push keeps state fresh in between. After a successful
        fetch, prunes device-registry devices for nodes that have dropped out of
        the inventory (see :meth:`_reconcile_node_devices`).

        Returns:
            Brain dataclass with full node/capability tree.

        Raises:
            ConfigEntryAuthFailed: On authentication error (triggers reauth).
            UpdateFailed: On connection error.

        """
        self._rest_poll_count += 1
        try:
            brain = await self.api_client.async_get_brain_status(self._brain_mac)
            # Status response doesn't include mac_address — set from config
            brain.mac_address = self._brain_mac
            brain.name = self.config_entry.data.get(CONF_BRAIN_NAME, "")
            nodes = await self.api_client.async_get_inventory(self._brain_mac)
            brain.nodes = nodes
            node_data = await self.api_client.async_get_node_data(self._brain_mac)
            self._apply_node_data(brain, node_data)
            # Fetch sync data to populate capability config names
            await self._apply_sync_config(brain)
            await self._apply_node_errors(brain)
            await self._async_fetch_gps()
            self._reconcile_node_devices(brain)
            return brain
        except RevotionAuthError as err:
            raise ConfigEntryAuthFailed from err
        except RevotionConnectionError as err:
            self._rest_poll_errors += 1
            self.record_error("rest_error", str(err))
            raise UpdateFailed(f"Failed to update Brain {self._brain_mac} data: {err}") from err

    async def async_sync_from_rest(self) -> None:
        """Perform one-time REST sync to recover data missed during downtime.

        Called after MQTT reconnect (D-02) or Brain coming online (D-13).
        Best-effort recovery -- logs warnings on failure but does not crash.
        """
        try:
            brain = await self.api_client.async_get_brain_status(self._brain_mac)
            brain.mac_address = self._brain_mac
            brain.name = self.config_entry.data.get(CONF_BRAIN_NAME, "")
            nodes = await self.api_client.async_get_inventory(self._brain_mac)
            brain.nodes = nodes
            node_data = await self.api_client.async_get_node_data(self._brain_mac)
            self._apply_node_data(brain, node_data)
            # Fetch sync data to populate capability config names
            await self._apply_sync_config(brain)
            await self._apply_node_errors(brain)
            await self._async_fetch_gps()
            self._reconcile_node_devices(brain)
            self.async_set_updated_data(brain)
            _LOGGER.info(
                "REST sync completed for Brain %s after reconnect",
                self._brain_mac,
            )
        except RevotionConnectionError as err:
            self.record_error("rest_sync_error", str(err))
            _LOGGER.warning(
                "REST sync after reconnect failed for Brain %s: %s",
                self._brain_mac,
                err,
            )
        except RevotionAuthError:
            self.record_error("auth_error", f"Auth failed during REST sync for Brain {self._brain_mac}")
            _LOGGER.error(
                "Auth failed during REST sync for Brain %s",
                self._brain_mac,
            )

    async def async_on_mqtt_connected(self) -> None:
        """Handle MQTT reconnection event (D-02).

        Called by __init__.py's on_connected closure when MQTT reconnects.
        Triggers REST sync to recover data missed during downtime.
        """
        _LOGGER.info(
            "MQTT reconnected for Brain %s, REST sync triggered",
            self._brain_mac,
        )
        await self.async_sync_from_rest()

    async def _resync_config_from_mqtt(self) -> None:
        """Debounced authoritative config re-pull after an MQTT /config event.

        The /config payload is treated as a *notification* that capability config
        (names, images, feature flags) changed; the full, consistent config is
        then pulled over REST, exactly like the inventory re-sync on pair/unpair.
        A burst of /config messages is coalesced into one sync: while a re-sync is
        already pending, further events are ignored (the pending sync will pick up
        all changes). The flag clears after the debounce window so a later change
        schedules a fresh sync.
        """
        if self._config_resync_scheduled:
            return
        self._config_resync_scheduled = True
        try:
            await asyncio.sleep(2.5)  # debounce: coalesce a burst of /config msgs
        finally:
            self._config_resync_scheduled = False
        await self.async_sync_from_rest()

    async def _handle_pair_event(self, data: dict[str, Any]) -> None:
        """Handle pair/unpair event from MQTT (D-05, D-06, D-07).

        Both actions re-fetch the authoritative inventory over REST (after a
        short debounce so the backend has settled). The fresh inventory drives
        discovery for new nodes and device pruning for removed ones via
        :meth:`_reconcile_node_devices` -- so a removed node's device disappears
        without a separate code path.
        """
        action = data.get("action", "")
        node_mac = normalize_mac(data.get("MAC", data.get("mac", "")))

        if action == "paired":
            _LOGGER.info("Node %s paired, syncing inventory after debounce", node_mac)
            await asyncio.sleep(2.5)  # D-06: debounce
            await self.async_sync_from_rest()
        elif action == "unpaired":
            _LOGGER.info("Node %s unpaired, re-syncing inventory after debounce", node_mac)
            # Optimistically drop the node locally so its entities go unavailable
            # at once; the debounced REST sync below then re-fetches the inventory
            # (node gone) and _reconcile_node_devices removes the stale device. If
            # the backend still lists it (race), the sync re-adds it -> self-heals.
            if self.data is not None:
                remaining = [n for n in self.data.nodes if normalize_mac(n.mac_address) != node_mac]
                if len(remaining) != len(self.data.nodes):
                    self.data.nodes = remaining
                    self.async_set_updated_data(self.data)
            await asyncio.sleep(2.5)
            await self.async_sync_from_rest()
        else:
            _LOGGER.warning("Unknown pair action '%s' for node %s", action, node_mac)

    def _reconcile_node_devices(self, brain: Brain) -> None:
        """Remove device-registry devices for nodes no longer in the inventory.

        The REST inventory is the source of truth for which nodes are paired to
        the Brain. Any *node* device whose MAC is absent from a freshly fetched,
        non-empty inventory is a stale orphan (e.g. a node re-paired with a new
        MAC) and is removed together with its entities.

        Guards against data loss:
        - The Brain hub device (keyed by the Brain MAC) is never removed.
        - Runs only when the inventory is non-empty, so a transient backend
          hiccup that yields an empty list (``_parse_inventory`` returns ``[]``
          for a non-list response) cannot wipe every live device. A Brain that
          genuinely has zero nodes keeps its orphans for manual deletion via
          ``async_remove_config_entry_device`` (the per-device UI button).
        """
        if not brain.nodes:
            return
        live_macs = {normalize_mac(node.mac_address) for node in brain.nodes}
        brain_mac = normalize_mac(self._brain_mac)
        registry = dr.async_get(self.hass)
        entry_devices = dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
        pruned = 0
        for device in entry_devices:
            device_macs = {ident for domain, ident in device.identifiers if domain == DOMAIN}
            # Skip the Brain hub and any device without a revotion MAC identifier.
            if not device_macs or brain_mac in device_macs:
                continue
            if device_macs.isdisjoint(live_macs):
                _LOGGER.info("Pruning stale node device %s (no longer in inventory)", device_macs)
                registry.async_remove_device(device.id)
                pruned += 1
        _LOGGER.debug(
            "Reconcile Brain %s: %d live nodes, %d entry devices, %d pruned",
            brain_mac,
            len(live_macs),
            len(entry_devices),
            pruned,
        )

    def handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Process incoming MQTT message and update Brain tree.

        Routes messages by topic suffix (data, status, config, gps, error, pair).
        Updates coordinator.data and pushes to all entity listeners via
        async_set_updated_data.

        Args:
            topic: Full MQTT topic string (e.g. "aabbccddeeff/data").
            payload: Raw message payload bytes.

        """
        try:
            data: dict[str, Any] = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.debug("Invalid JSON on topic %s", topic)
            return

        topic_suffix = topic.split("/", 1)[1] if "/" in topic else ""

        match topic_suffix:
            case "data":
                self._handle_data_payload(data)
            case "status":
                self._handle_status_payload(data)
            case "config":
                # Optimistic in-memory update for instant feedback, then pull the
                # full, authoritative config over REST -- same principle as the
                # inventory re-sync on pair/unpair (MQTT = "something changed"
                # notification, REST = source of truth). A burst of /config
                # messages (the app often pushes several capabilities at once) is
                # coalesced into a single debounced sync.
                self._handle_config_payload(data)
                self.hass.async_create_task(
                    self._resync_config_from_mqtt(),
                    f"revotion_config_resync_{self._brain_mac}",
                )
            case "gps":
                self._gps_data = data
                _LOGGER.debug("GPS data received for Brain %s", self._brain_mac)
                self.async_set_updated_data(self.data)
            case "error":
                self._handle_error_payload(data)
            case "pair":
                _LOGGER.info(
                    "Pair event: %s %s",
                    data.get("MAC", data.get("mac", "unknown")),
                    data.get("action", "unknown"),
                )
                self.hass.async_create_task(
                    self._handle_pair_event(data),
                    f"revotion_pair_{self._brain_mac}",
                )
            case _:
                _LOGGER.debug("Ignoring unknown MQTT topic: %s", topic)
                return

        self.async_set_updated_data(self.data)

    def _handle_data_payload(self, data: dict[str, Any]) -> None:
        """Update node capability data from MQTT data payload.

        The firmware /data payload is FLAT — value fields live at the root
        alongside MAC and cap_index, e.g. {"MAC":"...","cap_index":0,"val":24.56}
        or {"MAC":"...","cap_index":0,"device":1024,"dev_data":{...}}. There is no
        nested "data" wrapper (see Brain_v2_ESPNOW/docs/node_payload_schemas.md and
        Ha-Integration-Docs/connect-integration.md § 2.1). Mirrors _apply_node_data (REST path).

        Args:
            data: Parsed JSON payload with MAC, cap_index and value fields.

        """
        mac = normalize_mac(data.get("MAC", data.get("mac", "")))
        cap_index = data.get("capabilityIndex", data.get("cap_index", -1))

        # Extract payload data: everything except the MAC and cap_index keys.
        payload = {k: v for k, v in data.items() if k not in ("MAC", "mac", "cap_index", "capabilityIndex")}

        for node in self.data.nodes:
            if normalize_mac(node.mac_address) == mac:
                for capability in node.capabilities:
                    if capability.capability_index == cap_index:
                        capability.data = payload
                        return
                _LOGGER.debug(
                    "Capability index %s not found on node %s",
                    cap_index,
                    mac,
                )
                return

        _LOGGER.debug("Node %s not found in Brain tree", mac)

    @staticmethod
    def _user_errors_mean_reachable(user_errors: Any) -> bool | None:
        """Map a device error entry's "User" list to node reachability.

        The firmware replaces the whole error list on every change, so the
        presence/absence of 4101 in a fresh list is authoritative: 4101 present
        -> unreachable, any other (or empty) list -> reachable. Returns None
        for a malformed entry (no "User" list) so callers leave the current
        state untouched.
        """
        if not isinstance(user_errors, list):
            return None
        return ERROR_CODE_NODE_NOT_AVAILABLE not in user_errors

    def _handle_error_payload(self, data: dict[str, Any]) -> None:
        """Update node reachability from an MQTT error payload.

        The Brain pushes one {mac}/error message per device whenever its error
        list changes: {"MAC": <node-mac>, "User": [...], "Dev": [...],
        "Cap_errors": [...]} for nodes, {"MAC": <brain-mac>, "User": [...],
        "Backend": [...], "ESPNOW": [...]} for the Brain itself. User-error
        4101 marks a node the Brain cannot reach over ESP-NOW; its absence in
        a fresh list marks recovery. Brain entries match no node and only end
        up in the diagnostics history.
        """
        device_mac = data.get("MAC", data.get("mac", "unknown"))
        self.record_error("mqtt_device_error", f"Device {device_mac}: {data}")
        _LOGGER.warning("Error from device %s: %s", device_mac, data)

        node = find_node(self.data, normalize_mac(str(device_mac)))
        if node is None:
            return
        reachable = self._user_errors_mean_reachable(data.get("User"))
        if reachable is None or reachable == node.reachable:
            return
        node.reachable = reachable
        _LOGGER.info(
            "Node %s is now %s",
            device_mac,
            "reachable" if reachable else "unreachable (error 4101)",
        )

    async def _apply_node_errors(self, brain: Brain) -> None:
        """Seed node reachability from the persisted REST error lists.

        The MQTT {mac}/error pushes are not retained, so without this a node
        that went unreachable before an HA (re)start would show as connected
        until the next error push. Best-effort: when the fetch fails, the
        current in-memory flags are carried over so a transient endpoint
        hiccup cannot flip every node back to "reachable".
        """
        try:
            entries = await self.api_client.async_get_errors(self._brain_mac)
        except Exception:
            _LOGGER.debug("Error-list REST fetch failed for Brain %s", self._brain_mac)
            if self.data is not None:
                for node in brain.nodes:
                    previous = find_node(self.data, normalize_mac(node.mac_address))
                    if previous is not None:
                        node.reachable = previous.reachable
            return

        reachable_by_mac: dict[str, bool] = {}
        for entry in entries:
            reachable = self._user_errors_mean_reachable(entry.get("User"))
            if reachable is not None:
                reachable_by_mac[normalize_mac(str(entry.get("MAC", "")))] = reachable

        # Nodes without an error entry have never reported errors -> reachable.
        for node in brain.nodes:
            node.reachable = reachable_by_mac.get(normalize_mac(node.mac_address), True)

    def _handle_status_payload(self, data: dict[str, Any]) -> None:
        """Update Brain online/offline state from MQTT status payload.

        Per D-13: When Brain comes online (isOnline=1), schedules REST sync
        to refresh current state before MQTT data resumes.

        Args:
            data: Parsed JSON payload with isOnline field.

        """
        is_online = bool(data.get("isOnline", 0))
        self.data.is_online = is_online

        intf = data.get("intf")
        if intf is not None:
            try:
                self.data.connection_interface = int(intf)
            except (TypeError, ValueError):
                _LOGGER.debug(
                    "Invalid intf value on status for Brain %s: %r",
                    self._brain_mac,
                    intf,
                )

        if is_online:
            _LOGGER.info("Brain %s back online", self._brain_mac)
            self.hass.async_create_task(self.async_sync_from_rest())
        else:
            _LOGGER.info("Brain %s reported offline", self._brain_mac)

    def _handle_config_payload(self, data: dict[str, Any]) -> None:
        """Update node capability config from MQTT config payload.

        The wire payload is FLAT -- config fields (e.g. the ambient ``typ``)
        live at the root next to MAC/cap_index/name/image, exactly as the
        api-ha backend parses it (parseMqttConfigPayload) and re-serves it in
        /brain/sync. Everything except the addressing/name/image keys becomes
        ``config.data``, mirroring ``_apply_sync_config``.

        Args:
            data: Parsed JSON payload with MAC, cap_index, name, image and
                flat config fields.

        """
        mac = normalize_mac(data.get("MAC", data.get("mac", "")))
        cap_index = data.get("capabilityIndex", data.get("cap_index", -1))

        for node in self.data.nodes:
            if normalize_mac(node.mac_address) == mac:
                for capability in node.capabilities:
                    if capability.capability_index == cap_index:
                        capability.config = CapabilityConfig(
                            name=data.get("name", ""),
                            image=data.get("image", ""),
                            data={
                                k: v
                                for k, v in data.items()
                                if k not in ("MAC", "mac", "cap_index", "capabilityIndex", "name", "image")
                            },
                        )
                        return
                _LOGGER.debug(
                    "Capability index %s not found on node %s for config",
                    cap_index,
                    mac,
                )
                return

        _LOGGER.debug("Node %s not found in Brain tree for config update", mac)

    def _apply_node_data(self, brain: Brain, node_data: dict[str, Any]) -> None:
        """Map REST node data response onto Brain.nodes[].capabilities[].data.

        The API returns data grouped by firmware capability type (not by node):
        {
            "2": [{"MAC": "aa:bb:...", "cap_index": 0, "val": 1, ...}],
            "5": [{"MAC": "aa:bb:...", "cap_index": 0, "soc": 85, ...}]
        }

        Each entry's payload fields (excluding MAC/cap_index) become
        the capability's data dict.
        """
        if not isinstance(node_data, dict):
            _LOGGER.warning("Node data response is not a dict: %s", type(node_data))
            return

        applied = 0
        for _cap_type_str, entries in node_data.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                entry_mac = normalize_mac(entry.get("MAC", ""))
                cap_index = entry.get("cap_index", -1)

                # Extract payload data (everything except MAC and cap_index)
                payload = {k: v for k, v in entry.items() if k not in ("MAC", "cap_index")}

                for node in brain.nodes:
                    if normalize_mac(node.mac_address) == entry_mac:
                        for capability in node.capabilities:
                            if capability.capability_index == cap_index:
                                capability.data = payload
                                applied += 1
                                break
                        break

        _LOGGER.debug("Applied REST data to %d capabilities", applied)

    async def _async_fetch_gps(self) -> None:
        """Fetch the latest GPS payload via REST and store it (best-effort).

        Empty {} responses must NOT overwrite existing MQTT GPS data.
        Failures are silently swallowed so callers are never interrupted.
        """
        try:
            gps = await self.api_client.async_get_brain_gps(self._brain_mac)
        except Exception:
            _LOGGER.debug("GPS REST fetch failed for Brain %s", self._brain_mac)
            return
        if isinstance(gps, dict) and gps:
            self._gps_data = gps

    async def _apply_sync_config(self, brain: Brain) -> None:
        """Fetch sync endpoint and apply capability config names to Brain tree.

        The sync endpoint (GET /brain/sync/:mac) returns user-configured
        capability names and images that are not available from the inventory
        endpoint. This populates CapabilityConfig.name on each capability,
        which is then used by _register_node_device for device naming.

        Best-effort: logs warning on failure but does not crash.
        """
        try:
            sync_data = await self.api_client.async_get_sync(self._brain_mac)
        except Exception:
            _LOGGER.warning(
                "Failed to fetch sync data for Brain %s, device names may be generic",
                self._brain_mac,
            )
            return

        sync_nodes = sync_data.get("Nodes", [])
        if not isinstance(sync_nodes, list):
            return

        applied = 0
        for sync_entry in sync_nodes:
            if not isinstance(sync_entry, dict):
                continue
            entry_mac = normalize_mac(sync_entry.get("MAC", ""))
            cap_index = sync_entry.get("cap_index", -1)
            name = sync_entry.get("name", "")
            image = sync_entry.get("image", "")

            for node in brain.nodes:
                if normalize_mac(node.mac_address) == entry_mac:
                    for capability in node.capabilities:
                        if capability.capability_index == cap_index:
                            capability.config = CapabilityConfig(
                                name=name,
                                image=image,
                                data={
                                    k: v
                                    for k, v in sync_entry.items()
                                    if k not in ("MAC", "cap_index", "name", "image")
                                },
                            )
                            applied += 1
                            break
                    break

        _LOGGER.debug("Applied sync config to %d capabilities", applied)

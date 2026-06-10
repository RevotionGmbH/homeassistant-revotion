"""Data models for the Revotion integration."""

from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_call_later

from .const import (
    CAPABILITY_TYPE_LABELS,
    COMMAND_TIMEOUT_MESSAGE,
    DOMAIN,
    MANUFACTURER,
    TOPIC_CONTROL_DATA,
    CapabilityType,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .const import RevotionConfigEntry

_LOGGER = logging.getLogger(__name__)

# Revert the optimistic assumption if no MQTT echo lands within this window.
# Command round-trips are currently LTE-M (up to ~5 s); the Brain is slated to
# move to a WiFi backend later, which will shrink the round-trip -- 60 s stays
# generous either way.
COMMAND_TIMEOUT_S = 60


def normalize_mac(mac: str) -> str:
    """Normalize MAC address to lowercase without separators.

    Input formats: 'AA:BB:CC:DD:EE:FF', 'aa:bb:cc:dd:ee:ff', 'AABBCCDDEEFF', 'AA-BB-CC-DD-EE-FF'
    Output: 'aabbccddeeff'
    """
    return mac.lower().replace(":", "").replace("-", "")


def format_mac_for_display(mac: str) -> str:
    """Format MAC address for display: 'aa:bb:cc:dd:ee:ff'."""
    normalized = normalize_mac(mac)
    return ":".join(normalized[i : i + 2] for i in range(0, 12, 2))


@dataclass
class CapabilityConfig:
    """Configuration for a capability (from /config topic or REST sync).

    ``data`` carries the app's flat config fields (e.g. the ambient ``typ``).
    Values are JSON scalars; the REST sync serves them as strings ("2"),
    while MQTT may deliver raw numbers -- consumers must parse defensively.
    """

    name: str = ""
    image: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capability:
    """A single capability on a Node."""

    capability_index: int
    capability_type: CapabilityType
    config: CapabilityConfig = field(default_factory=CapabilityConfig)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Node:
    """A Revotion Node device (child of Brain)."""

    mac_address: str
    firmware_version: str
    hardware_revision: str
    node_type: str
    node_number: int
    # ESP-NOW link to the Brain. False while the firmware reports user-error
    # 4101 (node not available) for this node -- via MQTT {mac}/error pushes
    # and the persisted REST /brain/error/:mac state. Defaults to True so a
    # node without any error report counts as connected.
    reachable: bool = True
    capabilities: list[Capability] = field(default_factory=list)


def find_node(brain: Brain | None, node_mac: str) -> Node | None:
    """Return the node with the given normalized MAC, or None.

    ``node_mac`` must already be normalized (lowercase, no separators).
    """
    if brain is None:
        return None
    for node in brain.nodes:
        if normalize_mac(node.mac_address) == node_mac:
            return node
    return None


def get_node_device_name(node: Node) -> str:
    """Determine the best display name for a node device.

    Connect nodes use the has_entity_name model: the *device* carries the
    app-configured name (and its entities are bare fields like "Gas"), so the
    name is the Connect capability's ``config.name`` (e.g. "Alde Compact"). This
    is kept in sync with live app renames by :func:`sync_connect_device_names`.
    Until the config name has arrived it falls through to the label/number form.

    Native nodes keep the capability-type label (e.g. "NODE Switch") -- their
    entities still carry the per-capability name themselves.
    """
    for cap in node.capabilities:
        if cap.capability_type == CapabilityType.CONNECT and cap.config.name:
            return cap.config.name

    if node.capabilities:
        seen_labels: list[str] = []
        for cap in node.capabilities:
            label = CAPABILITY_TYPE_LABELS.get(cap.capability_type)
            if label and label not in seen_labels:
                seen_labels.append(label)
        if seen_labels:
            return f"NODE {', '.join(seen_labels)}"

    return f"NODE {node.node_number}"


def get_node_model_name(node: Node) -> str:
    """Determine the model string for a node device.

    Returns "Node {Type}" (e.g. "Node Switch", "Node Battery").
    Falls back to "Node" if no capability type is known.
    """
    if node.capabilities:
        seen_labels: list[str] = []
        for cap in node.capabilities:
            label = CAPABILITY_TYPE_LABELS.get(cap.capability_type)
            if label and label not in seen_labels:
                seen_labels.append(label)
        if seen_labels:
            return f"Node {', '.join(seen_labels)}"

    return node.node_type or "Node"


@dataclass
class Brain:
    """A Revotion Brain device (hub)."""

    mac_address: str
    name: str
    firmware_version: str
    hardware_revision: str
    is_online: bool = False
    # Unix timestamp of the Brain's last connection (REST 'lastConnection').
    last_connection: int | None = None
    # Active network interface: 0 = cellular, 1 = wifi.
    # Populated from REST 'interface' (status v2.6.0) and MQTT 'intf' field.
    connection_interface: int | None = None
    # Hardware/feature variant reported by the API (REST 'variant', status v2.6.0).
    # Semantics are firmware-defined; surfaced raw for diagnostics.
    variant: int | None = None
    nodes: list[Node] = field(default_factory=list)


# MQTT Payload dataclasses (per D-14, IMPLEMENTATION_PLAN Section 5)


@dataclass
class DataPayload:
    """Payload from {mac}/data topic."""

    mac: str
    cap_index: int
    data: dict[str, Any]


@dataclass
class StatusPayload:
    """Payload from {mac}/status topic."""

    is_online: bool


@dataclass
class GpsPayload:
    """Payload from {mac}/gps topic."""

    utc: int
    latitude: float
    longitude: float
    altitude: float
    speed: float
    hdop: float
    cog: float


@dataclass
class ConfigPayload:
    """Payload from {mac}/config topic."""

    mac: str
    cap_index: int
    name: str
    image: str
    config_data: dict[str, str]


@dataclass
class PairPayload:
    """Payload from {mac}/pair topic."""

    mac: str
    action: str


@dataclass
class ErrorPayload:
    """Payload from {mac}/error topic."""

    mac: str
    error_data: dict[str, Any]


# --- Shared helpers (extracted from platform files) ---


def format_timer_attributes(timer: dict[str, Any] | None) -> dict[str, Any]:
    """Extract timer attributes from timer dict.

    Returns dict with timer_state, timer_remaining, timer_scheduled_utc
    when data is present and active. Omits keys when inactive or missing.
    """
    attrs: dict[str, Any] = {}
    if timer is None:
        return attrs

    timer_state_val = timer.get("timer_state")
    if timer_state_val is None:
        return attrs

    attrs["timer_state"] = "active" if timer_state_val else "inactive"

    if timer_state_val:
        set_utc = timer.get("set_UTC")
        cur_utc = timer.get("cur_UTC")
        if set_utc is not None and cur_utc is not None:
            remaining_secs = max(0, set_utc - cur_utc)
            hours = remaining_secs // 3600
            minutes = (remaining_secs % 3600) // 60
            seconds = remaining_secs % 60
            attrs["timer_remaining"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        if set_utc is not None:
            with contextlib.suppress(OSError, ValueError):
                attrs["timer_scheduled_utc"] = datetime.fromtimestamp(set_utc, tz=UTC).isoformat()

    return attrs


def register_node_device(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    node: Node,
    brain_mac: str,
) -> None:
    """Register Node as child device with via_device pointing to Brain.

    Device naming priority (via get_node_device_name):
    1. User-configured name from sync endpoint / MQTT /config
    2. Descriptive label from capability type (e.g. "Battery", "Temperature")
    3. Final fallback: "Node {node_number}"

    Idempotent -- dr.async_get_or_create is safe to call multiple times.
    """
    from homeassistant.helpers import device_registry as dr

    node_mac = normalize_mac(node.mac_address)
    brain_mac_normalized = normalize_mac(brain_mac)
    registry = dr.async_get(hass)

    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, node_mac)},
        manufacturer=MANUFACTURER,
        model=get_node_model_name(node),
        name=get_node_device_name(node),
        sw_version=node.firmware_version,
        hw_version=node.hardware_revision or None,
        via_device=(DOMAIN, brain_mac_normalized),
    )


def sync_connect_device_names(hass: HomeAssistant, brain: Brain) -> None:
    """Keep Connect node device names in sync with their live app config name.

    has_entity_name model: a Connect node's *device* carries the name and its
    entities are bare fields. When the app renames the capability (MQTT /config
    -> debounced REST re-pull -> coordinator update) this updates the device
    registry's ``name`` so every field-entity re-renders with the new name.

    Only the registry ``name`` (the "original" name) is touched -- a manual HA
    rename (``name_by_user``) is preserved, because HA always shows
    ``name_by_user`` over ``name``. No-op for devices not yet registered and for
    native (non-Connect) nodes, whose entities carry their own name.
    """
    from homeassistant.helpers import device_registry as dr

    registry = dr.async_get(hass)
    for node in brain.nodes:
        if not any(cap.capability_type == CapabilityType.CONNECT for cap in node.capabilities):
            continue
        device = registry.async_get_device(identifiers={(DOMAIN, normalize_mac(node.mac_address))})
        if device is None:
            continue
        desired = get_node_device_name(node)
        # device.name is the registry "original" name; only update on a real
        # change to avoid spurious registry writes on every coordinator tick.
        if desired and device.name != desired:
            registry.async_update_device(device.id, name=desired)


class RevotionCapabilityMixin:
    """Mixin for entities that read from a specific node capability.

    Provides _find_capability() and _build_base_payload() to avoid
    duplicating these methods across switch, light, sensor, and
    binary_sensor entity classes.

    Requires: self._node_mac (normalized), self._cap_index (int),
    self.coordinator (DataUpdateCoordinator[Brain]).
    """

    _node_mac: str
    _cap_index: int

    def _find_node(self) -> Node | None:
        """Locate this entity's node in coordinator data."""
        return find_node(self.coordinator.data, self._node_mac)  # type: ignore[attr-defined]

    def _node_reachable(self) -> bool:
        """Return True while the node's ESP-NOW link to the Brain is up.

        Feeds the ``available`` property of every capability entity so a node
        the Brain cannot reach shows up as "unavailable" in HA -- mirroring
        the "not connected" indicator in the Revotion app.
        """
        node = self._find_node()
        return node is not None and node.reachable

    def _find_capability(self) -> Capability | None:
        """Locate the capability in coordinator data."""
        node = self._find_node()
        if node is None:
            return None
        for cap in node.capabilities:
            if cap.capability_index == self._cap_index:
                return cap
        return None

    def _build_base_payload(self) -> dict[str, Any]:
        """Build base MQTT payload with MAC and cap_index."""
        return {
            "MAC": format_mac_for_display(self._node_mac),
            "cap_index": self._cap_index,
        }


class RevotionCommandMixin:
    """Optimistic MQTT-command publishing for writable native capabilities.

    Shared command lock + 60 s timeout + optimistic-state lifecycle, cleared on
    the real MQTT echo. Mirrors the proven switch.py / ConnectCommandMixin
    mechanism but transport-generic: the host hands a *complete* flat ``ctr_data``
    payload to :meth:`_publish_command` (no Connect ``device``/``dev_data``
    wrapper). The native Switch (Type 2) keeps its own inline copy for now; new
    multi-state native entities (Two-Way cover) build on this.

    The rationale is the command round-trip (currently LTE-M, up to ~5 s; the
    Brain is slated to move to a WiFi backend later, which will shrink it):
    without an optimistic assumption the coordinator overwrites the UI before the
    echo arrives and the control bounces back.

    The host entity owns *what* it is optimistic about (a bool, a direction, ...)
    in its own attribute and consults it in its state property; this mixin only
    owns the lifecycle -- set on send, cleared once the echo *confirms* the
    value (the host calls :meth:`_sync_command_state` from its
    ``_handle_coordinator_update`` and overrides :meth:`_optimistic_confirmed`
    with its value comparison) or on timeout (this calls
    :meth:`_revert_optimistic`, which the host overrides to drop its
    assumption). Confirmation matters because the coordinator fires for every
    incoming MQTT message: clearing on just any update would bounce the UI
    back while the echo is still in flight.

    Combine with :class:`RevotionCapabilityMixin` (for ``_build_base_payload``)
    and a HA ``CoordinatorEntity`` subclass. Requires the host to set, in its
    ``__init__``: ``self._brain_mac`` (display format, ``ctr_data`` topic owner)
    and ``self._mqtt_client``; and to call :meth:`_init_command_state`.
    """

    _brain_mac: str

    def _init_command_state(self) -> None:
        """Initialise optimistic-command bookkeeping. Call from ``__init__``."""
        self._command_sent_at: float | None = None
        self._command_pending: bool = False
        self._timeout_cancel: CALLBACK_TYPE | None = None

    def _cancel_command_timeout(self) -> None:
        """Cancel the pending command-timeout callback, if any."""
        if self._timeout_cancel is not None:
            self._timeout_cancel()
            self._timeout_cancel = None

    def _clear_command_lock(self) -> None:
        """Release the command lock + timeout and log the turnaround.

        Invoked via :meth:`_sync_command_state` once the echo confirms the
        command. The host clears its own optimistic value (via
        :meth:`_revert_optimistic`); this clears the shared lock so a new
        command can go out.
        """
        if self._command_sent_at is not None:
            elapsed = time.monotonic() - self._command_sent_at
            _LOGGER.debug(
                "TURNAROUND %s: %.2fs (command -> MQTT echo)",
                self.entity_id or self.unique_id,  # type: ignore[attr-defined]
                elapsed,
            )
            self._command_sent_at = None
        self._cancel_command_timeout()
        self._command_pending = False

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any in-flight command timeout when the entity is removed.

        The 60 s timeout is scheduled via ``async_call_later``; without this it
        would survive a reload/unpair and fire against a dead entity. Hosts that
        override this must call ``super().async_will_remove_from_hass()``.
        """
        self._cancel_command_timeout()
        self._command_pending = False
        self._command_sent_at = None
        await super().async_will_remove_from_hass()  # type: ignore[misc]

    @callback
    def _on_command_timeout(self, _now: Any) -> None:
        """Revert optimistic state and notify when no echo arrives in time."""
        self._timeout_cancel = None
        self._command_pending = False
        self._command_sent_at = None
        self._revert_optimistic()
        entity_name = self.name or self.entity_id or self.unique_id  # type: ignore[attr-defined]
        _LOGGER.warning(
            "TIMEOUT %s: no MQTT echo within %ds",
            entity_name,
            COMMAND_TIMEOUT_S,
        )
        if self.hass is not None:  # type: ignore[attr-defined]
            # @callback context: schedule the service call, do not await it.
            self.hass.async_create_task(  # type: ignore[attr-defined]
                self.hass.services.async_call(  # type: ignore[attr-defined]
                    "persistent_notification",
                    "create",
                    {
                        "title": "Revotion Timeout",
                        "message": COMMAND_TIMEOUT_MESSAGE.format(entity=entity_name, timeout=COMMAND_TIMEOUT_S),
                        "notification_id": f"revotion_timeout_{self.unique_id}",  # type: ignore[attr-defined]
                    },
                )
            )
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption. Overridden by the host entity.

        Default is a no-op; most hosts reset their ``_optimistic_*`` attribute.
        """

    def _optimistic_confirmed(self) -> bool:
        """Return True once real data confirms the optimistic assumption.

        Overridden by the host entity to compare its ``_optimistic_*`` value(s)
        against the freshly arrived capability data. Must return True when no
        optimistic value is active (nothing to confirm). The default True keeps
        hosts without an override on the old clear-on-any-update behaviour.
        """
        return True

    def _sync_command_state(self) -> None:
        """Release lock + optimistic state once the echo confirms the command.

        Call from the host's ``_handle_coordinator_update`` *before*
        ``super()._handle_coordinator_update()``. The coordinator pushes an
        update for *every* incoming MQTT message (any capability, status, GPS),
        so clearing unconditionally would drop the optimistic value while the
        real echo is still in flight over LTE-M -- the UI would bounce back to
        the stale value. Instead the state is kept until the data actually
        matches (or the 60 s timeout reverts it).
        """
        if self._optimistic_confirmed():
            self._clear_command_lock()
            self._revert_optimistic()

    async def _publish_command(self, payload: dict[str, Any]) -> None:
        """Publish a flat ``ctr_data`` command under the single-command lock.

        Publishes ``payload`` on ``{brain-mac}/ctr_data`` and schedules a timeout
        that reverts the optimistic state. Raises if the Brain is on LTE-M
        (writes are WiFi-only, SIM cost guard), or ``HomeAssistantError`` if a
        command is still pending (no echo yet) so the UI surfaces "please wait".
        """
        from homeassistant.exceptions import HomeAssistantError

        self.coordinator.assert_commands_allowed()  # type: ignore[attr-defined]
        if self._command_pending:
            entity_name = str(self.name or self.entity_id)  # type: ignore[attr-defined]
            raise HomeAssistantError(
                f"A command for {entity_name} is still pending. Please wait.",
                translation_domain=DOMAIN,
                translation_key="command_pending",
                translation_placeholders={"entity": entity_name},
            )

        topic = TOPIC_CONTROL_DATA.format(mac=self._brain_mac)
        self._command_sent_at = time.monotonic()
        self._command_pending = True
        self._cancel_command_timeout()
        if self.hass is not None:  # type: ignore[attr-defined]
            self._timeout_cancel = async_call_later(
                self.hass,  # type: ignore[attr-defined]
                COMMAND_TIMEOUT_S,
                self._on_command_timeout,
            )
        _LOGGER.debug(
            "COMMAND SENT %s -> %s: %s",
            self.entity_id or self.unique_id,  # type: ignore[attr-defined]
            topic,
            payload,
        )
        try:
            await self._mqtt_client.async_publish(topic, json.dumps(payload))  # type: ignore[attr-defined]
        except Exception:
            self._command_pending = False
            self._command_sent_at = None
            self._cancel_command_timeout()
            raise

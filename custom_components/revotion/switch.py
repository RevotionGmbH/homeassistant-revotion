"""Switch platform for the Revotion integration.

Implements Switch (Type 2) entities with MQTT control command publishing.
TwoWay (Type 11) is NOT here -- it is a cover (OPEN/CLOSE/STOP), see cover.py.
HighCurrent (Type 8) is explicitly NOT handled here -- it is remapped to
Platform.SENSOR per SWIT-03 override / Pitfall 6.

State updates use optimistic mode: after sending a command, the entity
immediately assumes the new state to prevent UI bounce-back over slow
LTE-M connections (up to 5s round-trip). The optimistic state is kept until a
coordinator update *confirms* the commanded value (or the 60 s timeout
reverts it) -- the coordinator fires for every incoming MQTT message, so
clearing on just any update would bounce the UI back to the stale value
while the echo is still in flight.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    bool_to_int01,
    get_descriptor,
    has_descriptor,
    int01_to_bool,
    is_path_available,
    read_dev_data_path,
    reconcile_gated_entities,
    resolve_connect_device,
)
from .connect.control import ConnectCommandMixin, set_dev_data_path
from .connect.descriptors import SwitchSpec
from .connect.entity import resolve_entity_category, resolve_switch_device_class
from .const import (
    COMMAND_TIMEOUT_MESSAGE,
    CONF_BRAIN_MAC,
    DOMAIN,
    TOPIC_CONTROL_DATA,
    CapabilityType,
    RevotionConfigEntry,
)
from .coordinator import RevotionCoordinator
from .models import (
    RevotionCapabilityMixin,
    format_mac_for_display,
    format_timer_attributes,
    normalize_mac,
    register_node_device,
)
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)

COMMAND_TIMEOUT_S = 60


def _format_fuse(value: int | None) -> str | None:
    """Map fuse value: 0 -> 'ok', 1 -> 'blown', None -> None."""
    if value is None:
        return None
    return "blown" if value else "ok"


class RevotionSwitchEntity(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SwitchEntity):
    """Base switch entity for Revotion capabilities.

    Uses optimistic state management: after sending a command, the entity
    assumes the new state immediately to prevent UI bounce-back. The
    optimistic state is cleared on the next coordinator update so real
    MQTT echo data takes over.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    _attr_assumed_state = False

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        mqtt_client: RevotionMqttClient,
        translation_key: str,
        config_name: str = "",
        channel: int | None = None,
    ) -> None:
        """Initialize the switch entity.

        ``channel`` (1-based) is set only for Multiswitch (SW_5CH) channels: when
        no config_name is provided, the entity falls back to the "switch_channel"
        translation_key ("Channel {channel}") instead of the plain translation_key.
        """
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._mqtt_client = mqtt_client
        self._optimistic_state: bool | None = None
        self._command_sent_at: float | None = None
        self._command_pending: bool = False
        self._timeout_cancel: CALLBACK_TYPE | None = None
        if config_name:
            self._attr_name = config_name
        elif channel is not None:
            self._attr_translation_key = "switch_channel"
            self._attr_translation_placeholders = {"channel": str(channel)}
        else:
            self._attr_translation_key = translation_key
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it.

        This is called by CoordinatorEntity whenever the coordinator fires
        async_set_updated_data -- which happens for *every* incoming MQTT
        message (any capability, status, GPS), not just this switch's echo.
        Clearing unconditionally would drop the optimistic state while the
        echo is still in flight over LTE-M and bounce the UI back to the
        stale value, so the state is kept until the data matches the
        commanded value (or the 60 s timeout reverts it).
        """
        if self._optimistic_confirmed():
            if self._command_sent_at is not None:
                elapsed = time.monotonic() - self._command_sent_at
                _LOGGER.debug(
                    "TURNAROUND %s: %.2fs (command → MQTT echo)",
                    self.entity_id or self._attr_unique_id,
                    elapsed,
                )
                self._command_sent_at = None
            if self._timeout_cancel is not None:
                self._timeout_cancel()
                self._timeout_cancel = None
            self._command_pending = False
            self._optimistic_state = None
        super()._handle_coordinator_update()

    def _optimistic_confirmed(self) -> bool:
        """Return True once capability data matches the optimistic state.

        True when no optimistic state is active (nothing to confirm). A
        missing capability or ``val`` keeps the optimistic state -- the 60 s
        timeout is the safety net if the echo never arrives.
        """
        if self._optimistic_state is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        val = cap.data.get("val")
        return val is not None and bool(val) == self._optimistic_state

    @callback
    def _handle_command_timeout(self, _now: Any) -> None:
        """Handle command timeout — notify user and revert optimistic state."""
        self._timeout_cancel = None
        self._command_pending = False
        self._command_sent_at = None
        self._optimistic_state = None
        entity_name = self.name or self.entity_id or self._attr_unique_id
        _LOGGER.warning(
            "TIMEOUT %s: no MQTT echo within %ds",
            entity_name,
            COMMAND_TIMEOUT_S,
        )
        if self.hass is not None:
            # @callback context: hass.services.call (sync) would deadlock the
            # event loop (run_coroutine_threadsafe(...).result() on the running
            # loop) -- schedule the async variant instead, like the mixins do.
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Revotion Switch Timeout",
                        "message": COMMAND_TIMEOUT_MESSAGE.format(entity=entity_name, timeout=COMMAND_TIMEOUT_S),
                        "notification_id": f"revotion_timeout_{self._attr_unique_id}",
                    },
                )
            )
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    async def _publish_command(self, payload: dict[str, Any]) -> None:
        """Publish command to MQTT ctr_data topic.

        Raises if the Brain is on LTE-M (writes are WiFi-only, SIM cost guard).
        Blocks if a previous command is still pending (no MQTT echo yet).
        Schedules a timeout that reverts state and notifies the user.
        """
        self.coordinator.assert_commands_allowed()
        if self._command_pending:
            from homeassistant.exceptions import HomeAssistantError

            entity_name = str(self.name or self.entity_id)
            raise HomeAssistantError(
                f"A command for {entity_name} is still pending. Please wait.",
                translation_domain=DOMAIN,
                translation_key="command_pending",
                translation_placeholders={"entity": entity_name},
            )
        topic = TOPIC_CONTROL_DATA.format(mac=self._brain_mac)
        self._command_sent_at = time.monotonic()
        self._command_pending = True
        if self._timeout_cancel is not None:
            self._timeout_cancel()
        if self.hass is not None:
            self._timeout_cancel = async_call_later(self.hass, COMMAND_TIMEOUT_S, self._handle_command_timeout)
        _LOGGER.debug(
            "COMMAND SENT %s → %s: %s",
            self.entity_id or self._attr_unique_id,
            topic,
            payload,
        )
        try:
            await self._mqtt_client.async_publish(topic, json.dumps(payload))
        except Exception:
            self._command_pending = False
            self._command_sent_at = None
            if self._timeout_cancel is not None:
                self._timeout_cancel()
                self._timeout_cancel = None
            raise

    def _set_optimistic_state(self, state: bool) -> None:
        """Set optimistic state and push to HA if entity is registered."""
        self._optimistic_state = state
        if self.hass is not None:
            self.async_write_ha_state()


class RevotionSwitch(RevotionSwitchEntity):
    """Switch entity for Type 2 capabilities."""

    @property
    def is_on(self) -> bool | None:
        """Return True if switch is on (val == 1).

        Returns optimistic state if a command was recently sent and
        coordinator hasn't pushed new data yet.
        """
        if self._optimistic_state is not None:
            return self._optimistic_state
        cap = self._find_capability()
        if cap is None:
            return None
        val = cap.data.get("val")
        if val is None:
            return None
        return bool(val)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes: voltage, current, fuse, timer (D-15)."""
        cap = self._find_capability()
        if cap is None:
            return {}

        attrs: dict[str, Any] = {}
        data = cap.data

        if "volt" in data:
            attrs["voltage"] = data["volt"]
        if "cur" in data:
            attrs["current"] = data["cur"]
        if "fuse" in data:
            attrs["fuse"] = _format_fuse(data["fuse"])

        # Timer attributes (D-15)
        timer = data.get("timer")
        attrs.update(format_timer_attributes(timer))

        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch by publishing val=1 to ctr_data."""
        payload = self._build_base_payload()
        payload["val"] = 1
        await self._publish_command(payload)
        self._set_optimistic_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch by publishing val=0 to ctr_data."""
        payload = self._build_base_payload()
        payload["val"] = 0
        await self._publish_command(payload)
        self._set_optimistic_state(False)


class RevotionConnectSwitch(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    SwitchEntity,
):
    """Descriptor-driven writable switch on a Connect device (e.g. Airtronic eco).

    Distinct from the native Switch capabilities above: the toggle lives inside
    a Connect device's ``dev_data`` (0/1 flag). Read decodes via int01_to_bool;
    write publishes ``{write_key: 0/1}`` on ``ctr_data`` with optimistic state
    (LTE-M round-trip up to 5 s), cleared on the MQTT echo.
    """

    _attr_has_entity_name = True
    # assumed_state=False -> HA renders the standard toggle, not the two
    # flash on/off buttons. The optimistic anti-bounce-back is handled purely by
    # _optimistic_state winning in is_on() (cleared on the MQTT echo / 60 s
    # timeout); it does NOT need assumed_state. Connect switches DO report their
    # real 0/1 state back via dev_data, so the state is confirmed (not "assumed")
    # once the echo lands -- matching the native switches (RevotionSwitchEntity).
    _attr_assumed_state = False

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: SwitchSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize a descriptor-driven Connect switch."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_state: bool | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: the device carries the name, the entity is just
        # the field ("Gas"). No config_name prefix -> HA shows "<device> Gas",
        # not "<device> <app name> Gas".
        self._attr_name = spec.name
        self._attr_device_class = resolve_switch_device_class(spec.device_class)
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the capability exists and (if gated) is available.

        ``available_path`` gates accessory switches (Dometic Absorber
        ``fan_one_av`` etc.). These are *presence-gated*: the discovery listener
        removes the switch outright when the flag is falsy (connect/discovery.py),
        so this is mainly a safety net for the brief window before removal. A
        missing flag is treated as available (shared is_path_available semantics).
        """
        cap = self._find_capability()
        if not (super().available and self._node_reachable() and cap is not None):
            return False
        return is_path_available(cap, self._spec.available_path)

    @property
    def is_on(self) -> bool | None:
        """Return the decoded 0/1 flag (optimistic value wins until echo)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        cap = self._find_capability()
        if cap is None:
            return None
        return int01_to_bool(read_dev_data_path(cap, self._spec.path))

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption on command timeout."""
        self._optimistic_state = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once the dev_data flag matches the optimistic state."""
        if self._optimistic_state is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        real = int01_to_bool(read_dev_data_path(cap, self._spec.path))
        return real is not None and real == self._optimistic_state

    def _build_switch_command(self, value: bool) -> dict[str, Any]:
        """Build the control dev_data for the toggle (0/1 + extra fields).

        ``write_key`` is a dotted path so nested toggles (Truma
        ``comb_water.state``) write as nested objects; a flat key (Airtronic
        ``eco``) just sets a top-level field.
        """
        dev_data: dict[str, Any] = {}
        set_dev_data_path(dev_data, self._spec.write_key, bool_to_int01(value))
        for path, extra in self._spec.extra_command_fields.items():
            set_dev_data_path(dev_data, path, extra)
        return dev_data

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on by publishing write_key=1."""
        await self._publish_connect_command(self._build_switch_command(True))
        self._optimistic_state = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off by publishing write_key=0."""
        await self._publish_connect_command(self._build_switch_command(False))
        self._optimistic_state = False
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion switch entities with dynamic discovery support.

    Creates switch entities for Switch (Type 2), plus descriptor-driven Connect
    switches (Type 12, deferred). TwoWay (Type 11) is a cover (see cover.py).
    HighCurrent (Type 8) is explicitly NOT handled here -- remapped to SENSOR
    per SWIT-03 / Pitfall 6.
    """
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    brain_norm = normalize_mac(brain_mac)
    known_node_macs: set[str] = set()

    def _check_device() -> None:
        """Check for new nodes and add switch entities dynamically."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        new_macs = current_macs - known_node_macs
        if not new_macs:
            return
        known_node_macs.update(new_macs)

        entities: list[RevotionSwitchEntity] = []
        for node in coordinator.data.nodes:
            if normalize_mac(node.mac_address) not in new_macs:
                continue
            register_node_device(hass, entry, node, brain_mac)

            for capability in node.capabilities:
                match capability.capability_type:
                    case CapabilityType.SWITCH:
                        entities.append(
                            RevotionSwitch(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                mqtt_client=mqtt_client,
                                translation_key="switch",
                                config_name=capability.config.name,
                            )
                        )
                    case CapabilityType.SW_5CH:
                        # Multiswitch: N independent channels, one SW_5CH cap each
                        # (own cap_index). Payload is byte-identical to a plain
                        # Switch, so RevotionSwitch is reused. Without a config_name
                        # the channel number (1-based) names the entity.
                        entities.append(
                            RevotionSwitch(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                mqtt_client=mqtt_client,
                                translation_key="switch",
                                config_name=capability.config.name,
                                channel=capability.capability_index + 1,
                            )
                        )
                    # TwoWay (Type 11) is NOT handled here -- it is a cover
                    # (OPEN/CLOSE/STOP), see cover.py / RevotionTwoWayCover.
                    # HighCurrent (Type 8) intentionally NOT handled here
                    # Remapped to Platform.SENSOR per SWIT-03 / Pitfall 6

        if entities:
            async_add_entities(entities)

    # Connect (Type 12) switches use deferred discovery (descriptor + dev_data
    # only known after the first /data message) and are presence-gated: each
    # SwitchSpec's available_path (Dometic Absorber fan_one_av / ice_maker_av
    # ...) decides whether the switch should exist *now*, so the listener adds
    # it when the flag turns on and removes it (live + registry) when it turns
    # off. Tracked at (node, cap, key) granularity.
    connect_entities: dict[tuple[str, int, str], RevotionConnectSwitch] = {}

    def _make_connect_switch(node, capability, device_code, spec):
        """Bind a per-spec factory (own scope avoids late-binding in the loop)."""

        def factory() -> RevotionConnectSwitch:
            register_node_device(hass, entry, node, brain_mac)
            return RevotionConnectSwitch(
                coordinator=coordinator,
                brain_mac=brain_mac,
                node_mac=node.mac_address,
                cap_index=capability.capability_index,
                device_code=device_code,
                spec=spec,
                mqtt_client=mqtt_client,
                config_name=capability.config.name,
            )

        return factory

    def _check_connect() -> None:
        """Reconcile presence-gated Connect switches once a device code resolves.

        Only devices with a tailored descriptor produce switches here; native
        Switch caps (above) are untouched, and devices without a descriptor
        never reach this platform (their dev_data is mirrored as read-only
        sensors).
        """
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        candidates = []
        for node in coordinator.data.nodes:
            node_mac = normalize_mac(node.mac_address)
            for capability in node.capabilities:
                if capability.capability_type != CapabilityType.CONNECT:
                    continue
                device_code = resolve_connect_device(capability)
                if device_code is None or not has_descriptor(device_code):
                    continue
                descriptor = get_descriptor(device_code)
                assert descriptor is not None
                for spec in descriptor.switches:
                    key = (node_mac, capability.capability_index, spec.key)
                    present = is_path_available(capability, spec.available_path)
                    unique_id = f"revotion_{brain_norm}_{node_mac}_{capability.capability_index}_{spec.key}"
                    candidates.append(
                        (key, present, unique_id, _make_connect_switch(node, capability, device_code, spec))
                    )

        reconcile_gated_entities(
            hass=hass,
            entity_domain="switch",
            entities=connect_entities,
            current_macs=current_macs,
            candidates=candidates,
            async_add_entities=async_add_entities,
        )

    def _on_update() -> None:
        """Coordinator listener: run native and Connect switch discovery."""
        _check_device()
        _check_connect()

    _on_update()  # Initial entity creation
    entry.async_on_unload(coordinator.async_add_listener(_on_update))

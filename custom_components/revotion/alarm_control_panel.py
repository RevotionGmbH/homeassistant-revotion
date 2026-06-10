"""Alarm control panel platform for the Revotion integration.

Hosts Connect-device alarm panels (Phase 1: Thitronik WiPro III, device 1024).
The platform is a generic registry dispatch: it iterates Connect capabilities,
and for any whose resolved ``device`` code has a descriptor with an
``alarm_panel`` spec it creates one :class:`RevotionConnectAlarmPanel`. Adding a
future alarm device is a descriptor change, not a platform change.

Discovery is deferred like the Connect sensors: the ``device`` code only arrives
with the first /data message after the node is paired, so entity creation runs
on every coordinator update and tracks which (node, cap) panels already exist.

Arming/disarming publishes a ``ctr_data`` command via :class:`ConnectCommandMixin`
with optimistic state (LTE-M round-trip up to 5 s).
"""

from __future__ import annotations

import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import get_descriptor, has_descriptor, int01_to_bool, read_dev_data_path, resolve_connect_device
from .connect.control import ConnectCommandMixin, connect_command_dev_data
from .connect.descriptors import AlarmPanelSpec
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class RevotionConnectAlarmPanel(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    AlarmControlPanelEntity,
):
    """Alarm control panel for a Connect alarm device (e.g. Thitronik).

    State derives from ``dev_data`` flags: TRIGGERED when the alarm flag is set,
    else ARMED_AWAY when armed, else DISARMED. Arm/disarm publish a command code
    on ``ctr_data`` and assume the new state optimistically until the MQTT echo
    arrives (cleared in ``_handle_coordinator_update``).
    """

    _attr_has_entity_name = True
    _attr_supported_features = AlarmControlPanelEntityFeature.ARM_AWAY
    _attr_code_arm_required = False
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: AlarmPanelSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the alarm control panel."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_state: AlarmControlPanelState | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the panel state from dev_data, or the optimistic assumption.

        TRIGGERED takes priority over armed/disarmed: a triggered alarm is the
        most safety-relevant state. The optimistic value (set on arm/disarm)
        wins until the coordinator echoes real data.
        """
        if self._optimistic_state is not None:
            return self._optimistic_state
        cap = self._find_capability()
        if cap is None:
            return None
        if self._spec.alarm_path is not None and int01_to_bool(read_dev_data_path(cap, self._spec.alarm_path)):
            return AlarmControlPanelState.TRIGGERED
        armed = int01_to_bool(read_dev_data_path(cap, self._spec.armed_path))
        if armed is None:
            return None
        return AlarmControlPanelState.ARMED_AWAY if armed else AlarmControlPanelState.DISARMED

    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption on command timeout."""
        self._optimistic_state = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once dev_data matches the optimistic armed/disarmed state.

        A real TRIGGERED report also confirms (returns True): the alarm firing
        is the most safety-relevant state and must replace the optimistic
        assumption immediately rather than being held back until the arm/disarm
        echo lands.
        """
        if self._optimistic_state is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        if self._spec.alarm_path is not None and int01_to_bool(read_dev_data_path(cap, self._spec.alarm_path)):
            return True
        armed = int01_to_bool(read_dev_data_path(cap, self._spec.armed_path))
        if armed is None:
            return False
        expected_armed = self._optimistic_state == AlarmControlPanelState.ARMED_AWAY
        return armed == expected_armed

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Arm the alarm system (away) by publishing the arm command."""
        await self._publish_connect_command(connect_command_dev_data(self._spec.arm_away_command))
        self._optimistic_state = AlarmControlPanelState.ARMED_AWAY
        self.async_write_ha_state()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Disarm the alarm system by publishing the disarm command."""
        await self._publish_connect_command(connect_command_dev_data(self._spec.disarm_command))
        self._optimistic_state = AlarmControlPanelState.DISARMED
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect alarm control panels via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known: set[tuple[str, int, str]] = set()

    def _check_connect() -> None:
        """Create alarm panels for Connect devices whose descriptor defines one."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known if key[0] not in current_macs}
        known.difference_update(stale)

        entities: list[RevotionConnectAlarmPanel] = []
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
                if descriptor.alarm_panel is None:
                    continue
                key = (node_mac, capability.capability_index, descriptor.alarm_panel.key)
                if key in known:
                    continue
                known.add(key)
                register_node_device(hass, entry, node, brain_mac)
                entities.append(
                    RevotionConnectAlarmPanel(
                        coordinator=coordinator,
                        brain_mac=brain_mac,
                        node_mac=node.mac_address,
                        cap_index=capability.capability_index,
                        device_code=device_code,
                        spec=descriptor.alarm_panel,
                        mqtt_client=mqtt_client,
                        config_name=capability.config.name,
                    )
                )

        if entities:
            async_add_entities(entities)

    _check_connect()
    entry.async_on_unload(coordinator.async_add_listener(_check_connect))

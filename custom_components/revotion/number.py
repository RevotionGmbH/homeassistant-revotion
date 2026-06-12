"""Number platform for the Revotion integration.

Hosts Connect-device number entities (Phase 3: Truma Combi water target temp,
device 512). Generic registry dispatch + deferred discovery, identical in shape
to climate.py/select.py.

Writing publishes the value at the spec's (possibly nested) write_path via the
Connect control plumbing, with optimistic state (LTE-M round-trip up to 5 s).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    get_descriptor,
    has_descriptor,
    is_path_available,
    read_dev_data_path,
    reconcile_gated_entities,
    resolve_connect_device,
)
from .connect.control import ConnectCommandMixin, set_dev_data_path
from .connect.descriptors import NumberSpec
from .connect.entity import resolve_entity_category, resolve_number_device_class, resolve_number_mode
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class RevotionConnectNumber(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    NumberEntity,
):
    """Number entity for a writable Connect setpoint (e.g. Truma water temp)."""

    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: NumberSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_value: float | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_mode = resolve_number_mode(spec.mode)
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = resolve_number_device_class(spec.device_class)
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the capability exists and (if gated) is available.

        ``available_path`` gates feature-flagged setpoints (Alde ``fuel_av``).
        These are *presence-gated*: the discovery listener removes the entity
        outright when the flag is falsy, so this is mainly a safety net for the
        brief window before removal.
        """
        cap = self._find_capability()
        if not (super().available and self._node_reachable() and cap is not None):
            return False
        return is_path_available(cap, self._spec.available_path)

    @property
    def native_value(self) -> float | None:
        """Return the setpoint (optimistic value wins until echo)."""
        if self._optimistic_value is not None:
            return self._optimistic_value
        cap = self._find_capability()
        if cap is None:
            return None
        value = read_dev_data_path(cap, self._spec.path)
        return float(value) if isinstance(value, (int, float)) else None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption on command timeout."""
        self._optimistic_value = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once the dev_data value matches the optimistic one."""
        if self._optimistic_value is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        real = read_dev_data_path(cap, self._spec.path)
        return isinstance(real, (int, float)) and abs(float(real) - self._optimistic_value) <= 0.05

    async def async_set_native_value(self, value: float) -> None:
        """Set the value by publishing it at the spec's write_path."""
        wire_value: Any = int(value) if self._spec.as_int else value
        dev_data: dict[str, Any] = {}
        set_dev_data_path(dev_data, self._spec.write_path, wire_value)
        for path, extra in self._spec.extra_command_fields.items():
            set_dev_data_path(dev_data, path, extra)
        await self._publish_connect_command(dev_data)
        # Track what actually went on the wire (as_int truncates), so display
        # and echo confirmation agree with the device.
        self._optimistic_value = float(wire_value)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect number entities via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    brain_norm = normalize_mac(brain_mac)
    # Presence-gated like Connect switches: each NumberSpec's available_path
    # (Alde fuel_av) decides whether the entity should exist *now*, so the
    # listener adds it when the flag turns on and removes it (live + registry)
    # when it turns off. Tracked at (node, cap, key) granularity.
    connect_entities: dict[tuple[str, int, str], RevotionConnectNumber] = {}

    def _make_connect_number(node, capability, device_code, spec):
        """Bind a per-spec factory (own scope avoids late-binding in the loop)."""

        def factory() -> RevotionConnectNumber:
            register_node_device(hass, entry, node, brain_mac)
            return RevotionConnectNumber(
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
        """Reconcile presence-gated number entities for descriptor devices."""
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
                for spec in descriptor.numbers:
                    key = (node_mac, capability.capability_index, spec.key)
                    present = is_path_available(capability, spec.available_path)
                    unique_id = f"revotion_{brain_norm}_{node_mac}_{capability.capability_index}_{spec.key}"
                    candidates.append(
                        (key, present, unique_id, _make_connect_number(node, capability, device_code, spec))
                    )

        reconcile_gated_entities(
            hass=hass,
            entity_domain="number",
            entities=connect_entities,
            current_macs=current_macs,
            candidates=candidates,
            async_add_entities=async_add_entities,
        )

    _check_connect()
    entry.async_on_unload(coordinator.async_add_listener(_check_connect))

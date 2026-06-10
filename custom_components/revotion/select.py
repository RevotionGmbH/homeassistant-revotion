"""Select platform for the Revotion integration.

Hosts Connect-device select entities (Phase 3: Truma Combi energy source and
water power limit, device 512). Generic registry dispatch + deferred discovery,
identical in shape to climate.py/number.py.

The select maps user-facing option strings to/from the raw ``dev_data`` value
via the spec's two lookup tables; setting publishes the mapped value at the
spec's (possibly nested) write_path with optimistic state.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import get_descriptor, has_descriptor, read_dev_data_path, resolve_connect_device
from .connect.control import ConnectCommandMixin, set_dev_data_path
from .connect.descriptors import SelectSpec
from .connect.entity import resolve_entity_category
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class RevotionConnectSelect(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    SelectEntity,
):
    """Select entity for a discrete Connect setting (e.g. Truma energy source)."""

    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: SelectSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the select entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_option: str | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_options = list(spec.options)
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def current_option(self) -> str | None:
        """Return the mapped current option (optimistic value wins until echo)."""
        if self._optimistic_option is not None:
            return self._optimistic_option
        cap = self._find_capability()
        if cap is None:
            return None
        value = read_dev_data_path(cap, self._spec.path)
        return self._spec.value_to_option.get(value) if isinstance(value, int) else None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption on command timeout."""
        self._optimistic_option = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once the mapped dev_data option matches the optimistic one."""
        if self._optimistic_option is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        value = read_dev_data_path(cap, self._spec.path)
        real = self._spec.value_to_option.get(value) if isinstance(value, int) else None
        return real == self._optimistic_option

    async def async_select_option(self, option: str) -> None:
        """Set the option by publishing its mapped raw value at write_path."""
        if option not in self._spec.option_to_value:
            return
        dev_data: dict[str, Any] = {}
        set_dev_data_path(dev_data, self._spec.write_path, self._spec.option_to_value[option])
        for path, extra in self._spec.extra_command_fields.items():
            set_dev_data_path(dev_data, path, extra)
        await self._publish_connect_command(dev_data)
        self._optimistic_option = option
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect select entities via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known: set[tuple[str, int, str]] = set()

    def _check_connect() -> None:
        """Create select entities for Connect devices whose descriptor defines them."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known if key[0] not in current_macs}
        known.difference_update(stale)

        entities: list[RevotionConnectSelect] = []
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
                for spec in descriptor.selects:
                    key = (node_mac, capability.capability_index, spec.key)
                    if key in known:
                        continue
                    known.add(key)
                    register_node_device(hass, entry, node, brain_mac)
                    entities.append(
                        RevotionConnectSelect(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            device_code=device_code,
                            spec=spec,
                            mqtt_client=mqtt_client,
                            config_name=capability.config.name,
                        )
                    )

        if entities:
            async_add_entities(entities)

    _check_connect()
    entry.async_on_unload(coordinator.async_add_listener(_check_connect))

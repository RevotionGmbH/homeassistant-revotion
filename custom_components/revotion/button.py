"""Button platform for the Revotion integration.

Hosts Connect-device one-shot command buttons (Phase 1: the Thitronik WiPro III
panic alarm, device 1024 / command 115 -- the app's PanikWidget tile). Generic
registry dispatch, mirroring lock.py: any Connect device whose descriptor
carries :class:`~.connect.descriptors.ButtonSpec` entries produces one
:class:`RevotionConnectButton` per spec.

Pressing publishes a ``ctr_data`` command via :class:`ConnectCommandMixin`
under the usual single-command lock. Buttons are stateless, so there is no
optimistic value to revert; the lock is held until the spec's ``confirm_path``
flag confirms the action (Thitronik: ``alarm`` flips to 1 once the siren
fires), the next coordinator update (no ``confirm_path``), or the 60 s timeout.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import get_descriptor, has_descriptor, int01_to_bool, read_dev_data_path, resolve_connect_device
from .connect.control import ConnectCommandMixin, connect_command_dev_data
from .connect.descriptors import ButtonSpec
from .connect.entity import resolve_entity_category
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class RevotionConnectButton(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    ButtonEntity,
):
    """One-shot command button for a Connect device (e.g. Thitronik panic).

    Pressing publishes the spec's command code on ``ctr_data``. The shared
    command lock blocks a second press until the device confirms (the
    ``confirm_path`` flag) or the timeout reverts -- a double-press over the
    slow link would otherwise queue redundant commands.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: ButtonSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the button entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_entity_category = resolve_entity_category(spec.entity_category)
        if spec.icon is not None:
            self._attr_icon = spec.icon

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    def _handle_coordinator_update(self) -> None:
        """Release the command lock once real data confirms the press."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _optimistic_confirmed(self) -> bool:
        """Return True once the press is confirmed (releases the command lock).

        A button has no optimistic state, only the lock: with a ``confirm_path``
        the lock holds until that 0/1 flag reads truthy (Thitronik panic: the
        ``alarm`` flag); without one, any coordinator update releases it.
        """
        if not self._command_pending:
            return True
        if self._spec.confirm_path is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        return bool(int01_to_bool(read_dev_data_path(cap, self._spec.confirm_path)))

    async def async_press(self) -> None:
        """Publish the spec's command code."""
        await self._publish_connect_command(connect_command_dev_data(self._spec.command))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect buttons via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known: set[tuple[str, int, str]] = set()

    def _check_connect() -> None:
        """Create buttons for Connect devices whose descriptor defines them."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known if key[0] not in current_macs}
        known.difference_update(stale)

        entities: list[RevotionConnectButton] = []
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
                for spec in descriptor.buttons:
                    key = (node_mac, capability.capability_index, spec.key)
                    if key in known:
                        continue
                    known.add(key)
                    register_node_device(hass, entry, node, brain_mac)
                    entities.append(
                        RevotionConnectButton(
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

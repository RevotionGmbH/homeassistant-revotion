"""Binary sensor platform for the Revotion integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    control_lock_reason,
    get_descriptor,
    has_descriptor,
    int01_to_bool,
    read_dev_data_path,
    reconcile_gated_entities,
    resolve_connect_device,
)
from .connect.descriptors import ArrayBinarySensorSpec, BinarySensorSpec, ControlLockSpec
from .connect.entity import resolve_binary_sensor_device_class, resolve_entity_category
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, find_node, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

MQTT_CONNECTION_DESCRIPTION = BinarySensorEntityDescription(
    key="mqtt_connection",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    has_entity_name=True,
    translation_key="mqtt_connection",
)

BRAIN_ONLINE_DESCRIPTION = BinarySensorEntityDescription(
    key="brain_online",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    has_entity_name=True,
    translation_key="brain_online",
)

SWITCH_FUSE_DESCRIPTION = BinarySensorEntityDescription(
    key="switch_fuse",
    device_class=BinarySensorDeviceClass.PROBLEM,
    has_entity_name=True,
    translation_key="switch_fuse",
)

NODE_CONNECTIVITY_DESCRIPTION = BinarySensorEntityDescription(
    key="node_connectivity",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    has_entity_name=True,
    translation_key="node_connectivity",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion binary sensor entities."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known_node_macs: set[str] = set()

    # Brain-level entities (always created)
    async_add_entities(
        [
            RevotionMqttConnectionBinarySensor(
                coordinator=coordinator,
                description=MQTT_CONNECTION_DESCRIPTION,
                brain_mac=brain_mac,
                mqtt_client=mqtt_client,
            ),
            RevotionBrainOnlineBinarySensor(
                coordinator=coordinator,
                description=BRAIN_ONLINE_DESCRIPTION,
                brain_mac=brain_mac,
            ),
        ]
    )

    # Node-level connectivity + fuse entities (dynamic discovery)
    def _check_device() -> None:
        """Check for new nodes and add connectivity + fuse binary sensors."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        new_macs = current_macs - known_node_macs
        if not new_macs:
            return
        known_node_macs.update(new_macs)

        entities: list[BinarySensorEntity] = []
        for node in coordinator.data.nodes:
            if normalize_mac(node.mac_address) not in new_macs:
                continue
            register_node_device(hass, entry, node, brain_mac)

            entities.append(
                RevotionNodeConnectivityBinarySensor(
                    coordinator=coordinator,
                    brain_mac=brain_mac,
                    node_mac=node.mac_address,
                )
            )

            for capability in node.capabilities:
                if capability.capability_type in (CapabilityType.SWITCH, CapabilityType.TWO_WAY):
                    entities.append(
                        RevotionSwitchFuseBinarySensor(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            config_name=capability.config.name,
                        )
                    )

        if entities:
            async_add_entities(entities)

    # Connect (Type 12) deferred discovery: only devices with a tailored
    # descriptor produce binary_sensors here (generic devices mirror everything
    # through sensor.py). Track at (node_mac, cap_index, key) granularity so
    # entities appearing in later messages are picked up exactly once and a
    # re-paired node recreates its entities. Fixed sensors (spec.key) stay
    # additive; array elements ("{key_prefix}_{n}") are *reconciled* against the
    # array length so sensors deleted in the app disappear here too.
    known_connect_keys: set[tuple[str, int, str]] = set()
    brain_norm = normalize_mac(brain_mac)
    array_entities: dict[tuple[str, int, str], BinarySensorEntity] = {}

    def _make_array_binary_sensor(node, capability, array_spec, index):
        """Bind a per-element factory (own scope avoids late-binding in the loop)."""

        def factory() -> RevotionConnectArrayBinarySensor:
            register_node_device(hass, entry, node, brain_mac)
            return RevotionConnectArrayBinarySensor(
                coordinator=coordinator,
                brain_mac=brain_mac,
                node_mac=node.mac_address,
                cap_index=capability.capability_index,
                spec=array_spec,
                index=index,
                config_name=capability.config.name,
            )

        return factory

    def _check_connect() -> None:
        """Create descriptor-driven binary sensors once a device code resolves.

        Fixed-path BinarySensorSpecs map one dev_data leaf each (additive).
        ArrayBinarySensorSpecs expand to one entity per array element and are
        reconciled against the array length: the Thitronik ``stat[]``/``bat[]``
        arrays always carry exactly the currently paired sensors, so a shrink
        means sensors were deleted in the app -- their entities are removed
        (live + registry row) instead of lingering as "unavailable" ghosts.
        Indices are swept up to the spec's firmware bound so ghost rows from an
        earlier session are also cleaned after a restart. Every entity owns a
        fixed key, so there is no value-dependent routing and no unique_id
        collision (Phase 0 review blocker B1).
        """
        if coordinator.data is None:
            return

        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known_connect_keys if key[0] not in current_macs}
        known_connect_keys.difference_update(stale)

        entities: list[BinarySensorEntity] = []
        array_candidates = []
        for node in coordinator.data.nodes:
            node_mac = normalize_mac(node.mac_address)
            for capability in node.capabilities:
                if capability.capability_type != CapabilityType.CONNECT:
                    continue
                device_code = resolve_connect_device(capability)
                if device_code is None or not has_descriptor(device_code):
                    continue  # not resolved yet, or generic -> handled by sensor.py
                descriptor = get_descriptor(device_code)
                assert descriptor is not None  # has_descriptor guarantees it

                for spec in descriptor.binary_sensors:
                    key = (node_mac, capability.capability_index, spec.key)
                    if key in known_connect_keys:
                        continue
                    known_connect_keys.add(key)
                    entities.append(
                        RevotionConnectBinarySensor(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            spec=spec,
                            config_name=capability.config.name,
                        )
                    )

                for array_spec in descriptor.array_binary_sensors:
                    # Only an array actually present in dev_data is authoritative
                    # (the firmware always serializes the full array, so its
                    # length == paired sensors). Absent -- e.g. no /data message
                    # yet after a restart -- means "no information": skip, so
                    # the reconcile never wipes registry rows (and the user's
                    # renames/areas) on a data-less startup window.
                    elements = read_dev_data_path(capability, array_spec.array_key)
                    if not isinstance(elements, list):
                        continue
                    # Sweep the full firmware range, not just len(elements):
                    # indices beyond the current length yield present=False so
                    # their entities/ghost rows are removed by the reconcile.
                    for index in range(array_spec.max_elements):
                        number = index + 1
                        key = (node_mac, capability.capability_index, f"{array_spec.key_prefix}_{number}")
                        unique_id = (
                            f"revotion_{brain_norm}_{node_mac}_{capability.capability_index}"
                            f"_{array_spec.key_prefix}_{number}"
                        )
                        array_candidates.append(
                            (
                                key,
                                index < len(elements),
                                unique_id,
                                _make_array_binary_sensor(node, capability, array_spec, index),
                            )
                        )

                for lock_spec in descriptor.control_locks:
                    key = (node_mac, capability.capability_index, lock_spec.key)
                    if key in known_connect_keys:
                        continue
                    known_connect_keys.add(key)
                    entities.append(
                        RevotionConnectControlLockBinarySensor(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            spec=lock_spec,
                            config_name=capability.config.name,
                        )
                    )

        if entities:
            async_add_entities(entities)

        reconcile_gated_entities(
            hass=hass,
            entity_domain="binary_sensor",
            entities=array_entities,
            current_macs=current_macs,
            candidates=array_candidates,
            async_add_entities=async_add_entities,
        )

    def _on_update() -> None:
        """Coordinator listener: run both standard and Connect discovery."""
        _check_device()
        _check_connect()

    _on_update()
    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class RevotionMqttConnectionBinarySensor(CoordinatorEntity[RevotionCoordinator], BinarySensorEntity):
    """Binary sensor for MQTT connection status."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        description: BinarySensorEntityDescription,
        brain_mac: str,
        mqtt_client: RevotionMqttClient,
    ) -> None:
        """Initialize the MQTT connection binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._mqtt_client = mqtt_client
        normalized_mac = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{normalized_mac}_mqtt_connection"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, normalized_mac)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if MQTT is connected."""
        return self._mqtt_client.is_connected


class RevotionBrainOnlineBinarySensor(CoordinatorEntity[RevotionCoordinator], BinarySensorEntity):
    """Binary sensor for Brain online/offline status (BSEN-01)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        description: BinarySensorEntityDescription,
        brain_mac: str,
    ) -> None:
        """Initialize the Brain online binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        normalized_mac = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{normalized_mac}_online"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, normalized_mac)},
        }

    @property
    def is_on(self) -> bool:
        """Return True if Brain is online."""
        return self.coordinator.data.is_online


class RevotionNodeConnectivityBinarySensor(CoordinatorEntity[RevotionCoordinator], BinarySensorEntity):
    """Binary sensor for the node's ESP-NOW link to the Brain.

    Mirrors the "not connected" indicator in the Revotion app (firmware
    user-error 4101). All other entities of an unreachable node go
    *unavailable* via ``RevotionCapabilityMixin._node_reachable``; this sensor
    deliberately stays available and reads ``off`` instead, so the outage has
    history and can drive automations. It only goes unavailable when the node
    leaves the inventory entirely (unpaired).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
    ) -> None:
        """Initialize the node connectivity binary sensor."""
        super().__init__(coordinator)
        self.entity_description = NODE_CONNECTIVITY_DESCRIPTION
        self._node_mac = normalize_mac(node_mac)
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_connectivity"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}

    @property
    def available(self) -> bool:
        """Return True while the node is in the inventory (even if unreachable)."""
        return super().available and find_node(self.coordinator.data, self._node_mac) is not None

    @property
    def is_on(self) -> bool | None:
        """Return True if the Brain can reach the node over ESP-NOW."""
        node = find_node(self.coordinator.data, self._node_mac)
        if node is None:
            return None
        return node.reachable


class RevotionSwitchFuseBinarySensor(
    RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], BinarySensorEntity
):
    """Binary sensor for Switch fuse status (blown/ok)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        config_name: str = "",
    ) -> None:
        """Initialize the fuse binary sensor."""
        super().__init__(coordinator)
        self.entity_description = SWITCH_FUSE_DESCRIPTION
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_fuse"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        if config_name:
            self._attr_name = f"{config_name} Fuse"

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def is_on(self) -> bool | None:
        """Return True if fuse is blown (fuse == 1)."""
        cap = self._find_capability()
        if cap is None:
            return None
        fuse = cap.data.get("fuse")
        if fuse is None:
            return None
        return bool(fuse)


class RevotionConnectBinarySensor(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], BinarySensorEntity):
    """Descriptor-driven binary sensor for one 0/1 Connect dev_data flag.

    Created from a :class:`BinarySensorSpec` (fixed path) for a device with a
    tailored descriptor. The wire value is a 0/1 integer (bool serialized as a
    number); decoded via ``int01_to_bool`` so a real 0 reads as off, not unknown.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        spec: BinarySensorSpec,
        config_name: str = "",
    ) -> None:
        """Initialize a descriptor-driven Connect binary sensor."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._spec = spec
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_device_class = resolve_binary_sensor_device_class(spec.device_class)
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def is_on(self) -> bool | None:
        """Return the decoded 0/1 flag at this spec's dev_data path."""
        cap = self._find_capability()
        if cap is None:
            return None
        return int01_to_bool(read_dev_data_path(cap, self._spec.path))


class RevotionConnectArrayBinarySensor(
    RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], BinarySensorEntity
):
    """Descriptor-driven binary sensor for one element of a 0/1 Connect array.

    Created from an :class:`ArrayBinarySensorSpec` -- one entity per element of
    a ``dev_data`` array (Thitronik ``stat[]`` magnet contacts, ``bat[]`` sensor
    batteries). The element is read by index; if a later message shrinks the
    array below this index the discovery reconcile removes the entity (the
    sensor was deleted in the app).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        spec: ArrayBinarySensorSpec,
        index: int,
        config_name: str = "",
    ) -> None:
        """Initialize one array-element Connect binary sensor (0-based index)."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._spec = spec
        self._index = index
        number = index + 1  # 1-based for user-facing names/keys
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = (
            f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key_prefix}_{number}"
        )
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        element_name = f"{spec.name_prefix} {number}"
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = element_name
        self._attr_device_class = resolve_binary_sensor_device_class(spec.device_class)
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def is_on(self) -> bool | None:
        """Return the decoded 0/1 flag at this array element."""
        cap = self._find_capability()
        if cap is None:
            return None
        return int01_to_bool(read_dev_data_path(cap, f"{self._spec.array_key}.{self._index}"))


class RevotionConnectControlLockBinarySensor(
    RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], BinarySensorEntity
):
    """Diagnostic binary sensor: is remote control locked by the device panel?

    Created from a :class:`ControlLockSpec` for the lockable devices (Alde,
    Truma, Eberspächer Airtronic3, Thitronik). ``on`` == locked, i.e. the
    firmware's ``dev_stat`` reads 6 (main panel in use) or 7 (main panel +
    error). The ``reason`` attribute distinguishes the two. No ``device_class``
    is set: ``BinarySensorDeviceClass.LOCK`` has inverted semantics (on ==
    unlocked) and ``6`` is normal panel use rather than a "problem".
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        spec: ControlLockSpec,
        config_name: str = "",
    ) -> None:
        """Initialize a Connect control-lock binary sensor."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._spec = spec
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
    def is_on(self) -> bool | None:
        """Return True if control is locked (dev_stat 6/7), else False/None."""
        cap = self._find_capability()
        if cap is None:
            return None
        return control_lock_reason(cap, self._spec.lock_path) is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the lock reason (``main_panel`` / ``main_panel_error``)."""
        cap = self._find_capability()
        reason = control_lock_reason(cap, self._spec.lock_path) if cap is not None else None
        return {"reason": reason}

"""Sensor platform for the Revotion integration.

Implements all read-only sensor entities: Temperature (Type 3), Battery
sub-entities (Type 5), Level (Type 6), and HighCurrent sub-entities (Type 8).
Battery and HighCurrent capabilities produce multiple sub-entities each,
with current channels created dynamically from MQTT data array length.
Also provides Brain-level diagnostic sensors: connection type and last connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    DEV_STAT_KEY,
    connect_device_label,
    flatten_connect_capability,
    get_descriptor,
    has_descriptor,
    humanize_path,
    read_dev_data_array,
    read_dev_data_path,
    resolve_connect_device,
)
from .connect.descriptors import ArraySensorSpec, SensorSpec
from .connect.entity import resolve_entity_category, resolve_sensor_device_class
from .const import (
    CONF_BRAIN_MAC,
    CONNECTION_INTERFACE_LABELS,
    CONNECTION_INTERFACE_OPTIONS,
    DOMAIN,
    CapabilityType,
    RevotionConfigEntry,
)
from .coordinator import RevotionCoordinator
from .models import Capability, RevotionCapabilityMixin, normalize_mac, register_node_device

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class RevotionSensorEntityDescription(SensorEntityDescription):
    """Sensor entity description with Revotion data key."""

    data_key: str = ""
    data_index: int | None = None  # For array indexing (cur channels)


# --- Temperature (Type 3) ---

TEMPERATURE_DESCRIPTION = RevotionSensorEntityDescription(
    key="temperature",
    device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT,
    translation_key="temperature",
    data_key="val",
)

# --- Level (Type 6) ---

LEVEL_DESCRIPTION = RevotionSensorEntityDescription(
    key="level",
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
    translation_key="level",
    data_key="val",
)

# --- Battery (Type 5) static sub-entity descriptions ---
# NOTE: CHARGING_STAGE intentionally excluded per D-07 override / Pitfall 7
# (no MQTT data key exists for charging_stage)

BATTERY_DESCRIPTIONS: tuple[RevotionSensorEntityDescription, ...] = (
    RevotionSensorEntityDescription(
        key="battery_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="battery_soc",
        data_key="soc",
    ),
    RevotionSensorEntityDescription(
        key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="battery_voltage",
        data_key="volt",
    ),
    RevotionSensorEntityDescription(
        key="battery_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="battery_temperature",
        data_key="temp",
    ),
    RevotionSensorEntityDescription(
        key="battery_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        translation_key="battery_time_remaining",
        data_key="tr",
    ),
    RevotionSensorEntityDescription(
        key="battery_time_to_full",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        translation_key="battery_time_to_full",
        data_key="tf",
    ),
    RevotionSensorEntityDescription(
        key="battery_charge_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        translation_key="battery_charge_cycles",
        data_key="ch_cycle",
    ),
    RevotionSensorEntityDescription(
        key="battery_charge_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL_INCREASING,
        translation_key="battery_charge_total",
        data_key="ch_total",
    ),
    RevotionSensorEntityDescription(
        key="battery_secondary_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        translation_key="battery_secondary_voltage",
        data_key="sv",
    ),
)

# --- Switch (Type 2) sensor sub-entity descriptions ---
# Voltage, Current, Power as separate sensors under the Switch node device

SWITCH_SENSOR_DESCRIPTIONS: tuple[RevotionSensorEntityDescription, ...] = (
    RevotionSensorEntityDescription(
        key="switch_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="switch_voltage",
        data_key="volt",
    ),
    RevotionSensorEntityDescription(
        key="switch_current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement="A",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="switch_current",
        data_key="cur",
    ),
)


class RevotionSwitchPowerSensor(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Computed power sensor for Switch capabilities (volt * cur)."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        config_name: str = "",
    ) -> None:
        """Initialize the power sensor."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_power"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        if config_name:
            self._attr_name = f"{config_name} Power"
        else:
            self._attr_translation_key = "switch_power"

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return self._node_reachable() and self._find_capability() is not None

    @property
    def native_value(self) -> float | None:
        """Return power = voltage * current."""
        cap = self._find_capability()
        if cap is None:
            return None
        volt = cap.data.get("volt")
        cur = cap.data.get("cur")
        if volt is None or cur is None:
            return None
        return round(volt * cur, 1)


# --- HighCurrent (Type 8) static sub-entity descriptions ---
# Same as Battery but WITHOUT temperature and secondary_voltage,
# and using highcurrent_ prefix for translation_key

HIGHCURRENT_DESCRIPTIONS: tuple[RevotionSensorEntityDescription, ...] = (
    RevotionSensorEntityDescription(
        key="highcurrent_soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="highcurrent_soc",
        data_key="soc",
    ),
    RevotionSensorEntityDescription(
        key="highcurrent_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement="V",
        state_class=SensorStateClass.MEASUREMENT,
        translation_key="highcurrent_voltage",
        data_key="volt",
    ),
    RevotionSensorEntityDescription(
        key="highcurrent_time_remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        translation_key="highcurrent_time_remaining",
        data_key="tr",
    ),
    RevotionSensorEntityDescription(
        key="highcurrent_time_to_full",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement="min",
        translation_key="highcurrent_time_to_full",
        data_key="tf",
    ),
    RevotionSensorEntityDescription(
        key="highcurrent_charge_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        translation_key="highcurrent_charge_cycles",
        data_key="ch_cycle",
    ),
    RevotionSensorEntityDescription(
        key="highcurrent_charge_total",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.TOTAL_INCREASING,
        translation_key="highcurrent_charge_total",
        data_key="ch_total",
    ),
)


def _create_current_channel_descriptions(
    capability: Capability,
    prefix: str,
) -> list[RevotionSensorEntityDescription]:
    """Create dynamic current channel descriptions from capability data.

    Args:
        capability: The capability with data containing 'cur' list.
        prefix: Either 'battery' or 'highcurrent' for key/translation_key prefix.

    Returns:
        List of descriptions for each current channel (D-08).

    """
    cur_data = capability.data.get("cur", [])
    if not isinstance(cur_data, list):
        return []

    descriptions: list[RevotionSensorEntityDescription] = []
    for i in range(len(cur_data)):
        ch_num = i + 1  # 1-based channel numbering
        descriptions.append(
            RevotionSensorEntityDescription(
                key=f"{prefix}_current_ch{ch_num}",
                device_class=SensorDeviceClass.CURRENT,
                native_unit_of_measurement="A",
                state_class=SensorStateClass.MEASUREMENT,
                translation_key=f"{prefix}_current_ch{ch_num}",
                data_key="cur",
                data_index=i,
            )
        )
    return descriptions


class RevotionSensorEntity(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Base sensor entity for Revotion capabilities.

    Reads state from coordinator.data by locating the matching node/capability
    and extracting the value from capability.data using the description's data_key.
    """

    _attr_has_entity_name = True
    entity_description: RevotionSensorEntityDescription

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        description: RevotionSensorEntityDescription,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        sub_key: str | None = None,
        config_name: str = "",
    ) -> None:
        """Initialize the sensor entity.

        Args:
            coordinator: The data coordinator.
            description: Entity description with device_class, unit, data_key.
            brain_mac: Normalized Brain MAC address.
            node_mac: Normalized Node MAC address.
            cap_index: Capability index on the node.
            sub_key: Optional sub-entity key for unique ID suffix.
            config_name: User-defined capability name from the app.

        """
        super().__init__(coordinator)
        self.entity_description = description
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._brain_mac = normalize_mac(brain_mac)

        # Use user-defined config name from app if available
        if config_name and sub_key:
            # Battery/HighCurrent sub-entity: "System SOC", "Motorbatterie 1 Voltage"
            sub_display = description.translation_key or ""
            # Strip prefix to get display suffix: "battery_soc" -> "SOC"
            for prefix in ("battery_", "highcurrent_"):
                if sub_display.startswith(prefix):
                    sub_display = sub_display.removeprefix(prefix)
                    break
            sub_display = sub_display.replace("_", " ").title()
            self._attr_name = f"{config_name} {sub_display}"
        elif config_name:
            # Simple entity (Temperature, Level): use config name directly
            self._attr_name = config_name

        # Build unique ID: revotion_{brain}_{node}_{cap_index}[_{sub_key}]
        suffix = f"_{sub_key}" if sub_key else ""
        self._attr_unique_id = f"revotion_{self._brain_mac}_{self._node_mac}_{cap_index}{suffix}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._node_mac)},
        }

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return self._node_reachable() and self._find_capability() is not None

    @property
    def native_value(self) -> float | int | None:
        """Return the sensor value from capability data."""
        capability = self._find_capability()
        if capability is None:
            return None

        desc = self.entity_description
        value = capability.data.get(desc.data_key)
        if value is None:
            return None

        # Handle array indexing for current channels
        if desc.data_index is not None:
            if isinstance(value, list) and desc.data_index < len(value):
                return value[desc.data_index]
            return None

        return value


# --- Connect (Type 12) generic read-only mirror ---


class RevotionConnectSensor(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Generic read-only sensor for one flattened Connect dev_data leaf.

    Phase 0 mirror: until a device-specific descriptor exists (see
    connect/registry.py), *every* dev_data leaf becomes one of these -- including
    0/1 boolean flags, which are shown as their raw numeric value. Mirroring
    everything through the single sensor platform guarantees each flat path maps
    to exactly one entity, so a value flipping between boolean-looking and
    numeric can never split into two entities sharing a unique_id (B1).

    The value is looked up by its flat path (e.g. "comb_water.target_temp")
    on each read, so later /data messages update it in place. ``dev_stat`` is
    surfaced as a diagnostic sensor (concept §6).

    No device_class/unit is assigned: the raw firmware value is shown as-is,
    since semantics differ per device and are resolved in later phases.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        flat_path: str,
        device_code: int | None,
    ) -> None:
        """Initialize the generic Connect sensor.

        Args:
            coordinator: The data coordinator.
            brain_mac: Brain MAC address (any format).
            node_mac: Node MAC address (any format).
            cap_index: Capability index on the node.
            flat_path: Dotted dev_data leaf path this entity reflects.
            device_code: Resolved Connect device code, for the name prefix.

        """
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._brain_mac = normalize_mac(brain_mac)
        self._flat_path = flat_path
        self._attr_unique_id = f"revotion_{self._brain_mac}_{self._node_mac}_{cap_index}_{flat_path}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        self._attr_name = f"{connect_device_label(device_code)} {humanize_path(flat_path)}"
        # dev_stat is firmware status/diagnostic information.
        if flat_path == DEV_STAT_KEY:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return self._node_reachable() and self._find_capability() is not None

    @property
    def native_value(self) -> float | int | str | None:
        """Return the current value at this dev_data leaf path."""
        cap = self._find_capability()
        if cap is None:
            return None
        return flatten_connect_capability(cap).get(self._flat_path)


class RevotionConnectDescriptorSensor(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Descriptor-driven read-only sensor for one named Connect dev_data field.

    Unlike the generic mirror, this is created from a :class:`SensorSpec` for a
    device that has a tailored descriptor (see connect/registry.py): it reads a
    fixed ``dev_data`` path and carries the spec's name, device_class, unit and
    entity_category. The fixed path means no value-dependent routing, so no
    unique_id collision (Phase 0 review blocker B1).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        spec: SensorSpec,
        config_name: str = "",
    ) -> None:
        """Initialize a descriptor-driven Connect sensor."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._brain_mac = normalize_mac(brain_mac)
        self._spec = spec
        self._attr_unique_id = f"revotion_{self._brain_mac}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_device_class = resolve_sensor_device_class(spec.device_class)
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        self._attr_entity_category = resolve_entity_category(spec.entity_category)

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def native_value(self) -> float | int | str | None:
        """Return the current value at this spec's dev_data path."""
        cap = self._find_capability()
        if cap is None:
            return None
        return read_dev_data_path(cap, self._spec.path)


class RevotionConnectArraySensor(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Descriptor-driven sensor for one element of a numeric Connect array.

    Created from an :class:`ArraySensorSpec` -- one entity per element of a
    ``dev_data`` array (EcoFlow ``pwr[5]`` per-channel power). The element is
    read by index; if a later message shrinks the array below this index the
    value reads as None.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        spec: ArraySensorSpec,
        index: int,
        config_name: str = "",
    ) -> None:
        """Initialize one array-element Connect sensor (0-based index)."""
        super().__init__(coordinator)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._brain_mac = normalize_mac(brain_mac)
        self._spec = spec
        self._index = index
        number = index + 1  # 1-based for user-facing names/keys
        self._attr_unique_id = f"revotion_{self._brain_mac}_{self._node_mac}_{cap_index}_{spec.key_prefix}_{number}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        element_name = f"{spec.name_prefix} {number}"
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = element_name
        self._attr_device_class = resolve_sensor_device_class(spec.device_class)
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        self._attr_entity_category = resolve_entity_category(spec.entity_category)
        if spec.device_class is not None or spec.unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def native_value(self) -> float | int | None:
        """Return the value at this array element."""
        cap = self._find_capability()
        if cap is None:
            return None
        return read_dev_data_path(cap, f"{self._spec.array_key}.{self._index}")


# --- Brain-level diagnostic sensor descriptions ---
# These use SensorEntityDescription (like the capability sensors) so that every
# entity added by this platform exposes a consistent `entity_description`.

CONNECTION_TYPE_DESCRIPTION = SensorEntityDescription(
    key="connection_type",
    device_class=SensorDeviceClass.ENUM,
    options=CONNECTION_INTERFACE_OPTIONS,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="connection_type",
)

LAST_CONNECTION_DESCRIPTION = SensorEntityDescription(
    key="last_connection",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="last_connection",
)


class RevotionConnectionTypeSensor(CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Brain-level diagnostic sensor reporting the active connection interface (ENUM)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
    ) -> None:
        """Initialize the connection type sensor."""
        super().__init__(coordinator)
        self.entity_description = CONNECTION_TYPE_DESCRIPTION
        normalized_mac = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{normalized_mac}_connection_type"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, normalized_mac)},
        }

    @property
    def native_value(self) -> str | None:
        """Return the connection interface label, or None if unavailable."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.connection_interface
        if value is None:
            return None
        return CONNECTION_INTERFACE_LABELS.get(value)


class RevotionLastConnectionSensor(CoordinatorEntity[RevotionCoordinator], SensorEntity):
    """Brain-level diagnostic sensor reporting the last connection timestamp."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
    ) -> None:
        """Initialize the last connection timestamp sensor."""
        super().__init__(coordinator)
        self.entity_description = LAST_CONNECTION_DESCRIPTION
        normalized_mac = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{normalized_mac}_last_connection"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, normalized_mac)},
        }

    @property
    def native_value(self) -> datetime | None:
        """Return the last connection time as a UTC-aware datetime, or None."""
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.last_connection
        if not value:
            return None
        return datetime.fromtimestamp(value, tz=UTC)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion sensor entities with dynamic discovery support.

    Uses the _check_device listener pattern to dynamically create entities
    when new nodes are paired at runtime (D-05, D-06).
    """
    coordinator = entry.runtime_data.coordinator
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known_node_macs: set[str] = set()

    # Brain-level diagnostic sensors (always created once at setup)
    async_add_entities(
        [
            RevotionConnectionTypeSensor(coordinator=coordinator, brain_mac=brain_mac),
            RevotionLastConnectionSensor(coordinator=coordinator, brain_mac=brain_mac),
        ]
    )

    def _check_device() -> None:
        """Check for new nodes and add sensor entities dynamically."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        new_macs = current_macs - known_node_macs
        if not new_macs:
            return
        known_node_macs.update(new_macs)

        entities: list[RevotionSensorEntity] = []
        for node in coordinator.data.nodes:
            if normalize_mac(node.mac_address) not in new_macs:
                continue
            register_node_device(hass, entry, node, brain_mac)

            for capability in node.capabilities:
                match capability.capability_type:
                    case CapabilityType.TEMPERATURE:
                        entities.append(
                            RevotionSensorEntity(
                                coordinator=coordinator,
                                description=TEMPERATURE_DESCRIPTION,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                config_name=capability.config.name,
                            )
                        )

                    case CapabilityType.LEVEL:
                        entities.append(
                            RevotionSensorEntity(
                                coordinator=coordinator,
                                description=LEVEL_DESCRIPTION,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                config_name=capability.config.name,
                            )
                        )

                    case CapabilityType.BATTERY:
                        # Static sub-entities (SOC, Voltage, Temp, etc.)
                        for desc in BATTERY_DESCRIPTIONS:
                            entities.append(
                                RevotionSensorEntity(
                                    coordinator=coordinator,
                                    description=desc,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    sub_key=desc.key.removeprefix("battery_"),
                                    config_name=capability.config.name,
                                )
                            )
                        # Dynamic current channels (D-08)
                        for cur_desc in _create_current_channel_descriptions(capability, "battery"):
                            entities.append(
                                RevotionSensorEntity(
                                    coordinator=coordinator,
                                    description=cur_desc,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    sub_key=cur_desc.key.removeprefix("battery_"),
                                    config_name=capability.config.name,
                                )
                            )

                    case CapabilityType.SWITCH | CapabilityType.TWO_WAY:
                        # Voltage and Current sub-entities
                        for desc in SWITCH_SENSOR_DESCRIPTIONS:
                            entities.append(
                                RevotionSensorEntity(
                                    coordinator=coordinator,
                                    description=desc,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    sub_key=desc.key.removeprefix("switch_"),
                                    config_name=capability.config.name,
                                )
                            )
                        # Computed Power sensor
                        entities.append(
                            RevotionSwitchPowerSensor(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                config_name=capability.config.name,
                            )
                        )

                    case CapabilityType.HIGH_CURRENT:
                        # Static sub-entities (SOC, Voltage, etc. -- no temp, no sv)
                        for desc in HIGHCURRENT_DESCRIPTIONS:
                            entities.append(
                                RevotionSensorEntity(
                                    coordinator=coordinator,
                                    description=desc,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    sub_key=desc.key.removeprefix("highcurrent_"),
                                    config_name=capability.config.name,
                                )
                            )
                        # Dynamic current channels (D-08)
                        for cur_desc in _create_current_channel_descriptions(capability, "highcurrent"):
                            entities.append(
                                RevotionSensorEntity(
                                    coordinator=coordinator,
                                    description=cur_desc,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    sub_key=cur_desc.key.removeprefix("highcurrent_"),
                                    config_name=capability.config.name,
                                )
                            )

        if entities:
            async_add_entities(entities)

    # Connect (Type 12) uses deferred discovery: the device code and dev_data
    # only arrive with the first /data message, after the node is already
    # paired. Track at (node_mac, cap_index, key) granularity so new leaves from
    # later messages are picked up and nothing is added twice. The third element
    # is the descriptor spec.key for tailored devices, or the flat dev_data path
    # for the generic mirror -- both unique within a (node, cap).
    known_connect_paths: set[tuple[str, int, str]] = set()

    def _check_connect() -> None:
        """Create Connect sensors once a device code resolves.

        Devices with a tailored descriptor (connect/registry.py) get the named
        SensorSpecs from it (device status / alarm reason for Thitronik). All
        other devices fall back to the Phase 0 generic mirror, which routes
        *every* dev_data leaf through this platform -- including 0/1 flags (B1)
        -- so each flat path owns exactly one entity and a value flipping
        between boolean-looking and numeric never collides on unique_id.
        """
        if coordinator.data is None:
            return

        # Drop tracked paths for nodes that are gone (e.g. unpaired) so a later
        # re-pair recreates their entities and the set never grows unbounded (S2).
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known_connect_paths if key[0] not in current_macs}
        known_connect_paths.difference_update(stale)

        entities: list[SensorEntity] = []
        for node in coordinator.data.nodes:
            node_mac = normalize_mac(node.mac_address)
            for capability in node.capabilities:
                if capability.capability_type != CapabilityType.CONNECT:
                    continue
                device_code = resolve_connect_device(capability)
                if device_code is None:
                    continue  # device not resolved yet -> defer

                if has_descriptor(device_code):
                    descriptor = get_descriptor(device_code)
                    assert descriptor is not None  # has_descriptor guarantees it
                    for spec in descriptor.sensors:
                        key = (node_mac, capability.capability_index, spec.key)
                        if key in known_connect_paths:
                            continue
                        known_connect_paths.add(key)
                        entities.append(
                            RevotionConnectDescriptorSensor(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                spec=spec,
                                config_name=capability.config.name,
                            )
                        )
                    for array_spec in descriptor.array_sensors:
                        elements = read_dev_data_array(capability, array_spec.array_key)
                        for index in range(len(elements)):
                            element_key = f"{array_spec.key_prefix}_{index + 1}"
                            key = (node_mac, capability.capability_index, element_key)
                            if key in known_connect_paths:
                                continue
                            known_connect_paths.add(key)
                            entities.append(
                                RevotionConnectArraySensor(
                                    coordinator=coordinator,
                                    brain_mac=brain_mac,
                                    node_mac=node.mac_address,
                                    cap_index=capability.capability_index,
                                    spec=array_spec,
                                    index=index,
                                    config_name=capability.config.name,
                                )
                            )
                    continue  # tailored device: skip the generic mirror

                for flat_path in flatten_connect_capability(capability):
                    key = (node_mac, capability.capability_index, flat_path)
                    if key in known_connect_paths:
                        continue
                    known_connect_paths.add(key)
                    entities.append(
                        RevotionConnectSensor(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            flat_path=flat_path,
                            device_code=device_code,
                        )
                    )

        if entities:
            async_add_entities(entities)

    def _on_update() -> None:
        """Coordinator listener: run both standard and Connect discovery."""
        _check_device()
        _check_connect()

    _on_update()  # Initial entity creation
    entry.async_on_unload(coordinator.async_add_listener(_on_update))

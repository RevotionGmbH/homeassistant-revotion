"""Climate platform for the Revotion integration.

Hosts Connect-device climate entities (Phase 2: Eberspächer Airtronic 3,
device 256; Truma/Alde follow as descriptor-only additions). Generic registry
dispatch + deferred discovery, identical in shape to alarm_control_panel.py.

The HVAC state of these heaters is split across two ``dev_data`` fields -- an
on/off ``state`` and an integer ``mode`` -- so the :class:`ClimateSpec` declares
the read/write mapping (see descriptors.py). The firmware control decode reads
``state``, ``mode`` and ``target_temp`` together, so a single set always
publishes all three (concept §2.2) to avoid resetting an unmentioned field.

Optimistic state covers BOTH target_temperature and hvac_mode (LTE-M round-trip
up to 5 s): each setter assumes its new value immediately and the assumption is
cleared on the real MQTT echo via ConnectCommandMixin.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    get_descriptor,
    has_descriptor,
    int01_to_bool,
    is_path_available,
    read_dev_data_path,
    reconcile_gated_entities,
    resolve_connect_device,
)
from .connect.control import ConnectCommandMixin, set_dev_data_path
from .connect.descriptors import ClimateSpec
from .connect.entity import resolve_hvac_mode
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class RevotionConnectClimate(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    ClimateEntity,
):
    """Climate entity for a Connect heater/AC device (e.g. Airtronic 3).

    hvac_mode/target_temperature derive from ``dev_data`` via the ClimateSpec
    mapping; setters publish a full ``ctr_data`` command and assume the new
    value optimistically until the MQTT echo arrives.
    """

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_assumed_state = True
    # Default empty so .fan_modes never raises for non-fan climates; the
    # FAN_MODE feature (and a real list) is only set when the spec has fan modes.
    _attr_fan_modes: list[str] = []  # noqa: RUF012

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: ClimateSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_hvac_mode: HVACMode | None = None
        self._optimistic_target_temp: float | None = None
        self._optimistic_fan_mode: str | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        # Static, fully-resolved hvac modes from the spec. Served verbatim by the
        # hvac_modes property when the spec has no hvac_mode_av_flags; otherwise
        # the property filters this list by the per-mode availability flags.
        self._static_hvac_modes = [resolve_hvac_mode(m) for m in spec.hvac_modes]
        self._attr_min_temp = spec.min_temp
        self._attr_max_temp = spec.max_temp
        if spec.target_temp_step is not None:
            self._attr_target_temperature_step = spec.target_temp_step
        # FAN_MODE is advertised only when the spec defines fan modes. The
        # actual list is served by the fan_modes property (static for FreshJet,
        # dynamically filtered for Truma CP+).
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        )
        if spec.fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        self._attr_supported_features = features

    @property
    def available(self) -> bool:
        """Return True if the capability exists and (if gated) is available.

        ``available_path`` gates optional zones/units (Alde ``z2_av``, Truma
        ``is_con``). Such entities are *presence-gated*: the discovery listener
        removes them outright when the flag is falsy (see connect/discovery.py),
        so this check is mainly a safety net for the brief window before removal.
        A missing flag is treated as available (see is_path_available).
        """
        cap = self._find_capability()
        if not (super().available and self._node_reachable() and cap is not None):
            return False
        return is_path_available(cap, self._spec.available_path)

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the offered HVAC modes, filtered by per-mode availability flags.

        Static for devices without flags (the verbatim spec list -- existing
        devices unchanged). For Truma CP+ a mode listed in
        ``hvac_mode_av_flags`` is dropped when its ``dev_data`` flag reads falsy
        (e.g. ``heat`` without ``air_con.ac_heat_av``); modes with no flag entry
        (incl. OFF/COOL/FAN_ONLY) are always offered. Same shape as the
        ``fan_modes`` property.
        """
        if not self._spec.hvac_mode_av_flags:
            return self._static_hvac_modes
        cap = self._find_capability()
        result: list[HVACMode] = []
        for mode in self._static_hvac_modes:
            av_path = self._spec.hvac_mode_av_flags.get(mode.value)
            if av_path is not None and not (cap is not None and int01_to_bool(read_dev_data_path(cap, av_path))):
                continue
            result.append(mode)
        return result

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current HVAC mode (optimistic value wins until echo).

        ``state`` falsy -> OFF. Otherwise the raw ``mode`` value is mapped via
        the spec, falling back to the first non-off mode if unmapped.
        """
        if self._optimistic_hvac_mode is not None:
            return self._optimistic_hvac_mode
        return self._hvac_mode_from_data()

    def _hvac_mode_from_data(self) -> HVACMode | None:
        """Return the HVAC mode decoded from dev_data (ignoring optimistic)."""
        cap = self._find_capability()
        if cap is None:
            return None
        state = int01_to_bool(read_dev_data_path(cap, self._spec.state_path))
        if state is None:
            return None
        if not state:
            return HVACMode.OFF
        if self._spec.mode_path is not None:
            mode_value = read_dev_data_path(cap, self._spec.mode_path)
            mapped = self._spec.mode_value_to_hvac.get(mode_value) if mode_value is not None else None
            if mapped is not None:
                return resolve_hvac_mode(mapped)
        # On but no/unmapped mode: fall back to the first non-off configured mode.
        for hvac in self._static_hvac_modes:
            if hvac != HVACMode.OFF:
                return hvac
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the measured temperature, or None if the device reports none."""
        if self._spec.current_temp_path is None:
            return None
        cap = self._find_capability()
        if cap is None:
            return None
        return read_dev_data_path(cap, self._spec.current_temp_path)

    @property
    def target_temperature(self) -> float | None:
        """Return the setpoint (optimistic value wins until echo)."""
        if self._optimistic_target_temp is not None:
            return self._optimistic_target_temp
        cap = self._find_capability()
        if cap is None:
            return None
        return read_dev_data_path(cap, self._spec.target_temp_path)

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the offered fan modes, filtered to the current context.

        Static for devices without filters (FreshJet -> the spec list verbatim).
        For Truma CP+ the list is filtered per the spec: a mode whose
        ``fan_mode_av_flags`` flag is falsy is dropped, and a mode listed in
        ``fan_mode_hvac_only`` is dropped unless the current hvac_mode is in its
        allowed set (e.g. ``night`` only while cooling).
        """
        if not self._spec.fan_modes:
            return None
        if not (self._spec.fan_mode_av_flags or self._spec.fan_mode_hvac_only):
            return list(self._spec.fan_modes)
        cap = self._find_capability()
        current_hvac = self.hvac_mode
        result: list[str] = []
        for mode in self._spec.fan_modes:
            av_path = self._spec.fan_mode_av_flags.get(mode)
            if av_path is not None and not (cap is not None and int01_to_bool(read_dev_data_path(cap, av_path))):
                continue
            allowed_hvac = self._spec.fan_mode_hvac_only.get(mode)
            if allowed_hvac is not None and (current_hvac is None or current_hvac.value not in allowed_hvac):
                continue
            result.append(mode)
        return result

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode (optimistic value wins until echo).

        Two shapes: an ``fan_auto`` flag + discrete ``fan_speed`` (FreshJet), or
        a single integer ``fan_value_path`` (Truma CP+ ``air_con.fan_mode``).
        """
        if not self._spec.fan_modes:
            return None
        if self._optimistic_fan_mode is not None:
            return self._optimistic_fan_mode
        return self._fan_mode_from_data()

    def _fan_mode_from_data(self) -> str | None:
        """Return the fan mode decoded from dev_data (ignoring optimistic)."""
        cap = self._find_capability()
        if cap is None:
            return None
        if self._spec.fan_value_path is not None:
            value = read_dev_data_path(cap, self._spec.fan_value_path)
            return self._spec.fan_value_to_mode.get(value) if isinstance(value, int) else None
        if self._spec.fan_auto_path is not None and int01_to_bool(read_dev_data_path(cap, self._spec.fan_auto_path)):
            return self._spec.fan_auto_mode
        if self._spec.fan_speed_path is not None:
            speed = read_dev_data_path(cap, self._spec.fan_speed_path)
            if isinstance(speed, int):
                return self._spec.speed_value_to_fan.get(speed)
        return None

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop all optimistic assumptions on command timeout."""
        self._optimistic_hvac_mode = None
        self._optimistic_target_temp = None
        self._optimistic_fan_mode = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once dev_data matches every active optimistic value."""
        if (
            self._optimistic_hvac_mode is None
            and self._optimistic_target_temp is None
            and self._optimistic_fan_mode is None
        ):
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        if self._optimistic_hvac_mode is not None and self._hvac_mode_from_data() != self._optimistic_hvac_mode:
            return False
        if self._optimistic_target_temp is not None:
            real = read_dev_data_path(cap, self._spec.target_temp_path)
            if not isinstance(real, (int, float)) or abs(float(real) - self._optimistic_target_temp) > 0.05:
                return False
        return not (self._optimistic_fan_mode is not None and self._fan_mode_from_data() != self._optimistic_fan_mode)

    def _current_state_value(self) -> int:
        """Return the current ``state`` 0/1 from dev_data (default 0)."""
        cap = self._find_capability()
        return 1 if cap is not None and int01_to_bool(read_dev_data_path(cap, self._spec.state_path)) else 0

    def _current_mode_value(self) -> int:
        """Return the current raw ``mode`` value from dev_data (default 0)."""
        cap = self._find_capability()
        if cap is None or self._spec.mode_path is None:
            return 0
        value = read_dev_data_path(cap, self._spec.mode_path)
        return value if isinstance(value, int) else 0

    def _current_target_temp(self) -> float | None:
        """Return the current setpoint from dev_data, if present."""
        cap = self._find_capability()
        return read_dev_data_path(cap, self._spec.target_temp_path) if cap is not None else None

    def _build_command(self, *, hvac_mode: HVACMode | None, target_temp: float | None) -> dict[str, Any]:
        """Build the full control ``dev_data`` for a climate command.

        The firmware control decode reads state, mode and target_temp together,
        so all three are always present: whichever the caller did not change is
        filled from current data. When a new HVAC mode is requested, ``state``
        and ``mode`` come from the spec maps; for on-modes the mapped mode value
        is used, for OFF the current mode value is preserved. All paths go
        through set_dev_data_path so nested branches (Truma ``comb_air.*``) are
        written as nested objects and fields of the same branch merge.
        """
        dev_data: dict[str, Any] = {}

        if hvac_mode is not None:
            state_value = self._spec.hvac_to_state.get(hvac_mode.value, self._current_state_value())
            mode_value = self._spec.hvac_to_mode_value.get(hvac_mode.value)
            if mode_value is None:
                mode_value = self._current_mode_value()
        else:
            state_value = self._current_state_value()
            mode_value = self._current_mode_value()
        set_dev_data_path(dev_data, self._spec.state_path, state_value)
        # Only write the mode field for devices that have one (mode_path set).
        if self._spec.mode_path is not None:
            set_dev_data_path(dev_data, self._spec.mode_path, mode_value)

        effective_target = target_temp if target_temp is not None else self._current_target_temp()
        if effective_target is not None:
            set_dev_data_path(dev_data, self._spec.target_temp_path, effective_target)

        for path, value in self._spec.extra_command_fields.items():
            set_dev_data_path(dev_data, path, value)
        return dev_data

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode by publishing state+mode (+ current target).

        Validated against the dynamically filtered ``hvac_modes``: a mode whose
        availability flag is currently falsy (Truma ``heat`` without
        ``ac_heat_av``) is a no-op (debug-logged), mirroring async_set_fan_mode.
        """
        if hvac_mode not in self.hvac_modes:
            _LOGGER.debug("Ignoring unavailable hvac_mode %s for %s", hvac_mode, self.entity_id)
            return
        await self._publish_connect_command(self._build_command(hvac_mode=hvac_mode, target_temp=None))
        self._optimistic_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature by publishing target (+ current state/mode)."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self._publish_connect_command(self._build_command(hvac_mode=None, target_temp=temperature))
        self._optimistic_target_temp = float(temperature)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode, publishing only the fan field(s).

        Direct shape (Truma CP+): write the mapped value to ``fan_value_path``.
        Split shape (FreshJet): "auto" sets fan_auto=1 (speed untouched); any
        other mode sets fan_auto=0 + the mapped fan_speed. Validated against the
        currently offered (dynamically filtered) fan_modes.
        """
        if fan_mode not in (self.fan_modes or ()):
            return
        dev_data: dict[str, Any] = {}
        if self._spec.fan_value_path is not None:
            if fan_mode not in self._spec.fan_mode_to_value:
                return
            set_dev_data_path(dev_data, self._spec.fan_value_path, self._spec.fan_mode_to_value[fan_mode])
        else:
            is_auto = fan_mode == self._spec.fan_auto_mode
            if self._spec.fan_auto_path is not None:
                set_dev_data_path(dev_data, self._spec.fan_auto_path, 1 if is_auto else 0)
            if not is_auto and self._spec.fan_speed_path is not None and fan_mode in self._spec.fan_to_speed_value:
                set_dev_data_path(dev_data, self._spec.fan_speed_path, self._spec.fan_to_speed_value[fan_mode])
        await self._publish_connect_command(dev_data)
        self._optimistic_fan_mode = fan_mode
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect climate entities via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    brain_norm = normalize_mac(brain_mac)
    # key -> live entity. Climate zones/units are presence-gated: each spec's
    # available_path (Alde z2_av, Truma is_con) decides whether the entity
    # should exist *now*, so the listener adds it when the flag turns on and
    # removes it (live + registry, incl. a ghost from a prior session) when it
    # turns off -- no greyed-out leftover.
    entities: dict[tuple[str, int, str], RevotionConnectClimate] = {}

    def _make_climate(node, capability, device_code, spec):
        """Bind a per-spec factory (own scope avoids late-binding in the loop)."""

        def factory() -> RevotionConnectClimate:
            register_node_device(hass, entry, node, brain_mac)
            return RevotionConnectClimate(
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
        """Reconcile presence-gated climate entities for descriptor Connect devices."""
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
                for spec in descriptor.climates:
                    key = (node_mac, capability.capability_index, spec.key)
                    present = is_path_available(capability, spec.available_path)
                    unique_id = f"revotion_{brain_norm}_{node_mac}_{capability.capability_index}_{spec.key}"
                    candidates.append((key, present, unique_id, _make_climate(node, capability, device_code, spec)))

        reconcile_gated_entities(
            hass=hass,
            entity_domain="climate",
            entities=entities,
            current_macs=current_macs,
            candidates=candidates,
            async_add_entities=async_add_entities,
        )

    _check_connect()
    entry.async_on_unload(coordinator.async_add_listener(_check_connect))

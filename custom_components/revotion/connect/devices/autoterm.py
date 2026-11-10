"""Autoterm Air (device 304 = 0x130) descriptor -- diesel air heater.

Wire schema from the Brain firmware (``serialization_encode_autoterm.c`` /
``decode_autoterm_data_json`` in Brain_v2_ESPNOW, documented in
``docs/node_payload_schemas.md``) and the Connect node engine
(``Connect_Node_S3/main/services/appliance/autoterm``):

    dev_stat     uint8  -- status enum; 6 = wall panel took over (control locked
                           until the heater is off), 16 heating, 17 fan only
    state        uint8  -- 0 off, 1 running (command: 1 = start/adopt)
    mode         uint8  -- 0 thermostat, 1 power, 2 ventilation
    target_temp  float2 -- setpoint 0..30 °C (only regulates in thermostat mode)
    power_level  uint8  -- 1..6, ONLY present while mode == 1
    fan_level    uint8  -- 1..10, ONLY present while mode == 2
    dur_min      uint16 -- telemetry only (stored vent minutes), never commanded
    cur_temp / ext_temp (+ ext_temp_av) / heatx_temp / supply_volt
    phase_major / phase_minor -- operating phase (3.35 = fan only)
    panel_active bool   -- an OEM wall panel is present on the bus (NOT a lock)
    cur_err      {err_code, err_class} -- LAST run's failure, self-clears on start
    timer        {timer_state, val, set_UTC} -- platform auto-off, optional

Control (app ``NodeDataAutotermModel.toDataPayload`` parity): the payload is
shaped by state and mode -- stop is just ``{"state": 0}``; a start/change sends
``state`` + ``mode`` + exactly the field that mode uses (``target_temp`` /
``power_level`` / ``fan_level``). The Brain decodes into a zeroed struct, the
node only reads the field of the commanded mode, so the other fields are
irrelevant there. ``command_block_authoritative`` makes the integration send
the builder output verbatim (no fragment re-merge).

HVAC mapping: OFF / HEAT (thermostat or power, told apart by preset) /
FAN_ONLY (ventilation). Thermostat is the default heat preset. Levels are
``number`` entities that only accept a value while the heater runs in the
matching mode (the node ignores it otherwise).

Every read field keeps the key of the Phase 0 generic mirror (``key == path``),
so the read-only entities of existing installs keep their unique_id when the
heater gains this descriptor.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import (
    ClimateSpec,
    ConnectDeviceDescriptor,
    ControlLockSpec,
    NumberSpec,
    SensorSpec,
    read_block_path,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_FAN_ONLY = "fan_only"

MODE_THERMOSTAT = 0
MODE_POWER = 1
MODE_VENTILATION = 2

PRESET_THERMOSTAT = "thermostat"
PRESET_POWER = "power"

TARGET_TEMP_MIN = 0
TARGET_TEMP_MAX = 30
POWER_LEVEL_MIN = 1
POWER_LEVEL_MAX = 6
FAN_LEVEL_MIN = 1
FAN_LEVEL_MAX = 10
# The app's validator falls back to level 3 when the reported frame carries no
# level key (thermostat mode) -- a start into power/vent then sends 3.
DEFAULT_LEVEL = 3
DEFAULT_TARGET_TEMP = 21


def _int_in_range(value: Any, low: int, high: int, default: int) -> int:
    """Return ``value`` as an int clamped to ``low..high`` (``default`` if unusable)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, round(value)))


def _command_block(data: Mapping[str, Any]) -> dict[str, Any]:
    """Control payload in the app's state/mode-keyed shape (toDataPayload parity)."""
    state = read_block_path(data, "state")
    if not state:
        return {"state": 0}
    mode = read_block_path(data, "mode")
    if mode == MODE_POWER:
        level = _int_in_range(
            read_block_path(data, "power_level", None), POWER_LEVEL_MIN, POWER_LEVEL_MAX, DEFAULT_LEVEL
        )
        return {"state": 1, "mode": MODE_POWER, "power_level": level}
    if mode == MODE_VENTILATION:
        level = _int_in_range(read_block_path(data, "fan_level", None), FAN_LEVEL_MIN, FAN_LEVEL_MAX, DEFAULT_LEVEL)
        return {"state": 1, "mode": MODE_VENTILATION, "fan_level": level}
    target = _int_in_range(
        read_block_path(data, "target_temp", None), TARGET_TEMP_MIN, TARGET_TEMP_MAX, DEFAULT_TARGET_TEMP
    )
    return {"state": 1, "mode": MODE_THERMOSTAT, "target_temp": target}


AUTOTERM_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.AUTOTERM_AIR,
    name="Autoterm Air",
    command_block=_command_block,
    command_block_authoritative=True,
    # key == path for every field: identical unique_ids to the generic mirror
    # these entities replace (see module docstring).
    sensors=(
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
        SensorSpec(path="state", key="state", name="State", entity_category="diagnostic"),
        SensorSpec(path="mode", key="mode", name="Mode", entity_category="diagnostic"),
        SensorSpec(
            path="target_temp",
            key="target_temp",
            name="Target temperature",
            device_class="temperature",
            unit="°C",
            entity_category="diagnostic",
        ),
        SensorSpec(
            path="cur_temp",
            key="cur_temp",
            name="Heater temperature",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
        ),
        SensorSpec(
            path="ext_temp",
            key="ext_temp",
            name="External temperature",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
        ),
        SensorSpec(path="ext_temp_av", key="ext_temp_av", name="External sensor", entity_category="diagnostic"),
        SensorSpec(
            path="heatx_temp",
            key="heatx_temp",
            name="Heat exchanger temperature",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
        ),
        SensorSpec(
            path="supply_volt",
            key="supply_volt",
            name="Supply voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(path="power_level", key="power_level", name="Reported power level", entity_category="diagnostic"),
        SensorSpec(path="fan_level", key="fan_level", name="Reported fan level", entity_category="diagnostic"),
        SensorSpec(
            path="dur_min",
            key="dur_min",
            name="Run duration",
            device_class="duration",
            unit="min",
            entity_category="diagnostic",
        ),
        SensorSpec(path="phase_major", key="phase_major", name="Phase", entity_category="diagnostic"),
        SensorSpec(path="phase_minor", key="phase_minor", name="Sub-phase", entity_category="diagnostic"),
        SensorSpec(path="panel_active", key="panel_active", name="Wall panel connected", entity_category="diagnostic"),
        SensorSpec(path="cur_err.err_code", key="cur_err.err_code", name="Last error", entity_category="diagnostic"),
        SensorSpec(
            path="cur_err.err_class", key="cur_err.err_class", name="Last error class", entity_category="diagnostic"
        ),
        # Platform auto-off timer: only kept so a mirrored row survives; new
        # installs get them disabled.
        SensorSpec(
            path="timer.timer_state",
            key="timer.timer_state",
            name="Timer state",
            entity_category="diagnostic",
            enabled_default=False,
        ),
        SensorSpec(
            path="timer.val", key="timer.val", name="Timer value", entity_category="diagnostic", enabled_default=False
        ),
        SensorSpec(
            path="timer.set_UTC",
            key="timer.set_UTC",
            name="Timer set time",
            entity_category="diagnostic",
            enabled_default=False,
        ),
    ),
    climates=(
        ClimateSpec(
            key="climate",
            name="Heater",
            hvac_modes=(HVAC_OFF, HVAC_HEAT, HVAC_FAN_ONLY),
            state_path="state",
            mode_path="mode",
            target_temp_path="target_temp",
            current_temp_path="cur_temp",
            min_temp=TARGET_TEMP_MIN,
            max_temp=TARGET_TEMP_MAX,
            target_temp_step=1,
            mode_value_to_hvac={
                MODE_THERMOSTAT: HVAC_HEAT,
                MODE_POWER: HVAC_HEAT,
                MODE_VENTILATION: HVAC_FAN_ONLY,
            },
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1, HVAC_FAN_ONLY: 1},
            hvac_to_mode_value={HVAC_HEAT: MODE_THERMOSTAT, HVAC_FAN_ONLY: MODE_VENTILATION},
            preset_hvac=HVAC_HEAT,
            mode_value_to_preset={MODE_THERMOSTAT: PRESET_THERMOSTAT, MODE_POWER: PRESET_POWER},
            preset_to_mode_value={PRESET_THERMOSTAT: MODE_THERMOSTAT, PRESET_POWER: MODE_POWER},
            setpoint_mode_values=(MODE_THERMOSTAT,),
            translation_key="autoterm_heater",
        ),
    ),
    numbers=(
        NumberSpec(
            key="power_level_set",
            name="Power level",
            path="power_level",
            write_path="power_level",
            min_value=POWER_LEVEL_MIN,
            max_value=POWER_LEVEL_MAX,
            step=1,
            mode="slider",
            as_int=True,
            write_requires={"state": (1,), "mode": (MODE_POWER,)},
            write_requires_key="autoterm_power_level_mode",
        ),
        NumberSpec(
            key="fan_level_set",
            name="Fan level",
            path="fan_level",
            write_path="fan_level",
            min_value=FAN_LEVEL_MIN,
            max_value=FAN_LEVEL_MAX,
            step=1,
            mode="slider",
            as_int=True,
            write_requires={"state": (1,), "mode": (MODE_VENTILATION,)},
            write_requires_key="autoterm_fan_level_mode",
        ),
    ),
    # The node raises dev_stat 6 when the wall panel took over the session
    # (until the heater is off): commands would be dropped -> lock them.
    control_lock_path="dev_stat",
    control_locks=(ControlLockSpec(key="control_locked", name="Control locked", lock_path="dev_stat"),),
)

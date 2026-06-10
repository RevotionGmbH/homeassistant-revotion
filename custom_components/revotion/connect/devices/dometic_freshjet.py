"""Dometic FreshJet (device 1793) descriptor -- roof air conditioner.

Wire schema fixed by the Brain firmware (``form_dometic_freshjet_data_json`` /
``decode_dom_freshjet_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and
confirmed against the app enums. Flat ``dev_data`` (not nested); booleans 0/1.

    dev_stat   uint8   -- device status enum (diagnostic, §8)
    state      0/1     -- AC on/off
    target_temp float2 -- setpoint (16..30 °C)
    in_temp    float2  -- measured interior temperature (FW current_temp)
    ac_mode    uint8   -- 0 auto, 1 cool, 2 dehumidify, 3 heat, 4 vent (app ACMode.code)
    fan_auto   0/1     -- automatic fan
    fan_speed  uint8   -- 0 low, 1 medium, 2 high, 3 turbo (app FanSpeed.code)
    ex_light   0/1     -- exterior light on/off
    in_light_av 0/1    -- interior light available
    in_light   0/1     -- interior light on/off
    in_light_lv uint8  -- interior brightness level 0 off / 1 half / 2 full
    probe_err / mains_err / heater_av / heater_on / ac_on / sleep_mode  0/1
    err: { err_code uint8, err uint8 }

Control (``decode_dom_freshjet_data_json`` reads): state, target_temp, ac_mode,
fan_auto, fan_speed, ex_light, in_light, in_light_lv, sleep_mode. All top-level.

HVAC mapping: state 0 -> OFF. Otherwise ac_mode via the app's ACMode.code:
0 auto -> HEAT_COOL (Dometic auto holds a setpoint by heating OR cooling, which
is HA's heat_cool; HVACMode.AUTO is reserved for schedule-following devices),
1 -> COOL, 2 -> DRY, 3 -> HEAT, 4 -> FAN_ONLY. Setting an hvac mode keeps state
on and writes the ac_mode code; OFF writes state 0 keeping the current ac_mode.

Fan: fan_auto + fan_speed split (like the hvac state/mode split). "auto" ->
fan_auto=1; low/medium/high/turbo -> fan_auto=0 + fan_speed 0..3.

Lights: ex_light (plain on/off) and in_light + in_light_lv (dimmable, levels
0..2, gated by in_light_av).
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    BinarySensorSpec,
    ClimateSpec,
    ConnectDeviceDescriptor,
    LightSpec,
    SensorSpec,
    SwitchSpec,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"
HVAC_HEAT_COOL = "heat_cool"
HVAC_DRY = "dry"
HVAC_FAN_ONLY = "fan_only"

# ac_mode raw values (app ACMode.code, the value actually sent on the wire).
AC_AUTO = 0
AC_COOL = 1
AC_DEHUMIDIFY = 2
AC_HEAT = 3
AC_VENT = 4

# Fan modes (app FanSpeed.code).
FAN_AUTO = "auto"
FAN_LOW = "low"
FAN_MEDIUM = "medium"
FAN_HIGH = "high"
FAN_TURBO = "turbo"
SPEED_LOW = 0
SPEED_MEDIUM = 1
SPEED_HIGH = 2
SPEED_TURBO = 3

# Interior light brightness levels (app InternalLight: 0 off, 1 half, 2 full).
IN_LIGHT_MAX = 2

FRESHJET_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.DOMETIC_FRESHJET,
    name="Dometic FreshJet",
    sensors=(
        SensorSpec(
            path="dev_stat",
            key="dev_stat",
            name="Device status",
            entity_category="diagnostic",
        ),
        SensorSpec(
            path="err.err_code",
            key="err_code",
            name="Error code",
            entity_category="diagnostic",
        ),
    ),
    binary_sensors=(
        BinarySensorSpec(
            path="probe_err",
            key="probe_error",
            name="Probe error",
            device_class="problem",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="mains_err",
            key="mains_error",
            name="Mains error",
            device_class="problem",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="heater_on",
            key="heater_on",
            name="Heater running",
            device_class="running",
        ),
        BinarySensorSpec(
            path="ac_on",
            key="ac_on",
            name="Compressor running",
            device_class="running",
        ),
    ),
    climates=(
        ClimateSpec(
            key="climate",
            name="Air conditioner",
            hvac_modes=(HVAC_OFF, HVAC_HEAT_COOL, HVAC_COOL, HVAC_HEAT, HVAC_DRY, HVAC_FAN_ONLY),
            state_path="state",
            mode_path="ac_mode",
            target_temp_path="target_temp",
            current_temp_path="in_temp",
            min_temp=16,
            max_temp=30,
            target_temp_step=1,
            mode_value_to_hvac={
                AC_AUTO: HVAC_HEAT_COOL,
                AC_COOL: HVAC_COOL,
                AC_DEHUMIDIFY: HVAC_DRY,
                AC_HEAT: HVAC_HEAT,
                AC_VENT: HVAC_FAN_ONLY,
            },
            hvac_to_state={
                HVAC_OFF: 0,
                HVAC_HEAT_COOL: 1,
                HVAC_COOL: 1,
                HVAC_HEAT: 1,
                HVAC_DRY: 1,
                HVAC_FAN_ONLY: 1,
            },
            hvac_to_mode_value={
                HVAC_HEAT_COOL: AC_AUTO,
                HVAC_COOL: AC_COOL,
                HVAC_DRY: AC_DEHUMIDIFY,
                HVAC_HEAT: AC_HEAT,
                HVAC_FAN_ONLY: AC_VENT,
            },
            fan_modes=(FAN_AUTO, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_TURBO),
            fan_auto_path="fan_auto",
            fan_speed_path="fan_speed",
            fan_auto_mode=FAN_AUTO,
            speed_value_to_fan={
                SPEED_LOW: FAN_LOW,
                SPEED_MEDIUM: FAN_MEDIUM,
                SPEED_HIGH: FAN_HIGH,
                SPEED_TURBO: FAN_TURBO,
            },
            fan_to_speed_value={
                FAN_LOW: SPEED_LOW,
                FAN_MEDIUM: SPEED_MEDIUM,
                FAN_HIGH: SPEED_HIGH,
                FAN_TURBO: SPEED_TURBO,
            },
        ),
    ),
    switches=(
        SwitchSpec(
            key="sleep_mode",
            name="Sleep mode",
            path="sleep_mode",
            write_key="sleep_mode",
        ),
    ),
    lights=(
        LightSpec(
            key="exterior_light",
            name="Exterior light",
            state_path="ex_light",
            state_write_path="ex_light",
        ),
        LightSpec(
            key="interior_light",
            name="Interior light",
            state_path="in_light",
            state_write_path="in_light",
            level_path="in_light_lv",
            level_write_path="in_light_lv",
            level_max=IN_LIGHT_MAX,
            available_path="in_light_av",
        ),
    ),
)

"""Truma CP+ (device 514) descriptor -- combined heater + air conditioner.

The most complex Connect device: two independent sections (heater, air_con),
each nested under its own key, plus global auto fields. Wire schema fixed by the
Brain firmware (``form_truma_cpp_data_json`` / ``decode_truma_cpp_data_json``)
and checked against the app enums. Booleans are 0/1.

Top-level:
    auto_av    uint8  -- global auto-mode available
    auto_mode  uint8  -- global auto on/off (writable)
    auto_t_temp float2 -- global auto setpoint (writable)

heater. (nested, like Truma Combi but under "heater."):
    is_con     0/1    -- heater connected (availability gate)
    dev_stat   uint8  -- 6/7 = heater panel in use/error -> heater control locked
    comb_water { state, t_temp(int16, default 40), p_lim, frost_ctr, av_230V, win_sw_cl }
    comb_air   { state, t_temp(float2) }
    energy_sel uint8  -- 1 gas, 2 electric, 3 both
    energy_en  0/1
    err        { err_code "<str>", hintOrError }

air_con. (nested):
    is_con     0/1    -- AC connected (availability gate)
    dev_stat   uint8  -- 6/7 = AC panel in use/error -> AC control locked
    state      0/1    -- AC on/off
    t_temp     float2 -- setpoint (16..31, 18..25 in global auto)
    cur_temp   float2 -- measured
    fan_mid_av 0/1    -- "mid" fan speed available
    fan_mode   uint8  -- 1 low, 2 mid, 3 high, 4 night (app FanModeTruma)
    ac_auto_av 0/1    -- "auto" ac mode available
    ac_heat_av 0/1    -- "heat" ac mode available
    ac_mode    uint8  -- app ACModeTruma.code: 0 off, 1 fan, 2 cool, 3 heat, 4 auto
    light_av / light / light_bri  -- interior light avail / on-off / brightness
    ac_pow_av / ac_pow            -- AC power limit avail / value
    err        { err_code "<str>", hintOrError }

Control (``decode_truma_cpp_data_json`` reads): top auto_mode, auto_t_temp;
heater.comb_water{state,t_temp,p_lim}, heater.comb_air{state,t_temp},
heater.energy_sel; air_con{state,t_temp,fan_mode,ac_mode,light,light_bri,ac_pow}.

HEATER air climate: OFF/HEAT from heater.comb_air.state (mode_path=None, like
Truma Combi air), gated by heater.is_con.

AIR_CON climate: hvac from state + ac_mode (ACModeTruma codes!):
  state 0 -> OFF; state 1 + ac_mode 2 -> COOL, 3 -> HEAT, 1 -> FAN_ONLY,
  4 -> HEAT_COOL (auto). Gated by air_con.is_con. fan_mode is the direct
  air_con.fan_mode integer (low/mid/high/night) with DYNAMIC filtering: "mid"
  only when fan_mid_av, "night" only while cooling (app
  FanModeTruma.availableModes). HEAT/HEAT_COOL hvac modes are likewise dynamic:
  HEAT needs air_con.ac_heat_av, HEAT_COOL needs air_con.ac_auto_av
  (hvac_mode_av_flags); OFF/COOL/FAN_ONLY are always offered.

light air_con.light (+ brightness air_con.light_bri, app range 20..100 ->
level_min=20) gated by air_con.light_av.
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    BinarySensorSpec,
    ClimateSpec,
    ConnectDeviceDescriptor,
    ControlLockSpec,
    LightSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"
HVAC_HEAT_COOL = "heat_cool"
HVAC_FAN_ONLY = "fan_only"

# air_con ac_mode raw values (app ACModeTruma.code -- NOT sequential!).
AC_OFF = 0
AC_FAN = 1
AC_COOL = 2
AC_HEAT = 3
AC_AUTO = 4

# air_con fan_mode raw values (app FanModeTruma.code).
FAN_LOW = "low"
FAN_MID = "mid"
FAN_HIGH = "high"
FAN_NIGHT = "night"
FAN_LOW_V = 1
FAN_MID_V = 2
FAN_HIGH_V = 3
FAN_NIGHT_V = 4

# heater energy_sel (app: 1 gas, 2 electric, 3 both).
ENERGY_GAS = 1
ENERGY_ELECTRIC = 2
ENERGY_BOTH = 3

# heater comb_water power limit (W).
P_LIM_900 = 900
P_LIM_1800 = 1800

# Interior light brightness max (Truma light_bri; range assumed 0..100, TO VERIFY).
LIGHT_BRI_MAX = 100

TRUMA_CPP_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.TRUMA_CPP,
    name="Truma CP Plus",
    sensors=(
        SensorSpec(path="heater.dev_stat", key="heater_dev_stat", name="Heater status", entity_category="diagnostic"),
        SensorSpec(
            path="heater.err.err_code", key="heater_err", name="Heater error code", entity_category="diagnostic"
        ),
        SensorSpec(path="air_con.dev_stat", key="aircon_dev_stat", name="AC status", entity_category="diagnostic"),
        SensorSpec(path="air_con.err.err_code", key="aircon_err", name="AC error code", entity_category="diagnostic"),
    ),
    binary_sensors=(
        BinarySensorSpec(
            path="heater.is_con",
            key="heater_connected",
            name="Heater connected",
            device_class="connectivity",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="air_con.is_con",
            key="aircon_connected",
            name="AC connected",
            device_class="connectivity",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="heater.comb_water.av_230V",
            key="heater_av_230v",
            name="Heater 230V available",
            device_class="power",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="heater.comb_water.win_sw_cl",
            key="heater_window",
            name="Heater window closed",
            device_class="window",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="heater.comb_water.frost_ctr",
            key="heater_frost",
            name="Heater frost control",
            entity_category="diagnostic",
        ),
    ),
    climates=(
        # Heater air zone (like Truma Combi air, gated by heater.is_con).
        ClimateSpec(
            key="heater_air",
            name="Heater",
            hvac_modes=(HVAC_OFF, HVAC_HEAT),
            state_path="heater.comb_air.state",
            target_temp_path="heater.comb_air.t_temp",
            mode_path=None,
            available_path="heater.is_con",
            lock_path="heater.dev_stat",
            min_temp=5,
            max_temp=30,
            target_temp_step=1,
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1},
        ),
        # Air conditioner: hvac from state + ac_mode; dynamic fan_modes.
        ClimateSpec(
            key="air_con",
            name="Air conditioner",
            hvac_modes=(HVAC_OFF, HVAC_COOL, HVAC_HEAT, HVAC_FAN_ONLY, HVAC_HEAT_COOL),
            state_path="air_con.state",
            mode_path="air_con.ac_mode",
            target_temp_path="air_con.t_temp",
            current_temp_path="air_con.cur_temp",
            available_path="air_con.is_con",
            lock_path="air_con.dev_stat",
            min_temp=16,
            max_temp=31,
            target_temp_step=1,
            mode_value_to_hvac={
                AC_COOL: HVAC_COOL,
                AC_HEAT: HVAC_HEAT,
                AC_FAN: HVAC_FAN_ONLY,
                AC_AUTO: HVAC_HEAT_COOL,
            },
            # heat/heat_cool only offered when the unit advertises them (app
            # ACModeTruma.availableModes ac_heat_av/ac_auto_av). OFF/COOL/
            # FAN_ONLY have no flag -> always offered.
            hvac_mode_av_flags={HVAC_HEAT: "air_con.ac_heat_av", HVAC_HEAT_COOL: "air_con.ac_auto_av"},
            hvac_to_state={
                HVAC_OFF: 0,
                HVAC_COOL: 1,
                HVAC_HEAT: 1,
                HVAC_FAN_ONLY: 1,
                HVAC_HEAT_COOL: 1,
            },
            hvac_to_mode_value={
                HVAC_COOL: AC_COOL,
                HVAC_HEAT: AC_HEAT,
                HVAC_FAN_ONLY: AC_FAN,
                HVAC_HEAT_COOL: AC_AUTO,
            },
            # Direct single fan field; dynamic filtering: mid needs fan_mid_av,
            # night only while cooling (app FanModeTruma.availableModes).
            fan_modes=(FAN_LOW, FAN_MID, FAN_HIGH, FAN_NIGHT),
            fan_value_path="air_con.fan_mode",
            fan_value_to_mode={FAN_LOW_V: FAN_LOW, FAN_MID_V: FAN_MID, FAN_HIGH_V: FAN_HIGH, FAN_NIGHT_V: FAN_NIGHT},
            fan_mode_to_value={FAN_LOW: FAN_LOW_V, FAN_MID: FAN_MID_V, FAN_HIGH: FAN_HIGH_V, FAN_NIGHT: FAN_NIGHT_V},
            fan_mode_av_flags={FAN_MID: "air_con.fan_mid_av"},
            fan_mode_hvac_only={FAN_NIGHT: (HVAC_COOL,)},
        ),
    ),
    lights=(
        LightSpec(
            key="aircon_light",
            name="Interior light",
            state_path="air_con.light",
            state_write_path="air_con.light",
            level_path="air_con.light_bri",
            level_write_path="air_con.light_bri",
            level_max=LIGHT_BRI_MAX,
            # App brightness range is 20..100; a non-zero write is clamped up to
            # 20 so the device never receives a sub-minimum dim level.
            level_min=20,
            available_path="air_con.light_av",
            lock_path="air_con.dev_stat",
        ),
    ),
    switches=(
        SwitchSpec(
            key="heater_water",
            name="Water heater",
            path="heater.comb_water.state",
            write_key="heater.comb_water.state",
            lock_path="heater.dev_stat",
        ),
    ),
    numbers=(),
    selects=(
        SelectSpec(
            key="heater_water_temp",
            name="Water temperature",
            path="heater.comb_water.t_temp",
            write_path="heater.comb_water.t_temp",
            # App + validator allow ONLY 40 (eco) / 60 (hot) -- identical to
            # Truma Combi. A free number would let HA send invalid values that
            # the device rejects (review S1).
            options=("eco", "hot"),
            option_to_value={"eco": 40, "hot": 60},
            lock_path="heater.dev_stat",
        ),
        SelectSpec(
            key="heater_energy",
            name="Heater energy source",
            path="heater.energy_sel",
            write_path="heater.energy_sel",
            options=("gas", "electric", "both"),
            option_to_value={"gas": ENERGY_GAS, "electric": ENERGY_ELECTRIC, "both": ENERGY_BOTH},
            lock_path="heater.dev_stat",
        ),
        SelectSpec(
            key="heater_water_power",
            name="Water power limit",
            path="heater.comb_water.p_lim",
            write_path="heater.comb_water.p_lim",
            options=("900_w", "1800_w"),
            option_to_value={"900_w": P_LIM_900, "1800_w": P_LIM_1800},
            entity_category="config",
            lock_path="heater.dev_stat",
        ),
    ),
    # Per sub-device control lock: heater entities lock on heater.dev_stat 6/7,
    # air_con (incl. the interior light) on air_con.dev_stat 6/7. No device-wide
    # control_lock_path -- each writable spec carries its own lock_path. The app
    # derives a combined top-level state from both, but per-section is more
    # precise (only the locked half blocks).
    control_locks=(
        ControlLockSpec(key="heater_control_locked", name="Heater control locked", lock_path="heater.dev_stat"),
        ControlLockSpec(key="aircon_control_locked", name="AC control locked", lock_path="air_con.dev_stat"),
    ),
)

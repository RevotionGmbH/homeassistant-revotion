"""Truma Combi (device 512) descriptor -- combined air heater + water boiler.

Wire schema fixed by the Brain firmware (``form_truma_combi_data_json`` /
``decode_truma_combi_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and
confirmed against the app. The ``dev_data`` is NESTED: two sub-objects plus
top-level fields. Booleans are 0/1 on the wire (see :mod:`..coding`).

    dev_stat        uint8   -- overall device status enum (diagnostic, §8);
                               6/7 = main panel in use/error -> control locked
    energy_sel      uint8   -- 1 = gas, 2 = electric, 3 = both (prio electric)
    energyEnabled   0/1     -- whether energy selection is available (read-only)
    comb_air: { state 0/1, mode uint8, target_temp float2 (5..30 °C) }
    comb_water: {
        state 0/1, target_temp int16 (default 40), p_lim uint16 (900|1800 W),
        frost_ctr 0/1, av_230V 0/1, win_sw_cl 0/1, man_mode 0/1,
        resp_err 0/1, combi_err 0/1
    }
    err: { err_code "<string>", hintOrError 0/1 }

Control (``decode_truma_combi_data_json`` reads): top-level ``energy_sel``;
``comb_air`` {state, target_temp, mode}; ``comb_water`` {state, target_temp,
p_lim}. There is NO ``reset_err`` for Truma (unlike Airtronic) -- omitted.
Each sub-object is decoded independently, so a command may carry only the
branch(es) it changes.

HVAC mapping (comb_air): the app toggles only ``state`` (on/off) and
``target_temp`` -- it never switches an air ``mode``. So the climate exposes
OFF/HEAT driven purely by ``comb_air.state``; ``comb_air.mode`` is preserved
(sent at its current value) on every command. Truma Combi air has no
current-temperature field, so the climate shows setpoint only (acceptable).

p_lim is a two-value choice (900 / 1800 W) -> modelled as a select (cleaner
than a number with two valid points). Only meaningful when energy_sel is
electric/both, but always shown (the firmware accepts it regardless).
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    BinarySensorSpec,
    ClimateSpec,
    ConnectDeviceDescriptor,
    ControlLockSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"

# energy_sel raw values (app domain: 1 gas, 2 electric, 3 both prio electric).
ENERGY_GAS = 1
ENERGY_ELECTRIC = 2
ENERGY_BOTH = 3

# comb_water.p_lim power-limit options (W).
P_LIM_900 = 900
P_LIM_1800 = 1800

# comb_water.target_temp options (°C). App toggle: eco 40 / hot 60 only.
WATER_TEMP_ECO = 40
WATER_TEMP_HOT = 60

TRUMA_COMBI_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.TRUMA_COMBI,
    name="Truma Combi",
    sensors=(
        SensorSpec(
            path="dev_stat",
            key="dev_stat",
            name="Device status",
            entity_category="diagnostic",
        ),
        # err.err_code is a string code on the wire (device_error_str), unlike
        # Airtronic's numeric code. Surfaced as a diagnostic sensor.
        SensorSpec(
            path="err.err_code",
            key="err_code",
            name="Error code",
            entity_category="diagnostic",
        ),
    ),
    binary_sensors=(
        # All under comb_water (nested) per the firmware encode.
        BinarySensorSpec(
            path="comb_water.av_230V",
            key="av_230v",
            name="230V available",
            device_class="power",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="comb_water.win_sw_cl",
            key="window_switch",
            name="Window closed",
            device_class="window",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="comb_water.frost_ctr",
            key="frost_control",
            name="Frost control",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="comb_water.combi_err",
            key="combi_error",
            name="Combi error",
            device_class="problem",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="comb_water.resp_err",
            key="response_error",
            name="Response error",
            device_class="problem",
            entity_category="diagnostic",
        ),
    ),
    climates=(
        ClimateSpec(
            key="air",
            name="Air heater",
            hvac_modes=(HVAC_OFF, HVAC_HEAT),
            state_path="comb_air.state",
            mode_path="comb_air.mode",
            target_temp_path="comb_air.target_temp",
            # Truma Combi air reports no current temperature -> setpoint-only
            # climate (current_temp_path defaults to None).
            min_temp=5,
            max_temp=30,
            target_temp_step=1,
            # No air mode switching in the app: HEAT just sets state=1 and the
            # current mode value is preserved (empty maps -> _build_command
            # falls back to the live mode). state drives OFF/HEAT.
            mode_value_to_hvac={},
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1},
            hvac_to_mode_value={},
        ),
    ),
    switches=(
        SwitchSpec(
            key="water",
            name="Water heater",
            path="comb_water.state",
            write_key="comb_water.state",
        ),
    ),
    selects=(
        # Water target temperature is a TWO-VALUE choice, not a free number:
        # the app toggles only Eco(40)/Hot(60) and its validator rejects any
        # other value (rangeValidator: value != 40.0 && value != 60.0). Modelled
        # as a select so HA cannot send an invalid setpoint (review S1).
        SelectSpec(
            key="water_temperature",
            name="Water temperature",
            path="comb_water.target_temp",
            write_path="comb_water.target_temp",
            options=("eco", "hot"),
            option_to_value={"eco": WATER_TEMP_ECO, "hot": WATER_TEMP_HOT},
        ),
        SelectSpec(
            key="energy_source",
            name="Energy source",
            path="energy_sel",
            write_path="energy_sel",
            options=("gas", "electric", "both"),
            option_to_value={"gas": ENERGY_GAS, "electric": ENERGY_ELECTRIC, "both": ENERGY_BOTH},
        ),
        SelectSpec(
            key="water_power_limit",
            name="Water power limit",
            path="comb_water.p_lim",
            write_path="comb_water.p_lim",
            options=("900_w", "1800_w"),
            option_to_value={"900_w": P_LIM_900, "1800_w": P_LIM_1800},
            entity_category="config",
        ),
    ),
    # Control locked while the Truma's own panel is in use / shows an error
    # (dev_stat 6/7): commands rejected, diagnostic "Control locked" sensor on.
    control_lock_path="dev_stat",
    control_locks=(ControlLockSpec(key="control_locked", name="Control locked", lock_path="dev_stat"),),
)

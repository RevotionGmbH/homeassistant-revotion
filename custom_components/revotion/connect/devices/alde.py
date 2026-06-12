"""Alde 3030 (device 1536) descriptor -- 2-zone hydronic heater + water boiler.

Wire schema fixed by the Brain firmware (``form_alde_data_json`` /
``decode_alde_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and confirmed
against the app enums. Flat ``dev_data``; booleans 0/1.

    dev_stat   uint8   -- device status enum (diagnostic, §8); 6 = main panel
                          in use, 7 = main panel + error -> remote control
                          locked (commands rejected, "Control locked" sensor on)
    state      0/1     -- heater on/off (SHARED by both zones)
    z1_t_temp / z1_c_temp  float2 -- zone 1 target / current temp
    z2_t_temp / z2_c_temp  float2 -- zone 2 target / current temp
    z2_av      0/1     -- zone 2 available
    out_temp   float2  -- outdoor temperature
    out_av     0/1     -- outdoor sensor available
    pump_running / ac1_present / gas_running / ac_230v  0/1
    energy_prio_el  0/1 -- energy priority: 0 gas, 1 electric
    e_power_set uint8  -- electric power 0 off / 1..3 kW
    water_auto_av  0/1 -- water "auto" setting available
    water_state 0/1    -- water heater on/off
    water_setting uint8 -- 0 off, 1 normal, 2 boost, 3 auto (app WaterSetting)
    ac_auto_ctr 0/1    -- AC automatic control
    fuel_av / gas_av  0/1 -- fuel / gas power available
    f_power_set uint8  -- fuel power 0 off / 1..6 kW
    err: { err_code "<string>", hintOrError 0/1 }

Control (``decode_alde_data_json`` reads): state, z1_t_temp, z2_t_temp,
water_state, water_setting, energy_prio_el, e_power_set, gas_running,
ac_auto_ctr, f_power_set. All top-level, no reset_err.

Climate: TWO zones sharing one ``state``. Each zone is an on/off-only climate
(no mode field -> mode_path=None, HVACMode OFF/HEAT from state); a zone set
writes the shared ``state`` plus that zone's target. Zone 2 is presence-gated on
``z2_av`` via ClimateSpec.available_path: on a single-zone install (``z2_av`` 0)
the zone-2 climate is not created at all (and removed if the flag later drops),
so a single-zone Alde shows exactly one zone -- no greyed-out "unavailable"
ghost. It appears live when ``z2_av`` turns on. Zone 1 has no gate (always
present). See connect/discovery.py for the reconciliation.

p_power numbers: e_power_set 0-3 kW, f_power_set 0-6 kW (app
ElectricPowerSetting / FuelPowerSetting, integer steps).
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    BinarySensorSpec,
    ClimateSpec,
    ConnectDeviceDescriptor,
    ControlLockSpec,
    NumberSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
)

HVAC_OFF = "off"
HVAC_HEAT = "heat"

# energy_prio_el bool: 0 gas, 1 electric.
ENERGY_GAS = 0
ENERGY_ELECTRIC = 1

# water_setting (app WaterSetting): 0 off, 1 normal, 2 boost, 3 auto.
WATER_OFF = 0
WATER_NORMAL = 1
WATER_BOOST = 2
WATER_AUTO = 3

ALDE_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.ALDE,
    name="Alde 3030",
    sensors=(
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
        SensorSpec(path="out_temp", key="out_temp", name="Outdoor temperature", device_class="temperature", unit="°C"),
        # err.err_code is a string code on the wire (device_error_str).
        SensorSpec(path="err.err_code", key="err_code", name="Error code", entity_category="diagnostic"),
    ),
    binary_sensors=(
        BinarySensorSpec(path="pump_running", key="pump_running", name="Pump running", device_class="running"),
        BinarySensorSpec(
            path="ac1_present",
            key="ac1_present",
            name="AC1 present",
            device_class="power",
            entity_category="diagnostic",
        ),
        BinarySensorSpec(
            path="ac_230v", key="ac_230v", name="230V present", device_class="power", entity_category="diagnostic"
        ),
        BinarySensorSpec(path="gas_av", key="gas_available", name="Gas available", entity_category="diagnostic"),
        BinarySensorSpec(path="fuel_av", key="fuel_available", name="Fuel available", entity_category="diagnostic"),
        BinarySensorSpec(
            path="out_av", key="outdoor_available", name="Outdoor sensor available", entity_category="diagnostic"
        ),
    ),
    climates=(
        ClimateSpec(
            key="zone1",
            name="Zone 1",
            hvac_modes=(HVAC_OFF, HVAC_HEAT),
            state_path="state",
            target_temp_path="z1_t_temp",
            current_temp_path="z1_c_temp",
            mode_path=None,  # no mode field; OFF/HEAT from shared state
            min_temp=5,
            max_temp=30,
            target_temp_step=1,
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1},
        ),
        ClimateSpec(
            key="zone2",
            name="Zone 2",
            hvac_modes=(HVAC_OFF, HVAC_HEAT),
            state_path="state",
            target_temp_path="z2_t_temp",
            current_temp_path="z2_c_temp",
            mode_path=None,
            # Zone 2 only exists when z2_av; the climate reports unavailable on a
            # single-zone install so it does not surface stale z2 fields.
            available_path="z2_av",
            min_temp=5,
            max_temp=30,
            target_temp_step=1,
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1},
        ),
    ),
    # Feature-flag gating mirrors the app dialog (alde_tile_dialog.dart): gas
    # switch behind gas_av, fuel power behind fuel_av, AC automatic control
    # behind ac1_present. Electric power is ungated there too.
    switches=(
        SwitchSpec(key="water", name="Water heater", path="water_state", write_key="water_state"),
        SwitchSpec(key="gas", name="Gas", path="gas_running", write_key="gas_running", available_path="gas_av"),
        SwitchSpec(
            key="ac_auto_control",
            name="AC automatic control",
            path="ac_auto_ctr",
            write_key="ac_auto_ctr",
            entity_category="config",
            available_path="ac1_present",
        ),
    ),
    numbers=(
        NumberSpec(
            key="electric_power",
            name="Electric power",
            path="e_power_set",
            write_path="e_power_set",
            min_value=0,
            max_value=3,
            step=1,
            unit="kW",
            mode="slider",
            as_int=True,
        ),
        NumberSpec(
            key="fuel_power",
            name="Fuel power",
            path="f_power_set",
            write_path="f_power_set",
            min_value=0,
            max_value=6,
            step=1,
            unit="kW",
            mode="slider",
            as_int=True,
            available_path="fuel_av",
        ),
    ),
    selects=(
        SelectSpec(
            key="energy_priority",
            name="Energy priority",
            path="energy_prio_el",
            write_path="energy_prio_el",
            options=("gas", "electric"),
            option_to_value={"gas": ENERGY_GAS, "electric": ENERGY_ELECTRIC},
        ),
        SelectSpec(
            key="water_setting",
            name="Water setting",
            path="water_setting",
            write_path="water_setting",
            options=("off", "normal", "boost", "auto"),
            option_to_value={"off": WATER_OFF, "normal": WATER_NORMAL, "boost": WATER_BOOST, "auto": WATER_AUTO},
            # The app offers "Auto" only when water_auto_av (or it is the
            # current setting); same per-option gate here.
            option_av_flags={"auto": "water_auto_av"},
        ),
    ),
    # Control is locked while the Alde's own panel is in use / shows an error
    # (dev_stat 6/7): commands are rejected and the diagnostic sensor reports it.
    control_lock_path="dev_stat",
    control_locks=(ControlLockSpec(key="control_locked", name="Control locked", lock_path="dev_stat"),),
)

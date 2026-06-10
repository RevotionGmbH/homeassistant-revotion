"""EcoFlow PowerKit (device 1280) descriptor -- energy storage system.

Wire schema fixed by the Brain firmware (``form_ecoflow_data_json`` /
``decode_ecoflow_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and
checked against the app. Flat ``dev_data``; booleans 0/1.

    dev_stat   uint8   -- device status enum (diagnostic, §8)
    soc        uint8   -- state of charge %
    volt       float2  -- battery voltage V
    total_cur  int32   -- total current
    total_pwr  int32   -- total power W
    12or24     0/1     -- 12V/24V system (diagnostic flag)
    state_AC / state_DC  0/1 -- AC / DC output enabled (writable)
    lim_charge uint8   -- shore-power current limit (writable)
    lim_gen    uint8   -- generator current limit (writable)
    max_cap    uint8   -- usable capacity %
    bat_temp   int8    -- battery temperature °C
    tr / tf    int32   -- time remaining / time-to-full (minutes; firmware
                          splits the signed time_remaining into the two)
    pwr        int32[5]-- per-channel power W

Control (``decode_ecoflow_data_json`` reads): state_AC, state_DC, lim_charge,
lim_gen. gen_start / gen_stop are persistent config (``ctr_config``), NOT
modelled here.

lim_charge / lim_gen unit: the firmware comment says "%", but the app renders
and sends Amperes (shore power 1-15 A, generator 5-60 A -- verified in
ecoflow_dialog_capability_card.dart). The app is the UI source of truth, so the
numbers use Amperes with those ranges. TO VERIFY against a live device.

pwr[5] modelled as an array_sensor family (one power sensor per channel),
mirroring the native battery current-channel pattern.
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    ArraySensorSpec,
    ConnectDeviceDescriptor,
    NumberSpec,
    SensorSpec,
    SwitchSpec,
)

ECOFLOW_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.ECOFLOW,
    name="EcoFlow PowerKit",
    sensors=(
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
        SensorSpec(path="soc", key="soc", name="State of charge", device_class="battery", unit="%"),
        SensorSpec(path="volt", key="voltage", name="Voltage", device_class="voltage", unit="V"),
        SensorSpec(path="total_cur", key="total_current", name="Total current", device_class="current", unit="A"),
        SensorSpec(path="total_pwr", key="total_power", name="Total power", device_class="power", unit="W"),
        SensorSpec(
            path="bat_temp",
            key="battery_temperature",
            name="Battery temperature",
            device_class="temperature",
            unit="°C",
        ),
        SensorSpec(path="tr", key="time_remaining", name="Time remaining", device_class="duration", unit="min"),
        SensorSpec(path="tf", key="time_to_full", name="Time to full", device_class="duration", unit="min"),
        SensorSpec(path="max_cap", key="max_capacity", name="Usable capacity", unit="%", entity_category="diagnostic"),
    ),
    array_sensors=(
        ArraySensorSpec(
            array_key="pwr",
            key_prefix="channel_power",
            name_prefix="Channel power",
            device_class="power",
            unit="W",
        ),
    ),
    switches=(
        SwitchSpec(key="ac_output", name="AC output", path="state_AC", write_key="state_AC"),
        SwitchSpec(key="dc_output", name="DC output", path="state_DC", write_key="state_DC"),
    ),
    numbers=(
        # Shore-power current limit. App: 1-15 A. (FW comment says %; app=A wins.)
        NumberSpec(
            key="shore_power_limit",
            name="Shore power limit",
            path="lim_charge",
            write_path="lim_charge",
            min_value=1,
            max_value=15,
            step=1,
            unit="A",
            device_class="current",
            mode="slider",
            as_int=True,
        ),
        # Generator current limit. App: 5-60 A.
        NumberSpec(
            key="generator_limit",
            name="Generator current limit",
            path="lim_gen",
            write_path="lim_gen",
            min_value=5,
            max_value=60,
            step=1,
            unit="A",
            device_class="current",
            mode="slider",
            as_int=True,
        ),
    ),
)

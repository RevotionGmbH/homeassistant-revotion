"""Victron Orion XS DC-DC charger (device 770) descriptor.

Wire schema from the app models (``node_data_ve_direct_orion_xs_model.dart``).
Flat ``dev_data``:

    volt        float2 -- output (battery) voltage V
    cur         float1 -- output current A
    pwr         float  -- output power W
    in_volt     float2 -- input voltage V
    in_cur      float1 -- input current A
    in_pwr      float  -- input power W
    cs          int    -- Victron charge state (shared enum)
    mode        int    -- device MODE (read here; writes go via charger_on)
    charger_on  0/1    -- charger enabled (writable)
    errors / off_reasons -- code arrays (not modelled as entities)
    dev_stat    uint8  -- device status enum (diagnostic)
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import ConnectDeviceDescriptor, SensorSpec, SwitchSpec
from .ve_direct_shared import charge_state_spec, mode_spec

VE_DIRECT_ORION_XS_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.VE_DIRECT_ORION_XS,
    name="Victron Orion XS",
    sensors=(
        SensorSpec(
            path="volt",
            key="voltage",
            name="Output voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(
            path="cur",
            key="current",
            name="Output current",
            device_class="current",
            unit="A",
            state_class="measurement",
        ),
        SensorSpec(
            path="pwr", key="power", name="Output power", device_class="power", unit="W", state_class="measurement"
        ),
        SensorSpec(
            path="in_volt",
            key="input_voltage",
            name="Input voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(
            path="in_cur",
            key="input_current",
            name="Input current",
            device_class="current",
            unit="A",
            state_class="measurement",
        ),
        SensorSpec(
            path="in_pwr",
            key="input_power",
            name="Input power",
            device_class="power",
            unit="W",
            state_class="measurement",
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    enum_sensors=(
        charge_state_spec(),
        mode_spec(),
    ),
    switches=(SwitchSpec(key="charger_on", name="Charger", path="charger_on", write_key="charger_on"),),
)

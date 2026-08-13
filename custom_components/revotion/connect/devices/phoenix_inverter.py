"""Victron Phoenix Inverter (device 772) descriptor.

Wire schema from the app models (``node_data_phoenix_inverter_model.dart``)
and dialog (``phoenix_inverter_dialog_capability_card.dart``). Flat
``dev_data``:

    volt     float2 -- DC (battery) voltage V
    ac_volt  float1 -- AC output voltage V
    ac_cur   float2 -- AC output current A
    ac_pwr   float  -- AC output power W
    cs       int    -- Victron operating state (shared enum; 9 = inverting)
    mode     int    -- device MODE: 2 on / 4 off / 5 eco (writable)
    errors / warnings / off_reasons -- code arrays (not modelled as entities)
    dev_stat uint8  -- device status enum (diagnostic)
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import ConnectDeviceDescriptor, SelectSpec, SensorSpec
from .ve_direct_shared import charge_state_spec

PHOENIX_INVERTER_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.PHOENIX_INVERTER,
    name="Victron Phoenix Inverter",
    sensors=(
        SensorSpec(
            path="volt",
            key="voltage",
            name="Battery voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(
            path="ac_volt",
            key="ac_voltage",
            name="AC voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(
            path="ac_cur",
            key="ac_current",
            name="AC current",
            device_class="current",
            unit="A",
            state_class="measurement",
        ),
        SensorSpec(
            path="ac_pwr", key="ac_power", name="AC power", device_class="power", unit="W", state_class="measurement"
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    enum_sensors=(charge_state_spec(),),
    selects=(
        # App: 3-way slider on / off / eco (_indexToMode: 2 / 4 / 5).
        SelectSpec(
            key="inverter_mode",
            name="Mode",
            path="mode",
            write_path="mode",
            options=("on", "off", "eco"),
            option_to_value={"on": 2, "off": 4, "eco": 5},
        ),
    ),
)

"""Victron MPPT solar charger (device 769) descriptor.

Wire schema from the app models (``node_data_ve_direct_mppt_model.dart``) and
dialog (``ve_direct_mppt_dialog_capability_card.dart``). Flat ``dev_data``:

    volt        float2 -- battery voltage V
    cur         float1 -- battery (charge) current A
    pv_volt     float2 -- panel voltage V
    pv_pwr      float  -- panel power W
    load_cur    float1 -- load output current A (only with a load output)
    cs          int    -- Victron charge state (shared enum)
    mppt        int    -- tracker state: 0 off / 1 limited / 2 tracking
    load_on     0/1    -- load output state
    load_mode   int    -- load output mode: 0 off / 1 auto / 4 on (writable)
    yld_td      float2 -- yield today kWh
    yld_yd      float2 -- yield yesterday kWh
    yld_tot     float1 -- lifetime yield kWh
    maxp_td     int    -- max power today W
    maxp_yd     int    -- max power yesterday W
    errors / off_reasons -- code arrays (not modelled as entities)
    dev_stat    uint8  -- device status enum (diagnostic)

Config: ``load_av`` "1" (``dev_conf``) marks a charger with a load output; it
gates the load sensors + the load-mode select, mirroring the app (``loadAv``).

State-class notes: ``yld_tot`` is a lifetime counter (total_increasing, energy
dashboard); ``yld_td`` resets daily which total_increasing handles; the
yesterday/max columns are once-a-day snapshots and carry no state class.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import BinarySensorSpec, ConnectDeviceDescriptor, SelectSpec, SensorSpec
from .ve_direct_shared import charge_state_spec, tracker_state_spec


def _has_load_output(conf: Mapping[str, Any]) -> bool:
    """Load sensors/control only exist on chargers with a load output."""
    return conf.get("load_av") == "1"


VE_DIRECT_MPPT_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.VE_DIRECT_MPPT,
    name="Victron MPPT",
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
            path="cur",
            key="current",
            name="Charge current",
            device_class="current",
            unit="A",
            state_class="measurement",
        ),
        SensorSpec(
            path="pv_volt",
            key="pv_voltage",
            name="PV voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
        ),
        SensorSpec(
            path="pv_pwr", key="pv_power", name="PV power", device_class="power", unit="W", state_class="measurement"
        ),
        SensorSpec(
            path="load_cur",
            key="load_current",
            name="Load current",
            device_class="current",
            unit="A",
            state_class="measurement",
            config_gate=_has_load_output,
        ),
        SensorSpec(
            path="yld_td",
            key="yield_today",
            name="Yield today",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
        ),
        SensorSpec(path="yld_yd", key="yield_yesterday", name="Yield yesterday", device_class="energy", unit="kWh"),
        SensorSpec(
            path="yld_tot",
            key="yield_total",
            name="Yield total",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
        ),
        SensorSpec(path="maxp_td", key="max_power_today", name="Max power today", device_class="power", unit="W"),
        SensorSpec(
            path="maxp_yd", key="max_power_yesterday", name="Max power yesterday", device_class="power", unit="W"
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    enum_sensors=(
        charge_state_spec(),
        tracker_state_spec(),
    ),
    binary_sensors=(
        BinarySensorSpec(
            path="load_on",
            key="load_output",
            name="Load output",
            device_class="power",
            config_gate=_has_load_output,
        ),
    ),
    selects=(
        # App: 3-way slider off / auto / on (_indexToLoadMode: 0 / 1 / 4).
        SelectSpec(
            key="load_mode",
            name="Load output mode",
            path="load_mode",
            write_path="load_mode",
            options=("off", "auto", "on"),
            option_to_value={"off": 0, "auto": 1, "on": 4},
            config_gate=_has_load_output,
        ),
    ),
)

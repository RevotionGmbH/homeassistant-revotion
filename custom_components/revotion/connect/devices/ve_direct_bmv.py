"""Victron BMV / SmartShunt (device 768) descriptor -- battery monitor shunt.

Wire schema checked against a live device (Revotion Boot, 2026-08-13) and the
app models (``node_data_ve_direct_bmv_model.dart``). Flat ``dev_data``:

    volt      float2 -- main battery voltage V
    cur       float2 -- current A (signed; negative = discharging)
    pwr       int    -- power W (signed)
    soc       float1 -- state of charge % (absent meaning on a DC meter)
    cons_ah   float2 -- consumed Ah (signed)
    ttg       int    -- time-to-go, minutes
    aux_volt  float2 -- aux input as starter/secondary voltage (aux_typ 1)
    mid_dev   float  -- aux input as mid-point deviation % (aux_typ 2)
    temp      float1 -- aux input as battery temperature C (aux_typ 3)
    relay     0/1    -- relay state (writable only in remote mode)
    cycles    int    -- charge cycles
    chg_kwh   float2 -- lifetime charged energy kWh
    dchg_kwh  float2 -- lifetime discharged energy kWh
    errors    int[]  -- Victron fault codes (not modelled as entities)
    dev_stat  uint8  -- device status enum (diagnostic)

Config (``dev_conf`` via sync): ``aux_typ`` selects which aux reading exists
(0 none / 1 starter voltage / 2 mid-point / 3 temperature); ``dc_meter`` "1"
turns the shunt into a DC meter (no SoC / consumed-Ah semantics); the relay
toggle only obeys remote writes when ``relay_av`` "1" and ``relay_mode`` "2"
(``cap_ve_direct_bmv.dart``: relayControllable). All three drive config gates,
mirroring the app dialog exactly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import ConnectDeviceDescriptor, SensorSpec, SwitchSpec


def _not_dc_meter(conf: Mapping[str, Any]) -> bool:
    """SoC / consumed-Ah / time-to-go only make sense on a battery shunt."""
    return conf.get("dc_meter") != "1"


def _aux_is_starter(conf: Mapping[str, Any]) -> bool:
    return conf.get("aux_typ") == "1"


def _aux_is_midpoint(conf: Mapping[str, Any]) -> bool:
    return conf.get("aux_typ") == "2"


def _aux_is_temperature(conf: Mapping[str, Any]) -> bool:
    return conf.get("aux_typ") == "3"


def _relay_controllable(conf: Mapping[str, Any]) -> bool:
    """The relay only obeys a remote write when the shunt is in remote mode."""
    return conf.get("relay_av") == "1" and conf.get("relay_mode") == "2"


VE_DIRECT_BMV_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.VE_DIRECT_BMV,
    name="Victron BMV / SmartShunt",
    sensors=(
        SensorSpec(
            path="volt", key="voltage", name="Voltage", device_class="voltage", unit="V", state_class="measurement"
        ),
        SensorSpec(
            path="cur", key="current", name="Current", device_class="current", unit="A", state_class="measurement"
        ),
        SensorSpec(path="pwr", key="power", name="Power", device_class="power", unit="W", state_class="measurement"),
        SensorSpec(
            path="soc",
            key="soc",
            name="State of charge",
            device_class="battery",
            unit="%",
            state_class="measurement",
            config_gate=_not_dc_meter,
        ),
        SensorSpec(
            path="cons_ah",
            key="consumed_ah",
            name="Consumed Ah",
            unit="Ah",
            state_class="measurement",
            config_gate=_not_dc_meter,
        ),
        SensorSpec(
            path="ttg",
            key="time_to_go",
            name="Time to go",
            device_class="duration",
            unit="min",
            config_gate=_not_dc_meter,
        ),
        SensorSpec(
            path="aux_volt",
            key="aux_voltage",
            name="Starter voltage",
            device_class="voltage",
            unit="V",
            state_class="measurement",
            config_gate=_aux_is_starter,
        ),
        SensorSpec(
            path="mid_dev",
            key="midpoint_deviation",
            name="Mid-point deviation",
            unit="%",
            state_class="measurement",
            config_gate=_aux_is_midpoint,
        ),
        SensorSpec(
            path="temp",
            key="temperature",
            name="Battery temperature",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
            config_gate=_aux_is_temperature,
        ),
        SensorSpec(
            path="cycles",
            key="charge_cycles",
            name="Charge cycles",
            state_class="total_increasing",
            entity_category="diagnostic",
        ),
        SensorSpec(
            path="chg_kwh",
            key="charged_energy",
            name="Charged energy",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
        ),
        SensorSpec(
            path="dchg_kwh",
            key="discharged_energy",
            name="Discharged energy",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    switches=(SwitchSpec(key="relay", name="Relay", path="relay", write_key="relay", config_gate=_relay_controllable),),
)

"""Victron Phoenix Charger (device 771) descriptor -- read-only.

Wire schema from the app models (``node_data_phoenix_charger_model.dart``).
The firmware models this charger read-only (no ``toDataPayload`` in the app
either), so there are no writable entities. Flat ``dev_data``:

    volt / cur    float -- output 1 voltage V / current A
    volt2 / cur2  float -- output 2 (only on tri-output models)
    volt3 / cur3  float -- output 3 (only on tri-output models)
    cs            int   -- Victron charge state (shared enum)
    mode          int   -- device MODE (read-only diagnostic)
    errors        int[] -- code array (not modelled as entities)
    dev_stat      uint8 -- device status enum (diagnostic)

Config: ``num_outputs`` (``dev_conf``, "1"/"3") -- outputs 2+3 exist only on
tri-output models (app: ``triOutput = cap.numOutputs == 3``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import ConnectDeviceDescriptor, SensorSpec
from .ve_direct_shared import charge_state_spec, mode_spec


def _tri_output(conf: Mapping[str, Any]) -> bool:
    """Outputs 2 and 3 only exist on tri-output chargers."""
    return conf.get("num_outputs") == "3"


PHOENIX_CHARGER_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.PHOENIX_CHARGER,
    name="Victron Phoenix Charger",
    sensors=(
        SensorSpec(
            path="volt", key="voltage", name="Voltage", device_class="voltage", unit="V", state_class="measurement"
        ),
        SensorSpec(
            path="cur", key="current", name="Current", device_class="current", unit="A", state_class="measurement"
        ),
        SensorSpec(
            path="volt2",
            key="voltage_ch2",
            name="Voltage CH2",
            device_class="voltage",
            unit="V",
            state_class="measurement",
            config_gate=_tri_output,
        ),
        SensorSpec(
            path="cur2",
            key="current_ch2",
            name="Current CH2",
            device_class="current",
            unit="A",
            state_class="measurement",
            config_gate=_tri_output,
        ),
        SensorSpec(
            path="volt3",
            key="voltage_ch3",
            name="Voltage CH3",
            device_class="voltage",
            unit="V",
            state_class="measurement",
            config_gate=_tri_output,
        ),
        SensorSpec(
            path="cur3",
            key="current_ch3",
            name="Current CH3",
            device_class="current",
            unit="A",
            state_class="measurement",
            config_gate=_tri_output,
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    enum_sensors=(
        charge_state_spec(),
        mode_spec(),
    ),
)

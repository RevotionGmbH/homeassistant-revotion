"""CAN battery / BMS (device 773) descriptor -- read-only.

Wire schema from the app models (``node_data_can_battery_model.dart``) and
dialog (``can_battery_dialog_capability_card.dart``). The BMS is strictly
read-only (the app model implements no ``NodeDataWritable``). Flat
``dev_data``:

    volt / cur       float -- battery voltage V / current A (signed)
    soc              float -- state of charge %
    soh              int   -- state of health % (only when soh_av)
    temp             float -- battery temperature C
    cell_min_mv/_max int   -- extreme cell voltages mV (0 = not reported)
    cell_temp_min/max float -- extreme cell temperatures C (when cell_temp_av)
    tr               int   -- remaining time min (when tr_av)
    conn_cnt         int   -- connected battery packs (0 = not reported)
    chg_en / dchg_en 0/1   -- charge / discharge allowed by the BMS
    cvl / ccl / dcl  float -- charge voltage V / charge A / discharge A limits
                              (when limits_av)
    chg_kwh/dchg_kwh float -- lifetime charged/discharged kWh (when energy_av)
    bank_online/_offline int -- packs online/offline in the bank (when bank_av)
    errors / warnings int[] -- code arrays (not modelled as entities)
    dev_stat         uint8 -- device status enum (diagnostic)

Presence-gating mirrors the app rows: the ``*_av`` wire flags gate their
sensors via ``available_path``; ``cell_min_mv``/``cell_max_mv``/``conn_cnt``
gate on their own value being non-zero (``is_path_available`` treats an
explicit 0 as absent), matching the app's ``!= 0`` row conditions.
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import BinarySensorSpec, ConnectDeviceDescriptor, SensorSpec

CAN_BATTERY_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.CAN_BATTERY,
    name="CAN Battery (BMS)",
    sensors=(
        SensorSpec(
            path="volt", key="voltage", name="Voltage", device_class="voltage", unit="V", state_class="measurement"
        ),
        SensorSpec(
            path="cur", key="current", name="Current", device_class="current", unit="A", state_class="measurement"
        ),
        SensorSpec(
            path="soc", key="soc", name="State of charge", device_class="battery", unit="%", state_class="measurement"
        ),
        SensorSpec(
            path="soh",
            key="soh",
            name="State of health",
            unit="%",
            state_class="measurement",
            available_path="soh_av",
        ),
        SensorSpec(
            path="temp",
            key="temperature",
            name="Temperature",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
        ),
        SensorSpec(
            path="cell_min_mv",
            key="cell_voltage_min",
            name="Cell voltage min",
            device_class="voltage",
            unit="mV",
            state_class="measurement",
            available_path="cell_min_mv",
        ),
        SensorSpec(
            path="cell_max_mv",
            key="cell_voltage_max",
            name="Cell voltage max",
            device_class="voltage",
            unit="mV",
            state_class="measurement",
            available_path="cell_max_mv",
        ),
        SensorSpec(
            path="cell_temp_min",
            key="cell_temperature_min",
            name="Cell temperature min",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
            available_path="cell_temp_av",
        ),
        SensorSpec(
            path="cell_temp_max",
            key="cell_temperature_max",
            name="Cell temperature max",
            device_class="temperature",
            unit="°C",
            state_class="measurement",
            available_path="cell_temp_av",
        ),
        SensorSpec(
            path="tr",
            key="time_remaining",
            name="Time remaining",
            device_class="duration",
            unit="min",
            available_path="tr_av",
        ),
        SensorSpec(
            path="conn_cnt",
            key="battery_packs",
            name="Battery packs",
            entity_category="diagnostic",
            available_path="conn_cnt",
        ),
        SensorSpec(
            path="cvl",
            key="charge_voltage_limit",
            name="Charge voltage limit",
            device_class="voltage",
            unit="V",
            state_class="measurement",
            available_path="limits_av",
        ),
        SensorSpec(
            path="ccl",
            key="charge_current_limit",
            name="Charge current limit",
            device_class="current",
            unit="A",
            state_class="measurement",
            available_path="limits_av",
        ),
        SensorSpec(
            path="dcl",
            key="discharge_current_limit",
            name="Discharge current limit",
            device_class="current",
            unit="A",
            state_class="measurement",
            available_path="limits_av",
        ),
        SensorSpec(
            path="chg_kwh",
            key="charged_energy",
            name="Charged energy",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
            available_path="energy_av",
        ),
        SensorSpec(
            path="dchg_kwh",
            key="discharged_energy",
            name="Discharged energy",
            device_class="energy",
            unit="kWh",
            state_class="total_increasing",
            available_path="energy_av",
        ),
        SensorSpec(
            path="bank_online",
            key="bank_online",
            name="Packs online",
            entity_category="diagnostic",
            available_path="bank_av",
        ),
        SensorSpec(
            path="bank_offline",
            key="bank_offline",
            name="Packs offline",
            entity_category="diagnostic",
            available_path="bank_av",
        ),
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
    ),
    binary_sensors=(
        # App surfaces the inverse as "charge/discharge blocked" chips; the
        # entities carry the wire polarity (on = allowed by the BMS).
        BinarySensorSpec(path="chg_en", key="charge_enabled", name="Charging allowed"),
        BinarySensorSpec(path="dchg_en", key="discharge_enabled", name="Discharging allowed"),
    ),
)

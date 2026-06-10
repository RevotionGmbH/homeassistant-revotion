"""Eberspächer Airtronic 3 (device 256) descriptor -- air heater.

Wire schema fixed by the Brain firmware (``form_airtronic_3_data_json`` /
``decode_airtronic_3_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and
confirmed against the app:

    dev_stat   uint8   -- overall device status enum (diagnostic, §8);
                          6/7 = main panel in use/error -> control locked
    mode       uint8   -- 0 = heat, 1 = ventilate (fan only)
    state      uint8   -- 0 = off, 1 = running
    eco        0/1     -- temperature-reduction (eco) flag
    target_temp float2 -- setpoint, valid 5..36 °C (firmware clamps to 22 else)
    cur_temp   float2  -- measured cabin temperature
    out_temp   float2  -- heater outlet temperature
    cur_err    {err_code, err_class}  -- current fault (nested)
    prev_err   [{err_code, err_class}]  -- fault history (array)

HVAC mapping (from the app's tile logic: ``state == 1`` is on, ``mode == 0``
heat / ``mode >= 1`` ventilate):

    state 0            -> HVACMode.OFF
    state 1, mode 0    -> HVACMode.HEAT
    state 1, mode 1    -> HVACMode.FAN_ONLY

Control: the firmware's control decode reads ``mode``, ``state`` and
``target_temp`` together, so climate.py always sends all three (plus
``reset_err: 0`` so a set never re-triggers an error reset). ``eco`` is a
separate switch.

current/cur_err exposed as sensors; prev_err history is intentionally NOT
surfaced in Phase 2 (nested array of fault structs, low value as an entity --
left for a later diagnostics pass).
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import ClimateSpec, ConnectDeviceDescriptor, ControlLockSpec, SensorSpec, SwitchSpec

# HVACMode value strings (kept as plain strings; climate.py resolves to HVACMode
# so this module needs no HA import, consistent with the other descriptors).
HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_FAN_ONLY = "fan_only"

# Firmware mode-field values.
MODE_HEAT = 0
MODE_VENTILATE = 1

AIRTRONIC3_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.AIRTRONIC3,
    name="Eberspächer Airtronic 3",
    sensors=(
        SensorSpec(
            path="dev_stat",
            key="dev_stat",
            name="Device status",
            entity_category="diagnostic",
        ),
        SensorSpec(
            path="out_temp",
            key="out_temp",
            name="Outlet temperature",
            device_class="temperature",
            unit="°C",
        ),
        # Current fault code (nested cur_err.err_code). Diagnostic; 0 = no fault.
        SensorSpec(
            path="cur_err.err_code",
            key="cur_err_code",
            name="Error code",
            entity_category="diagnostic",
        ),
    ),
    climates=(
        ClimateSpec(
            key="climate",
            name="Heater",
            hvac_modes=(HVAC_OFF, HVAC_HEAT, HVAC_FAN_ONLY),
            state_path="state",
            mode_path="mode",
            target_temp_path="target_temp",
            current_temp_path="cur_temp",
            min_temp=5,
            max_temp=36,
            target_temp_step=1,
            mode_value_to_hvac={MODE_HEAT: HVAC_HEAT, MODE_VENTILATE: HVAC_FAN_ONLY},
            hvac_to_state={HVAC_OFF: 0, HVAC_HEAT: 1, HVAC_FAN_ONLY: 1},
            hvac_to_mode_value={HVAC_HEAT: MODE_HEAT, HVAC_FAN_ONLY: MODE_VENTILATE},
            # The firmware control decode also reads reset_err; send 0 so a
            # temperature/mode change never doubles as an error-reset trigger.
            extra_command_fields={"reset_err": 0},
        ),
    ),
    switches=(
        SwitchSpec(
            key="eco",
            name="Eco",
            path="eco",
            write_key="eco",
        ),
    ),
    # Control locked while the heater's own panel is in use / shows an error
    # (dev_stat 6/7): commands rejected, diagnostic "Control locked" sensor on.
    control_lock_path="dev_stat",
    control_locks=(ControlLockSpec(key="control_locked", name="Control locked", lock_path="dev_stat"),),
)

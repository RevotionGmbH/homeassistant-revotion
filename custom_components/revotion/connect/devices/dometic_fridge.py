"""Dometic Fridge (device 1792) descriptor -- compressor fridge.

Wire schema fixed by the Brain firmware (``form_dometic_fridge_data_json`` /
``decode_dom_fridge_data_json`` in Brain_v2_ESPNOW ``serialization_*.c``) and
checked against the app enums. Flat ``dev_data``; booleans 0/1.

    dev_stat   uint8   -- device status enum (diagnostic, §8)
    state      0/1     -- fridge on/off
    send_mode  uint8   -- 0 = fan_speed controls cooling, 1 = target_temp does
    mode       uint8   -- cooling mode: 0 performance, 2 silent, 3 turbo
                          (app DometicFridgeMode; value 1 exists in firmware as
                          "cooling" but is NOT app-selectable -- see below)
    fan_speed  uint8   -- 5 levels (1..5)
    comp_state 0/1     -- compressor running (read-only)
    cond_fan   0/1     -- condenser fan running (read-only)
    target_temp float2 -- setpoint -10..20 °C
    curr_temp  float2  -- measured temperature
    err: { err_code uint8, err uint8 }

send_mode model (the dual cooling-control scheme, shared with the absorber):
the fridge is controlled EITHER by a discrete fan-speed level (send_mode 0) OR
by a target temperature (send_mode 1). HA can't express "one of two controls is
live", so both are always offered:
  - climate (target_temp) -- its set always sends send_mode=1 so the device
    switches to temperature control.
  - number (fan_speed 1-5) -- its set always sends send_mode=0.
  - select "control_mode" -- exposes send_mode itself so the user sees/picks
    which is active.
The control decode (``decode_dom_fridge_data_json``) reads state, mode,
fan_speed, target_temp and send_mode, so sending send_mode alongside each set
is honoured.

mode (cooling profile) is a separate select, independent of hvac on/off. The
climate therefore has hvac OFF/COOL from ``state`` only (mode_path=None) -- the
fridge "mode" is the cooling profile, not an HVAC mode.

App mode gap: DometicFridgeMode exposes only performance(0), silent(2),
turbo(3). Firmware value 1 ("cooling") is not app-selectable, so the select
offers the same three; an incoming raw 1 maps to no option (current_option
None, no crash). TO VERIFY against a live device.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import (
    BinarySensorSpec,
    ClimateSpec,
    ConnectDeviceDescriptor,
    NumberSpec,
    SelectSpec,
    SensorSpec,
    read_block_path,
)

HVAC_OFF = "off"
HVAC_COOL = "cool"

# Cooling mode (app DometicFridgeMode.code). Value 1 ("cooling") not app-exposed.
MODE_PERFORMANCE = 0
MODE_SILENT = 2
MODE_TURBO = 3

# send_mode: which control is live.
SEND_MODE_FAN_SPEED = 0
SEND_MODE_TARGET_TEMP = 1


def _command_block(data: Mapping[str, Any]) -> dict[str, Any]:
    """Full writable control block, app compressor-fridge ``toDataPayload`` parity.

    Mirrors the app's ``send_mode`` split: level control sends ``fan_speed``,
    temperature control sends ``target_temp`` -- never both. A command that
    explicitly writes the other field anyway (climate set while in level mode)
    still reaches the wire via the fragment merge in ``_expand_command_block``.
    """
    send_mode = read_block_path(data, "send_mode")
    block: dict[str, Any] = {"state": read_block_path(data, "state")}
    if send_mode == SEND_MODE_FAN_SPEED:
        block["fan_speed"] = read_block_path(data, "fan_speed")
    else:
        block["target_temp"] = read_block_path(data, "target_temp")
    block["send_mode"] = send_mode
    block["mode"] = read_block_path(data, "mode")
    return block


FRIDGE_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.DOMETIC_FRIDGE,
    name="Dometic Fridge",
    command_block=_command_block,
    sensors=(
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
        SensorSpec(path="err.err_code", key="err_code", name="Error code", entity_category="diagnostic"),
    ),
    binary_sensors=(
        BinarySensorSpec(path="comp_state", key="compressor", name="Compressor running", device_class="running"),
        BinarySensorSpec(path="cond_fan", key="condenser_fan", name="Condenser fan running", device_class="running"),
    ),
    climates=(
        ClimateSpec(
            key="climate",
            name="Fridge",
            hvac_modes=(HVAC_OFF, HVAC_COOL),
            state_path="state",
            target_temp_path="target_temp",
            current_temp_path="curr_temp",
            mode_path=None,  # cooling profile is a separate select, not hvac mode
            min_temp=-10,
            max_temp=20,
            target_temp_step=1,
            hvac_to_state={HVAC_OFF: 0, HVAC_COOL: 1},
            # A temperature set switches the device to temperature control.
            extra_command_fields={"send_mode": SEND_MODE_TARGET_TEMP},
        ),
    ),
    numbers=(
        NumberSpec(
            key="fan_speed",
            # App label "Cooling level" (de: "Kühlstufe") -- the wire field is
            # fan_speed, but it sets the cooling intensity, not a fan.
            name="Cooling level",
            path="fan_speed",
            write_path="fan_speed",
            min_value=1,
            max_value=5,
            step=1,
            mode="slider",
            as_int=True,
            # A fan-speed set switches the device to fan-speed control.
            extra_command_fields={"send_mode": SEND_MODE_FAN_SPEED},
        ),
    ),
    selects=(
        SelectSpec(
            key="mode",
            name="Cooling mode",
            path="mode",
            write_path="mode",
            options=("performance", "silent", "turbo"),
            option_to_value={"performance": MODE_PERFORMANCE, "silent": MODE_SILENT, "turbo": MODE_TURBO},
        ),
        SelectSpec(
            key="control_mode",
            name="Control mode",
            path="send_mode",
            write_path="send_mode",
            options=("fan_speed", "target_temp"),
            option_to_value={"fan_speed": SEND_MODE_FAN_SPEED, "target_temp": SEND_MODE_TARGET_TEMP},
            entity_category="config",
        ),
    ),
    # No power switch: climate hvac OFF (which writes `state`) is the on/off
    # control; a second switch writing the same field would be contradictory.
)

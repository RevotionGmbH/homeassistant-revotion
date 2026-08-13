"""Dometic Fridge Absorber (device 1794) descriptor -- absorber fridge.

Wire schema fixed by the Brain firmware (``form_dometic_fridge_abs_data_json`` /
``decode_dom_fridge_abs_data_json``) and checked against the app enums. Flat
``dev_data``; booleans 0/1.

    dev_stat   uint8   -- device status enum (diagnostic, §8)
    state      0/1     -- fridge on/off
    mode       uint8   -- energy source: 0 automatic, 1 gas, 2 12V, 3 230V AC
                          (app DometicFridgeAbsorberMode)
    send_mode  uint8   -- 0 = fan_speed controls, 1 = target_temp controls
    fan_speed  uint8   -- 5 levels (1..5)
    target_temp float2 -- setpoint -10..20 °C
    curr_temp  float2  -- measured temperature
    buzzer     0/1     -- buzzer (no _av gate)
    fan_one_av / fan_one          -- accessory fan 1 available / state
    fan_second_av / fan_second    -- accessory fan 2 available / state
    ice_maker_av / ice_maker      -- ice maker available / state
    frame_heater_av / frame_heater-- frame heater available / state
    auto_mode_av  0/1  -- automatic energy-source mode available
    err: { err_code uint8, err uint8 }

Same send_mode dual-control scheme as the compressor fridge (see
dometic_fridge.py): climate (target_temp, send_mode=1) + number (fan_speed,
send_mode=0) + select control_mode. mode here is the ENERGY SOURCE select
(auto/gas/12V/AC), not a cooling profile. climate hvac OFF/COOL from state only.

Accessory switches (buzzer, fan_one, fan_second, ice_maker, frame_heater) are
gated by their ``*_av`` flag via SwitchSpec.available_path -- a switch is
unavailable when its accessory is absent. ``buzzer`` has no ``*_av`` flag, so
it is always available.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...const import ConnectDevice
from ..descriptors import (
    ClimateSpec,
    ConnectDeviceDescriptor,
    NumberSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
    read_block_path,
)

HVAC_OFF = "off"
HVAC_COOL = "cool"

# Energy source (app DometicFridgeAbsorberMode.code).
MODE_AUTOMATIC = 0
MODE_GAS = 1
MODE_12V = 2
MODE_AC = 3

SEND_MODE_FAN_SPEED = 0
SEND_MODE_TARGET_TEMP = 1


def _command_block(data: Mapping[str, Any]) -> dict[str, Any]:
    """Full writable control block, app absorber-fridge ``toDataPayload`` parity.

    The compressor-fridge block (including the ``send_mode`` split, see
    ``dometic_fridge._command_block``) plus the absorber-only comfort toggles.
    """
    send_mode = read_block_path(data, "send_mode")
    block: dict[str, Any] = {"state": read_block_path(data, "state")}
    if send_mode == SEND_MODE_FAN_SPEED:
        block["fan_speed"] = read_block_path(data, "fan_speed")
    else:
        block["target_temp"] = read_block_path(data, "target_temp")
    block["send_mode"] = send_mode
    block["mode"] = read_block_path(data, "mode")
    block["fan_one"] = read_block_path(data, "fan_one")
    block["fan_second"] = read_block_path(data, "fan_second")
    block["frame_heater"] = read_block_path(data, "frame_heater")
    block["ice_maker"] = read_block_path(data, "ice_maker")
    block["buzzer"] = read_block_path(data, "buzzer")
    return block


ABSORBER_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.DOMETIC_FRIDGE_ABS,
    name="Dometic Fridge (Absorber)",
    command_block=_command_block,
    sensors=(
        SensorSpec(path="dev_stat", key="dev_stat", name="Device status", entity_category="diagnostic"),
        SensorSpec(path="err.err_code", key="err_code", name="Error code", entity_category="diagnostic"),
    ),
    climates=(
        ClimateSpec(
            key="climate",
            name="Fridge",
            hvac_modes=(HVAC_OFF, HVAC_COOL),
            state_path="state",
            target_temp_path="target_temp",
            current_temp_path="curr_temp",
            mode_path=None,  # energy source is a separate select, not hvac mode
            min_temp=-10,
            max_temp=20,
            target_temp_step=1,
            hvac_to_state={HVAC_OFF: 0, HVAC_COOL: 1},
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
            extra_command_fields={"send_mode": SEND_MODE_FAN_SPEED},
        ),
    ),
    selects=(
        SelectSpec(
            key="energy_source",
            name="Energy source",
            path="mode",
            write_path="mode",
            options=("automatic", "gas", "v12", "ac"),
            option_to_value={"automatic": MODE_AUTOMATIC, "gas": MODE_GAS, "v12": MODE_12V, "ac": MODE_AC},
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
    switches=(
        # No power switch: climate hvac OFF (which writes `state`) is the on/off
        # control; a second switch writing the same field would be contradictory.
        # buzzer has no _av flag -> always available.
        SwitchSpec(key="buzzer", name="Buzzer", path="buzzer", write_key="buzzer", entity_category="config"),
        SwitchSpec(key="fan_one", name="Fan 1", path="fan_one", write_key="fan_one", available_path="fan_one_av"),
        SwitchSpec(
            key="fan_second", name="Fan 2", path="fan_second", write_key="fan_second", available_path="fan_second_av"
        ),
        SwitchSpec(
            key="ice_maker", name="Ice maker", path="ice_maker", write_key="ice_maker", available_path="ice_maker_av"
        ),
        SwitchSpec(
            key="frame_heater",
            name="Frame heater",
            path="frame_heater",
            write_key="frame_heater",
            available_path="frame_heater_av",
        ),
    ),
)

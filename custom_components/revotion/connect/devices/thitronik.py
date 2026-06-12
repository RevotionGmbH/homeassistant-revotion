"""Thitronik WiPro III (device 1024) descriptor -- alarm system.

Maps the Thitronik ``dev_data`` schema onto HA entities. The schema is fixed by
the Brain firmware (``form_thitronik_data_json`` in Brain_v2_ESPNOW
``serialization_encode.c``) and confirmed against the app's parser; all booleans
are serialized on the wire as 0/1 integers (see :mod:`..coding`):

    dev_stat      uint8  -- overall device status enum (diagnostic, see §8);
                            6/7 = main panel in use/error -> control locked
    alarm         0/1    -- alarm currently active/triggered
    alarm_reason  uint8  -- why the alarm fired (diagnostic)
    armed         0/1    -- system armed
    locked        0/1    -- SafeLock engaged
    ignition      0/1    -- vehicle ignition detected
    gas_alarm     0/1    -- gas sensor alarm
    door_open     0/1    -- door contact open
    bat[]         0/1[]  -- per-sensor battery ok flag (parallel to stat[])
    stat[]        0/1[]  -- per-sensor magnet-contact status (parallel to bat[])

Control is a single opaque command code on ``ctr_data``
(``dev_data: {"command": <code>}``): arm=72, disarm=170, lock=87, unlock=88,
triggerAlarm=115 (``ThitronikCommands`` enum / firmware
``node_payload_schemas.md``).

``triggerAlarm`` (115) is the app's panic tile (PanikWidget): it fires the
siren immediately. Surfaced twice -- as the panel's TRIGGER feature
(``alarm_control_panel.alarm_trigger``, for automations) and as a "Panic
alarm" button entity mirroring the app tile. Confirmation for both is the
``alarm`` flag flipping to 1.
"""

from __future__ import annotations

from ...const import ConnectDevice
from ..descriptors import (
    AlarmPanelSpec,
    ArrayBinarySensorSpec,
    BinarySensorSpec,
    ButtonSpec,
    ConnectDeviceDescriptor,
    ControlLockSpec,
    EnumSensorSpec,
    LockSpec,
    SensorSpec,
)

# device_class / entity_category values are kept as plain strings here so this
# descriptor module needs no HA imports; the platforms resolve them to the HA
# enums (SensorDeviceClass / BinarySensorDeviceClass / EntityCategory).

# Thitronik command codes (ThitronikCommands enum in the app / firmware schema).
COMMAND_ARM = 72
COMMAND_DISARM = 170
COMMAND_LOCK = 87
COMMAND_UNLOCK = 88
COMMAND_TRIGGER_ALARM = 115

# The app's radio-sensor type dropdown (thitronik_dropdown_items.dart), in
# order -- the per-sensor ``app_type`` config indices point into this list.
# English app labels; surfaced as a (non-translatable) state attribute.
THITRONIK_SENSOR_TYPE_LABELS = (
    "Remote control",
    "Magnetic contact",
    "Roof window",
    "Side window R",
    "Side window L",
    "Rear window",
    "Gas sensor",
    "Water sensor",
    "Driver's door",
    "Passenger door",
    "Sliding door",
    "Dinette",
    "Bathroom",
    "Bike rack",
    "Roof rack",
    "Rear rack",
    "Gas alarm",
    "Smoke detector",
    "Cable loop",
    "Heki",
    "Rear garage",
    "Sliding door",
    "Drawer",
)

THITRONIK_DESCRIPTOR = ConnectDeviceDescriptor(
    device=ConnectDevice.THITRONIK,
    name="Thitronik WiPro III",
    sensors=(
        SensorSpec(
            path="dev_stat",
            key="dev_stat",
            name="Device status",
            entity_category="diagnostic",
        ),
    ),
    enum_sensors=(
        # The app's alarm-reason mapping (thitronik_alarm_reason_items.dart):
        # 1-31 = index of the triggered radio sensor (type resolved via the
        # app_type config list), fixed codes for the built-in triggers.
        EnumSensorSpec(
            path="alarm_reason",
            key="alarm_reason",
            name="Alarm reason",
            translation_key="thitronik_alarm_reason",
            value_map=(
                (0, "none"),
                (32, "break_in_door_interior_light"),
                (33, "break_in_door_can_bus"),
                (36, "gas_detector"),
                (37, "jammer_detection"),
                (38, "radio_cable_loop"),
                (40, "radio_water_detector"),
                (43, "radio_gas_detector_co"),
                (224, "sms_alarm"),
                (225, "panic_alarm"),
            ),
            sensor_range=(1, 31),
            sensor_type_config_key="app_type",
            sensor_type_labels=THITRONIK_SENSOR_TYPE_LABELS,
            entity_category="diagnostic",
        ),
    ),
    binary_sensors=(
        BinarySensorSpec(
            path="alarm",
            key="alarm",
            name="Alarm",
            device_class="safety",
        ),
        BinarySensorSpec(
            path="ignition",
            key="ignition",
            name="Ignition",
            device_class="power",
        ),
        BinarySensorSpec(
            path="door_open",
            key="door_open",
            name="Door",
            device_class="door",
        ),
        BinarySensorSpec(
            path="gas_alarm",
            key="gas_alarm",
            name="Gas alarm",
            device_class="gas",
        ),
    ),
    array_binary_sensors=(
        # Magnet contacts: one binary_sensor per stat[] element (firmware
        # ``thitronik_sensor.status``). "opening" is the closest HA device class
        # for a magnet door/window contact (on == open).
        # POLARITY TO VERIFY: whether wire 1 means "open" or "closed" is not
        # documented in the firmware; confirm against a live device.
        ArrayBinarySensorSpec(
            array_key="stat",
            key_prefix="contact",
            name_prefix="Contact",
            # Firmware caps stat[]/bat[] at 30 sensors (serialization_encode.c
            # form_thitronik_data_json); the array length always equals the
            # number of currently paired sensors, so shrinking == deleted.
            max_elements=30,
            device_class="opening",
        ),
        # Per-sensor battery health (firmware ``thitronik_sensor.battery``,
        # parallel to stat[]). device_class "battery" means on == low battery.
        # POLARITY TO VERIFY: the firmware field is a bare bool with no
        # documented polarity -- whether wire 1 means "battery ok" or "battery
        # low" must be confirmed against a live device; if it is "ok", this
        # device_class (and the decode) needs inverting.
        ArrayBinarySensorSpec(
            array_key="bat",
            key_prefix="sensor_battery",
            name_prefix="Sensor battery",
            max_elements=30,
            device_class="battery",
            entity_category="diagnostic",
        ),
    ),
    alarm_panel=AlarmPanelSpec(
        key="alarm_panel",
        name="Alarm",
        armed_path="armed",
        alarm_path="alarm",
        arm_away_command=COMMAND_ARM,
        disarm_command=COMMAND_DISARM,
        trigger_command=COMMAND_TRIGGER_ALARM,
    ),
    buttons=(
        # The app's panic tile: fire the siren immediately. Confirmed (and the
        # command lock released) once the alarm flag reports 1.
        ButtonSpec(
            key="panic",
            name="Panic alarm",
            command=COMMAND_TRIGGER_ALARM,
            confirm_path="alarm",
            icon="mdi:alarm-light",
        ),
    ),
    lock=LockSpec(
        key="lock",
        name="SafeLock",
        locked_path="locked",
        lock_command=COMMAND_LOCK,
        unlock_command=COMMAND_UNLOCK,
        # SafeLock presence is a config flag: capability.config.data["locked"]
        # == "1" (app dev_conf "locked"). The platform creates the lock entity
        # only when this is set.
        config_flag="locked",
    ),
    # Control locked while the Thitronik's own panel is in use / shows an error
    # (dev_stat 6/7): arm/disarm + SafeLock commands rejected, diagnostic
    # "Control locked" sensor on. Matches the app's isConnectivityControllable
    # gate (TO VERIFY live: confirm dev_stat 6/7 actually occurs on Thitronik
    # and that blocking remote disarm during panel use is acceptable).
    control_lock_path="dev_stat",
    control_locks=(ControlLockSpec(key="control_locked", name="Control locked", lock_path="dev_stat"),),
)

"""Cross-platform descriptor schema for Connect devices (cap 12).

The Connect capability is polymorphic: one ``device`` code (e.g. 1024 =
Thitronik) maps to a whole set of HA entities spread across several platforms.
Rather than hand-coding each device in each platform file, a single
:class:`ConnectDeviceDescriptor` declares *everything* one device produces, and
the platform ``async_setup_entry`` functions read the relevant spec lists out of
it (see Ha-Integration-Docs/connect-integration.md §5).

Design goals:

- **One descriptor per device, platform-agnostic.** A descriptor bundles the
  sensor, binary_sensor, alarm_panel and lock specs for a device. Later phases
  add ``climate``/``select``/``number``/``switch`` spec fields *alongside* the
  existing ones -- new optional fields default to empty, so adding them never
  touches a device that does not use them (forward-compatible).
- **Frozen dataclasses.** Specs are immutable value objects shared across all
  instances of a device; they carry no per-entity state.
- **Path-addressed, not value-addressed.** Read specs name an explicit
  ``dev_data`` leaf path (the same dotted path that :mod:`.flatten` produces).
  This is deliberate: routing a field to sensor vs. binary_sensor by inspecting
  its *value* (as the Phase 0 generic mirror nearly did) lets a value flipping
  0/1<->numeric split one path across two platforms with a colliding
  ``unique_id`` (Phase 0 review blocker B1). A fixed path list per platform
  cannot collide.

The flat path uses the dotted form from :mod:`.flatten` (``"comb_water.state"``,
``"bat.0"``). For top-level scalar fields the path is just the key (``"armed"``).
Array elements get an index segment (``"stat.0"``); descriptors that need
per-element entities (Thitronik's magnet contacts / sensor batteries) declare an
:class:`ArrayBinarySensorSpec` instead of one spec per index, so the entity set
grows with the array length reported by the device (mirrors the dynamic battery
current-channel pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class SensorSpec:
    """One read-only ``sensor`` entity backed by a ``dev_data`` leaf.

    Attributes:
        path: Dotted ``dev_data`` leaf path to read (e.g. ``"alarm_reason"``).
        key: Stable suffix for the entity ``unique_id`` and ``translation_key``;
            must be unique within a descriptor across *all* platforms.
        name: Fallback display name when no app config name is available.
        device_class: Optional HA ``SensorDeviceClass`` value (as a string, so
            this module needs no HA import).
        unit: Optional native unit of measurement.
        entity_category: Optional HA ``EntityCategory`` value (``"diagnostic"``
            / ``"config"``) for diagnostic fields like ``dev_stat``.
    """

    path: str
    key: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, kw_only=True)
class EnumSensorSpec:
    """Translated enum ``sensor`` for a numeric ``dev_data`` code field.

    For wire fields that are an opaque code the app renders as localized text
    (Thitronik ``alarm_reason``). The entity gets ``device_class: enum`` with a
    fixed option list and a ``translation_key``, so HA translates the state via
    ``translations/{en,de}.json`` (``entity.sensor.<translation_key>.state.*``).
    The entity name also comes from the translation (no ``_attr_name``).

    Codes resolve in this order:

    1. An exact match in :attr:`value_map` -> that option.
    2. Inside :attr:`sensor_range` (Thitronik 1-31 = index of the triggered
       radio sensor) -> ``f"{sensor_option_prefix}_{code}"``.
    3. Anything else -> :attr:`unknown_option`.

    For range codes the spec can additionally resolve *which kind* of sensor
    triggered: the app config carries a per-sensor type index list
    (:attr:`sensor_type_config_key`, e.g. ``"app_type"``, top-level or nested
    under ``dev_conf`` in ``capability.config.data``) pointing into
    :attr:`sensor_type_labels` (the app's dropdown list). The resolved label is
    surfaced as an extra state attribute -- attributes are not translatable, so
    the labels are plain English.

    Attributes:
        path: Dotted ``dev_data`` leaf path holding the code.
        key: Stable suffix for the entity ``unique_id``.
        name: Fallback display name (used in logs; the UI name comes from the
            translation files via ``translation_key``).
        translation_key: HA translation key for entity name + state texts.
        value_map: Ordered ``(wire code, option string)`` pairs for fixed codes.
        sensor_range: Optional inclusive ``(low, high)`` code range mapped to
            per-sensor options; ``None`` if the device has no such range.
        sensor_option_prefix: Option prefix for range codes (``"sensor"`` ->
            ``sensor_1`` ...).
        sensor_type_config_key: Optional ``capability.config.data`` key holding
            the per-sensor type index list (checked top-level and under
            ``dev_conf``). ``None`` disables the type attribute.
        sensor_type_labels: Label per type index (the app's dropdown order).
        unknown_option: Option reported for unmapped codes.
        entity_category: Optional HA ``EntityCategory`` value.
    """

    path: str
    key: str
    name: str
    translation_key: str
    value_map: tuple[tuple[int, str], ...]
    sensor_range: tuple[int, int] | None = None
    sensor_option_prefix: str = "sensor"
    sensor_type_config_key: str | None = None
    sensor_type_labels: tuple[str, ...] = ()
    unknown_option: str = "unknown"
    entity_category: str | None = None

    def options(self) -> tuple[str, ...]:
        """Return every option this sensor can report (for ``_attr_options``)."""
        opts = [option for _, option in self.value_map]
        if self.sensor_range is not None:
            low, high = self.sensor_range
            opts += [f"{self.sensor_option_prefix}_{code}" for code in range(low, high + 1)]
        opts.append(self.unknown_option)
        return tuple(opts)

    def option_for(self, code: int) -> str:
        """Map a wire code to its option string (see class docstring order)."""
        for value, option in self.value_map:
            if code == value:
                return option
        if self.sensor_range is not None:
            low, high = self.sensor_range
            if low <= code <= high:
                return f"{self.sensor_option_prefix}_{code}"
        return self.unknown_option


@dataclass(frozen=True, kw_only=True)
class BinarySensorSpec:
    """One read-only ``binary_sensor`` entity backed by a 0/1 ``dev_data`` leaf.

    The wire value is the firmware's 0/1 integer (bool serialized as a number,
    see :mod:`.coding`); the entity decodes it via ``int01_to_bool``.

    Attributes:
        path: Dotted ``dev_data`` leaf path to read (e.g. ``"door_open"``).
        key: Stable suffix for ``unique_id`` / ``translation_key``; unique
            within the descriptor.
        name: Fallback display name.
        device_class: Optional HA ``BinarySensorDeviceClass`` value.
        entity_category: Optional HA ``EntityCategory`` value.
    """

    path: str
    key: str
    name: str
    device_class: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, kw_only=True)
class ArrayBinarySensorSpec:
    """A family of ``binary_sensor`` entities, one per element of a 0/1 array.

    Used for ``dev_data`` arrays where each index is an independent flag and the
    count is device-/install-dependent (Thitronik ``stat[]`` magnet contacts and
    ``bat[]`` sensor batteries). The platform reconciles the entity set against
    the array length present in the data: ``key_prefix_{n}`` entities are created
    as the array grows and removed (live + registry row) when it shrinks --
    e.g. sensors deleted in the app must not linger in HA as "unavailable"
    ghosts. Indices from the current length up to ``max_elements`` are swept so
    ghost registry rows left by an earlier session are cleaned up after a
    restart too (same mechanism as the presence-gated entities, see
    :mod:`..discovery`).

    Attributes:
        array_key: Top-level ``dev_data`` key holding the list (e.g. ``"stat"``).
        key_prefix: Prefix for each element's ``unique_id`` suffix /
            ``translation_key`` (``"contact"`` -> ``contact_1``, ...).
        name_prefix: Per-element fallback display name prefix
            (``"Contact"`` -> ``"Contact 1"``). 1-based to match user-facing
            sensor numbering.
        max_elements: Firmware upper bound of the array length (Thitronik: 30).
            Defines how far the ghost-cleanup sweep reaches; never creates
            entities beyond the actual data.
        device_class: Optional HA ``BinarySensorDeviceClass`` value applied to
            every element.
        entity_category: Optional HA ``EntityCategory`` value.
    """

    array_key: str
    key_prefix: str
    name_prefix: str
    max_elements: int
    device_class: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, kw_only=True)
class ArraySensorSpec:
    """A family of read-only ``sensor`` entities, one per array element.

    The numeric counterpart of :class:`ArrayBinarySensorSpec`: for a ``dev_data``
    array of scalar values where each index is its own measurement (EcoFlow
    ``pwr[5]`` per-channel power). One ``sensor`` per element, reconciled against
    the array length in the data (created as the array grows, removed live +
    registry when it shrinks; ghost rows swept up to ``max_elements``).

    Attributes:
        array_key: Top-level ``dev_data`` key holding the list (e.g. ``"pwr"``).
        key_prefix: Prefix for each element's ``unique_id`` suffix /
            ``translation_key`` (``"power"`` -> ``power_1``, ...).
        name_prefix: Per-element fallback display name prefix (1-based).
        max_elements: Firmware upper bound of the array length (EcoFlow: 5).
        device_class: Optional HA ``SensorDeviceClass`` value for every element.
        unit: Optional native unit of measurement.
        entity_category: Optional HA ``EntityCategory`` value.
    """

    array_key: str
    key_prefix: str
    name_prefix: str
    max_elements: int
    device_class: str | None = None
    unit: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, kw_only=True)
class AlarmPanelSpec:
    """Spec for an ``alarm_control_panel`` entity driven by ``dev_data`` flags.

    State derivation (see Ha-Integration-Docs/connect-integration.md §6): when the ``alarm``
    flag is set the panel reports TRIGGERED; otherwise ARMED_AWAY when ``armed``
    is set, else DISARMED. Arm/disarm publish a ``command`` code via the Connect
    control plumbing (``ctr_data``).

    Attributes:
        key: Stable suffix for the entity ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        armed_path: ``dev_data`` leaf holding the 0/1 "armed" flag.
        alarm_path: ``dev_data`` leaf holding the 0/1 "alarm active" flag;
            ``None`` if the device has no triggered state.
        arm_away_command: Command code published to arm (Thitronik: 72).
        disarm_command: Command code published to disarm (Thitronik: 170).
        trigger_command: Optional command code that fires the alarm directly
            (Thitronik panic: 115). When set, the panel supports the
            ``alarm_trigger`` service (``AlarmControlPanelEntityFeature.TRIGGER``);
            ``None`` means the device cannot be triggered remotely.
    """

    key: str
    name: str
    armed_path: str
    alarm_path: str | None
    arm_away_command: int
    disarm_command: int
    trigger_command: int | None = None


@dataclass(frozen=True, kw_only=True)
class ButtonSpec:
    """Spec for a ``button`` entity that publishes a bare command code.

    A stateless one-shot action (Thitronik panic alarm). Pressing publishes
    ``dev_data: {"command": <code>}`` via the Connect control plumbing under
    the usual single-command lock.

    Attributes:
        key: Stable suffix for the entity ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        command: Command code published on press (Thitronik panic: 115).
        confirm_path: Optional 0/1 ``dev_data`` leaf that confirms the press
            (Thitronik: ``"alarm"`` flips to 1 once the siren fires). The
            command lock is held until the flag reads truthy (or the 60 s
            timeout). ``None`` releases the lock on the next coordinator
            update (no device feedback for the action).
        icon: Optional MDI icon (buttons carry no device_class that fits).
        entity_category: Optional HA ``EntityCategory`` value.
    """

    key: str
    name: str
    command: int
    confirm_path: str | None = None
    icon: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, kw_only=True)
class LockSpec:
    """Spec for a ``lock`` entity driven by a ``dev_data`` flag.

    Attributes:
        key: Stable suffix for the entity ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        locked_path: ``dev_data`` leaf holding the 0/1 "locked" flag.
        lock_command: Command code published to lock (Thitronik: 87).
        unlock_command: Command code published to unlock (Thitronik: 88).
        config_flag: Optional ``capability.config.data`` key that gates whether
            the lock entity is created at all (Thitronik SafeLock:
            ``"locked"`` == ``"1"``). ``None`` means always create.
    """

    key: str
    name: str
    locked_path: str
    lock_command: int
    unlock_command: int
    config_flag: str | None = None


@dataclass(frozen=True, kw_only=True)
class ClimateSpec:
    """Spec for a ``climate`` entity (heater / AC) driven by ``dev_data``.

    The HVAC state is split across two ``dev_data`` fields, mirroring how the
    devices model it: an on/off field (``state_path``, 0/1) and a mode field
    (``mode_path``, an integer). The mapping is declarative so later heaters
    (Truma, Alde) only differ by paths and tables, not code:

    - **Read:** if ``state_path`` reads falsy -> :data:`hvac_off`. Otherwise the
      raw ``mode_path`` value is looked up in :attr:`mode_value_to_hvac`
      (fallback: the first non-off mode in :attr:`hvac_modes`).
    - **Write:** the target :class:`HVACMode` is looked up in
      :attr:`hvac_to_state` (the 0/1 to send for ``state_path``) and, when on,
      :attr:`hvac_to_mode_value` (the integer to send for ``mode_path``). The
      platform also always includes ``target_temp`` so the firmware's
      read-all-fields control decode never resets it (concept §2.2).

    All paths use the dotted :mod:`.flatten` form, so nested heaters
    (``comb_air.state``, ``comb_air.target_temp``) work via the same read
    helpers without schema changes.

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        hvac_modes: Supported HVACMode value strings (must include ``"off"``).
        hvac_mode_av_flags: hvac_mode -> ``dev_data`` 0/1 leaf that must be
            truthy for that mode to be offered (Truma CP+ ``heat`` needs
            ``air_con.ac_heat_av``, ``heat_cool`` needs ``air_con.ac_auto_av``).
            A mode without an entry here (incl. OFF) is always offered; an empty
            map -> the static ``hvac_modes`` list (existing devices unchanged).
            Mirrors :attr:`fan_mode_av_flags` for the hvac dimension; makes
            ``hvac_modes`` a dynamically filtered property.
        state_path: ``dev_data`` 0/1 leaf for on/off.
        mode_path: ``dev_data`` integer leaf selecting heat vs. fan etc.
        target_temp_path: ``dev_data`` leaf for the setpoint (read + written).
        current_temp_path: ``dev_data`` leaf for the measured temperature, or
            ``None`` for devices that report no current temperature (e.g. Truma
            Combi air -> setpoint-only climate).
        min_temp / max_temp: Setpoint bounds (°C).
        target_temp_step: Optional UI/step granularity.
        mode_value_to_hvac: Map raw ``mode_path`` value -> HVACMode string
            (used when the device is on).
        hvac_to_state: Map HVACMode string -> 0/1 written to ``state_path``.
        hvac_to_mode_value: Map HVACMode string -> integer written to
            ``mode_path`` (only consulted for on modes).
        extra_command_fields: Static extra ``dev_data`` keys always sent with a
            control command (e.g. ``{"reset_err": 0}`` to clear a latch). May be
            empty.
        fan_modes: Fan-mode option strings, or empty for no FAN_MODE support.
            When non-empty the climate entity advertises ClimateEntityFeature.FAN_MODE.
        fan_auto_path: ``dev_data`` 0/1 leaf for the "auto fan" flag (devices
            like FreshJet split fan into an auto toggle + a discrete speed).
        fan_speed_path: ``dev_data`` integer leaf for the discrete fan speed.
        fan_auto_mode: the fan_modes string that means "auto" (fan_auto=1).
        speed_value_to_fan: raw ``fan_speed_path`` value -> fan_mode string
            (read, used when fan_auto is off).
        fan_to_speed_value: fan_mode string -> raw ``fan_speed_path`` value
            (write, for non-auto modes).
        available_path: optional ``dev_data`` 0/1 leaf gating availability
            (Alde ``z2_av`` zone-2 present, later Truma CP+ heater/air_con
            ``is_con``). The entity reports unavailable when this reads falsy;
            ``None`` means always available.
        fan_value_path: alternative to the auto+speed split -- a single integer
            ``dev_data`` leaf carrying the fan mode directly (Truma CP+
            ``air_con.fan_mode`` low/mid/high/night). Read/write via
            :attr:`fan_value_to_mode` / :attr:`fan_mode_to_value`.
        fan_value_to_mode: raw ``fan_value_path`` value -> fan_mode string.
        fan_mode_to_value: fan_mode string -> raw ``fan_value_path`` value.
        fan_mode_av_flags: fan_mode -> ``dev_data`` 0/1 leaf that must be truthy
            for that mode to be offered (Truma ``mid`` needs ``fan_mid_av``).
            Absent entry -> always offered.
        fan_mode_hvac_only: fan_mode -> the hvac_modes in which it is offered
            (Truma ``night`` only when cooling). Absent entry -> all hvac_modes.
        Together these two make ``fan_modes`` dynamic (filtered per current
        hvac_mode + availability flags); a device with neither (FreshJet) keeps
        the static fan_modes list unchanged.
    """

    key: str
    name: str
    hvac_modes: tuple[str, ...]
    state_path: str
    target_temp_path: str
    available_path: str | None = None
    # Optional dev_stat path that locks remote control when it reads 6 (main
    # panel in use) or 7 (main panel + error). Overrides the descriptor-level
    # control_lock_path; needed by Truma CP+ whose heater/air_con sub-devices
    # carry separate dev_stat fields (heater.dev_stat / air_con.dev_stat).
    lock_path: str | None = None
    # mode_path is None for on/off-only heaters (Alde, Truma air) that have no
    # mode field on the wire; then OFF/<single heat mode> is driven by state
    # alone and no mode field is written.
    mode_path: str | None = None
    current_temp_path: str | None = None
    min_temp: float
    max_temp: float
    target_temp_step: float | None = None
    mode_value_to_hvac: dict[int, str] = field(default_factory=dict)
    hvac_to_state: dict[str, int] = field(default_factory=dict)
    hvac_to_mode_value: dict[str, int] = field(default_factory=dict)
    extra_command_fields: dict[str, int] = field(default_factory=dict)
    # Fan-mode support (optional; empty fan_modes -> no FAN_MODE feature).
    fan_modes: tuple[str, ...] = ()
    fan_auto_path: str | None = None
    fan_speed_path: str | None = None
    fan_auto_mode: str | None = None
    speed_value_to_fan: dict[int, str] = field(default_factory=dict)
    fan_to_speed_value: dict[str, int] = field(default_factory=dict)
    # Direct single-field fan value (Truma CP+) -- alternative to auto+speed.
    fan_value_path: str | None = None
    fan_value_to_mode: dict[int, str] = field(default_factory=dict)
    fan_mode_to_value: dict[str, int] = field(default_factory=dict)
    # Dynamic fan-mode filtering (availability + per-hvac-mode).
    fan_mode_av_flags: dict[str, str] = field(default_factory=dict)
    fan_mode_hvac_only: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Dynamic hvac-mode filtering (availability flags). Empty -> static list.
    hvac_mode_av_flags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SwitchSpec:
    """Spec for a writable ``switch`` entity on a Connect device.

    Distinct from the native Switch capabilities (Type 2/11): this is a boolean
    toggle inside a Connect device's ``dev_data`` (e.g. Airtronic ``eco``).
    Read decodes the 0/1 flag; write publishes ``{write_key: 0/1}`` (plus any
    :attr:`extra_command_fields`) via the Connect control plumbing.

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        path: ``dev_data`` 0/1 leaf to read.
        write_key: ``dev_data`` key written on toggle (usually the leaf name).
        device_class: Optional HA ``SwitchDeviceClass`` string.
        entity_category: Optional HA ``EntityCategory`` string.
        available_path: optional ``dev_data`` 0/1 leaf gating availability
            (Dometic Absorber accessory switches gated by ``fan_one_av`` etc.).
            The entity reports unavailable when this reads falsy; ``None`` means
            always available.
        extra_command_fields: Static extra ``dev_data`` keys sent with the
            command. May be empty.
    """

    key: str
    name: str
    path: str
    write_key: str
    device_class: str | None = None
    entity_category: str | None = None
    available_path: str | None = None
    # Optional dev_stat path locking control on 6/7; see ClimateSpec.lock_path.
    lock_path: str | None = None
    extra_command_fields: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class LightSpec:
    """Spec for a ``light`` entity on a Connect device.

    Two shapes: a plain on/off light (only ``state_path``/``state_write_path``),
    or a dimmable light that additionally carries a discrete brightness level
    (``level_path``/``level_write_path`` + ``level_max``). Dometic FreshJet has
    one of each: ``ex_light`` (on/off) and ``in_light`` + ``in_light_lv``
    (off/half/full -> levels 0..2).

    Brightness mapping: HA brightness is 0..255. A read maps the raw level L to
    ``round(255 * L / level_max)``; a write maps HA brightness B to the nearest
    level ``round(B / 255 * level_max)`` and sets the on/off field to (level>0
    or, when no level field, the requested on-state). Devices report on/off
    separately, so an explicit ``state_path`` is always present.

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        state_path: ``dev_data`` 0/1 leaf for on/off (dotted).
        state_write_path: ``dev_data`` path written for on/off (dotted).
        level_path: optional ``dev_data`` discrete-level leaf (dotted); None for
            a plain on/off light.
        level_write_path: ``dev_data`` path written for the level (dotted).
        level_max: the maximum raw level value (FreshJet internal light: 2).
        level_min: the minimum *on* raw level value (Truma CP+ interior light:
            20, app range 20..100). A write of a non-zero brightness is clamped
            into ``[level_min, level_max]`` so the device never receives an
            out-of-range dim level; reading is unchanged. Default 0 (FreshJet et
            al. unchanged: their on-floor stays the existing max(1, ...)).
        available_path: optional ``dev_data`` 0/1 leaf gating availability
            (FreshJet ``in_light_av``); the entity reports unavailable when 0.
        entity_category: Optional HA ``EntityCategory`` string.
        extra_command_fields: Static extra ``dev_data`` keys (dotted) sent with
            the command.
    """

    key: str
    name: str
    state_path: str
    state_write_path: str
    level_path: str | None = None
    level_write_path: str | None = None
    level_max: int = 0
    level_min: int = 0
    available_path: str | None = None
    # Optional dev_stat path locking control on 6/7; see ClimateSpec.lock_path.
    lock_path: str | None = None
    entity_category: str | None = None
    extra_command_fields: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class NumberSpec:
    """Spec for a writable ``number`` entity on a Connect device.

    A continuous/stepped setpoint in ``dev_data`` (e.g. Truma water
    ``comb_water.target_temp``). Read via the dotted path; write publishes the
    value at ``write_path`` (dotted -> nested via set_dev_data_path).

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        path: ``dev_data`` leaf to read (dotted).
        write_path: ``dev_data`` path written on set (dotted; usually == path).
        min_value / max_value: Bounds.
        step: Step granularity.
        unit: Optional native unit of measurement.
        device_class: Optional HA ``NumberDeviceClass`` string.
        mode: HA NumberMode string (``"auto"`` / ``"box"`` / ``"slider"``).
        entity_category: Optional HA ``EntityCategory`` string.
        as_int: Send the value as an int (firmware int16 fields like
            target_temp) rather than float. HA numbers are floats internally.
        available_path: optional ``dev_data`` 0/1 leaf gating availability
            (Alde ``fuel_av`` for the fuel-power setpoint -- the app hides the
            slider on installs without fuel). Presence-gated like switches: the
            discovery listener removes the entity outright when the flag is
            falsy; ``None`` means always present.
        extra_command_fields: Static extra ``dev_data`` keys (dotted) sent with
            the command. Values may be int or nested via the path syntax.
    """

    key: str
    name: str
    path: str
    write_path: str
    min_value: float
    max_value: float
    step: float
    unit: str | None = None
    device_class: str | None = None
    mode: str = "auto"
    entity_category: str | None = None
    as_int: bool = False
    available_path: str | None = None
    # Optional dev_stat path locking control on 6/7; see ClimateSpec.lock_path.
    lock_path: str | None = None
    extra_command_fields: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SelectSpec:
    """Spec for a ``select`` entity backed by a discrete ``dev_data`` value.

    Maps a set of user-facing option strings to/from the raw ``dev_data``
    values (e.g. Truma ``energy_sel`` 1/2/3 <-> gas/electric/both, or water
    ``comb_water.p_lim`` 900/1800 <-> the two power options). Write publishes
    :attr:`option_to_value` at ``write_path``; read looks the raw value up in
    the inverse map :attr:`value_to_option`, which is **derived** from
    ``option_to_value`` in ``__post_init__`` -- there is a single source of
    truth, so a forward/inverse typo is impossible across the many upcoming
    selects (review N5). ``__post_init__`` also asserts that ``options`` exactly
    matches the ``option_to_value`` keys and that the values are unique (a
    duplicate value would make the inverse map ambiguous).

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name.
        path: ``dev_data`` leaf to read (dotted).
        write_path: ``dev_data`` path written on select (dotted; usually == path).
        options: Ordered option strings exposed to HA.
        option_to_value: option string -> raw ``dev_data`` value written.
        value_to_option: derived inverse (raw value -> option); do not pass.
        entity_category: Optional HA ``EntityCategory`` string.
        available_path: optional ``dev_data`` 0/1 leaf gating availability
            (Truma Combi ``energyEnabled`` for the energy-source / power-limit
            pair -- the app hides the whole Energy section without it).
            Presence-gated like switches: the discovery listener removes the
            entity outright when the flag is falsy; ``None`` means always
            present.
        option_av_flags: option string -> ``dev_data`` 0/1 leaf that must be
            truthy for that option to be offered (Alde water ``auto`` needs
            ``water_auto_av``). The currently selected option is always offered
            regardless of its flag (mirrors the app: ``waterAutoAvailable ||
            selected``). Absent entry -> always offered.
        extra_command_fields: Static extra ``dev_data`` keys (dotted) sent with
            the command.
    """

    key: str
    name: str
    path: str
    write_path: str
    options: tuple[str, ...]
    option_to_value: dict[str, int]
    entity_category: str | None = None
    available_path: str | None = None
    option_av_flags: dict[str, str] = field(default_factory=dict)
    # Optional dev_stat path locking control on 6/7; see ClimateSpec.lock_path.
    lock_path: str | None = None
    extra_command_fields: dict[str, int] = field(default_factory=dict)
    # Derived in __post_init__; not a constructor argument.
    value_to_option: dict[int, str] = field(init=False)

    def __post_init__(self) -> None:
        """Derive + validate the value<->option mapping (review N5)."""
        if set(self.options) != set(self.option_to_value):
            raise ValueError(
                f"SelectSpec {self.key!r}: options {self.options} do not match "
                f"option_to_value keys {tuple(self.option_to_value)}"
            )
        values = list(self.option_to_value.values())
        if len(set(values)) != len(values):
            raise ValueError(f"SelectSpec {self.key!r}: duplicate raw value in option_to_value {self.option_to_value}")
        if not set(self.option_av_flags) <= set(self.options):
            raise ValueError(
                f"SelectSpec {self.key!r}: option_av_flags keys {tuple(self.option_av_flags)} "
                f"not a subset of options {self.options}"
            )
        # frozen dataclass -> bypass the immutability guard to set the derived field.
        object.__setattr__(self, "value_to_option", {v: k for k, v in self.option_to_value.items()})


@dataclass(frozen=True, kw_only=True)
class ControlLockSpec:
    """Spec for a diagnostic ``binary_sensor`` reporting the control-lock state.

    Some Connect devices (Alde, Truma, Eberspächer Airtronic3, Thitronik) refuse
    remote commands while their own panel is in control: the firmware's
    ``dev_stat`` enum reads ``6`` (main panel in use) or ``7`` (main panel +
    error). The app greys out *all* controls in that state; this integration
    instead keeps the entities visible but rejects commands (see
    :func:`..control_lock_reason`) and surfaces the state through one of these
    read-only binary sensors (``on`` == locked).

    Truma CP+ has two independent sub-devices (heater / air_con), each with its
    own ``dev_stat``, hence a tuple of specs with distinct ``lock_path``s rather
    than a single device-wide flag.

    Attributes:
        key: Stable suffix for ``unique_id`` / ``translation_key``.
        name: Fallback display name (e.g. ``"Control locked"``).
        lock_path: Dotted ``dev_data`` path to the ``dev_stat`` enum to read
            (``"dev_stat"`` or, for CP+, ``"heater.dev_stat"`` /
            ``"air_con.dev_stat"``).
    """

    key: str
    name: str
    lock_path: str


@dataclass(frozen=True, kw_only=True)
class ConnectDeviceDescriptor:
    """Everything one Connect ``device`` code produces, across all platforms.

    A descriptor is the single source of truth consulted by every Connect-aware
    platform's ``async_setup_entry``. Each platform reads only the spec list it
    owns (sensor.py -> :attr:`sensors`, binary_sensor.py ->
    :attr:`binary_sensors` + :attr:`array_binary_sensors`, etc.).

    Later phases extend this with ``climate``/``select``/``number``/``switch``
    fields as further ``tuple[...] = ()`` defaults; existing devices keep
    working untouched because the new platforms simply find an empty tuple.

    Attributes:
        device: The ``ConnectDevice`` code this descriptor models (e.g. 1024).
        name: Human-readable device label, used as the entity-name prefix until
            an app config name is available.
        sensors: Read-only ``sensor`` specs.
        enum_sensors: Translated enum ``sensor`` specs (Thitronik alarm_reason).
        binary_sensors: Read-only ``binary_sensor`` specs at fixed paths.
        array_binary_sensors: Per-element ``binary_sensor`` families.
        alarm_panel: Optional ``alarm_control_panel`` spec (``None`` if the
            device is not an alarm).
        lock: Optional ``lock`` spec (``None`` if the device has no lock).
        buttons: One-shot command ``button`` specs (Thitronik panic alarm).
        climates: ``climate`` specs (heaters / AC). Usually 0 or 1, but a tuple
            so a device with multiple climate zones (Alde, Truma CP+) fits.
        switches: Writable ``switch`` specs (e.g. Airtronic ``eco``).
        numbers: Writable ``number`` specs (e.g. Truma water target temp).
        selects: ``select`` specs (e.g. Truma energy source, power limit).
        lights: ``light`` specs (e.g. Dometic FreshJet interior/exterior light).
        array_sensors: Per-element ``sensor`` families (e.g. EcoFlow ``pwr[5]``).
        control_lock_path: Optional device-wide ``dev_stat`` path whose 6/7
            values lock *all* this device's writable entities (Alde, Truma
            Combi, Airtronic3, Thitronik use ``"dev_stat"``). A spec-level
            ``lock_path`` overrides it (Truma CP+ per sub-device). ``None`` (the
            default, e.g. Dometic) means the device never locks on the panel.
        control_locks: Diagnostic ``binary_sensor`` specs reporting the lock
            state (``on`` == locked). One per device, or two for Truma CP+.
    """

    device: int
    name: str
    sensors: tuple[SensorSpec, ...] = ()
    enum_sensors: tuple[EnumSensorSpec, ...] = ()
    binary_sensors: tuple[BinarySensorSpec, ...] = ()
    array_binary_sensors: tuple[ArrayBinarySensorSpec, ...] = ()
    alarm_panel: AlarmPanelSpec | None = None
    lock: LockSpec | None = None
    buttons: tuple[ButtonSpec, ...] = ()
    climates: tuple[ClimateSpec, ...] = ()
    switches: tuple[SwitchSpec, ...] = ()
    numbers: tuple[NumberSpec, ...] = ()
    selects: tuple[SelectSpec, ...] = ()
    lights: tuple[LightSpec, ...] = ()
    array_sensors: tuple[ArraySensorSpec, ...] = ()
    control_lock_path: str | None = None
    control_locks: tuple[ControlLockSpec, ...] = ()

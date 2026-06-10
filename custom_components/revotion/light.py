"""Light platform for the Revotion integration.

Implements Ambient/RGB (Type 4) light entity with color control, brightness
mapping, and MQTT command publishing to ctr_data. The color mode follows the
app's ambient hardware config (``typ`` in the capability config): rgb/rgbW get
the RGBW picker, white is a plain dimmer, coldWarmWhite a two-channel white
mix exposed as COLOR_TEMP.

State updates use optimistic mode: after sending a command, the entity
immediately assumes the new state to prevent color picker jumping over
slow LTE-M connections. The optimistic state is kept until a coordinator
update *confirms* the commanded values (or it expires after 60 s) -- the
coordinator fires for every incoming MQTT message, so clearing on just any
update would bounce the UI back while the echo is still in flight.

Brightness mapping (D-17):
  HA -> Revotion: revotion_bri = round(ha_brightness * 100 / 255)
  Revotion -> HA: ha_brightness = round(revotion_bri * 255 / 100)
"""

from __future__ import annotations

import json
import logging
import time
from enum import IntEnum
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGBW_COLOR,
    LightEntity,
)
from homeassistant.components.light.const import ColorMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import (
    get_descriptor,
    has_descriptor,
    int01_to_bool,
    is_path_available,
    read_dev_data_path,
    reconcile_gated_entities,
    resolve_connect_device,
)
from .connect.control import ConnectCommandMixin, set_dev_data_path
from .connect.descriptors import LightSpec
from .connect.entity import resolve_entity_category
from .const import (
    CONF_BRAIN_MAC,
    DOMAIN,
    TOPIC_CONTROL_DATA,
    CapabilityType,
    RevotionConfigEntry,
)
from .coordinator import RevotionCoordinator
from .models import (
    RevotionCapabilityMixin,
    format_mac_for_display,
    format_timer_attributes,
    normalize_mac,
    register_node_device,
)
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


class AmbientType(IntEnum):
    """App-side ambient hardware configs (revotion app, ambient_type.dart).

    Stored as ``typ`` in the capability config; reaches the integration via
    REST /brain/sync (flattened into ``config.data``, value arrives as a
    string, e.g. ``"2"``) and via the flat MQTT /config payload.
    """

    RGB = 0
    RGBW = 1
    WHITE = 2
    COLD_WARM_WHITE = 3


# coldWarmWhite has no real Kelvin scale -- the hardware mixes a warm and a
# cold channel linearly (app slider: rval = warm share 0..255, bval = cold
# share = 255 - rval, gval = 0). Map that mix onto a typical CCT strip range
# so HA renders a temperature slider.
COLD_WARM_MIN_KELVIN = 2700  # warmest: rval=255
COLD_WARM_MAX_KELVIN = 6500  # coldest: rval=0

# The Ambient light has no command lock/timeout machinery (unlike switch.py /
# RevotionCommandMixin), so an optimistic state whose echo never arrives needs
# a time-based fallback: it is dropped on the first coordinator update after
# this many seconds. Mirrors COMMAND_TIMEOUT_S elsewhere.
OPTIMISTIC_MAX_AGE_S = 60

# Confirmation tolerance for the brightness round-trip: HA 0-255 is converted
# to Revotion 0-100 and back, so the echo may differ by a rounding step.
_BRIGHTNESS_CONFIRM_TOLERANCE = 3


def _kelvin_to_rval(kelvin: int) -> int:
    """Map a kelvin value onto the warm-channel share (rval 0..255)."""
    kelvin = min(COLD_WARM_MAX_KELVIN, max(COLD_WARM_MIN_KELVIN, kelvin))
    warm_frac = (COLD_WARM_MAX_KELVIN - kelvin) / (COLD_WARM_MAX_KELVIN - COLD_WARM_MIN_KELVIN)
    return round(255 * warm_frac)


def _rval_to_kelvin(rval: int) -> int:
    """Map the warm-channel share (rval 0..255) back onto kelvin."""
    warm_frac = min(255, max(0, rval)) / 255
    return round(COLD_WARM_MAX_KELVIN - warm_frac * (COLD_WARM_MAX_KELVIN - COLD_WARM_MIN_KELVIN))


class RevotionAmbientLight(RevotionCapabilityMixin, CoordinatorEntity[RevotionCoordinator], LightEntity):
    """Light entity for Ambient (Type 4) and Multiwhite (Type 13) capabilities.

    The color mode follows the app's ambient hardware config (``typ``):
    rgb(0)/rgbW(1) expose the RGBW picker, white(2) is a plain BRIGHTNESS
    dimmer, coldWarmWhite(3) a two-channel white mix exposed as COLOR_TEMP.
    Multiwhite channels are forced white-only dimmers (the app forces
    AmbientType.white for AMB_3CH) — same wire payload, just no color picker.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
        channel: int | None = None,
        white_only: bool = False,
    ) -> None:
        """Initialize the light entity.

        ``channel`` (1-based) distinguishes the per-channel lights of a
        Multiwhite node (Type 13): each AMB_3CH cap is byte-identical to a
        normal Ambient light (Type 4) but carries its own cap_index. When set
        and no config_name is given, the entity is named "Channel {channel}".
        AMBIENT (Type 4) passes channel=None for the unchanged default name.

        ``white_only`` forces ColorMode.BRIGHTNESS regardless of config: the
        Multiwhite hardware only dims white (app: AmbientType.white). Commands
        keep sending the full ambient payload with the current color values
        untouched -- exactly what the app's white slider does. Plain Ambient
        caps instead derive their color mode from the configured ``typ``.
        """
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._mqtt_client = mqtt_client
        self._white_only = white_only
        self._attr_min_color_temp_kelvin = COLD_WARM_MIN_KELVIN
        self._attr_max_color_temp_kelvin = COLD_WARM_MAX_KELVIN
        self._apply_color_mode()
        self._optimistic_state: bool | None = None
        self._optimistic_brightness: int | None = None
        self._optimistic_rgbw: tuple[int, int, int, int] | None = None
        self._optimistic_since: float | None = None
        if config_name:
            self._attr_name = config_name
        elif channel is not None:
            self._attr_translation_key = "ambient_light_channel"
            self._attr_translation_placeholders = {"channel": str(channel)}
        else:
            self._attr_translation_key = "ambient_light"
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}

    def _config_ambient_type(self) -> int | None:
        """Return the app-configured AmbientType (``typ`` in config.data).

        None when the config has not arrived (yet) or carries no parseable
        value. The REST sync delivers the value as a string (e.g. ``"2"``).
        """
        cap = self._find_capability()
        if cap is None:
            return None
        raw = cap.config.data.get("typ")
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def _apply_color_mode(self) -> None:
        """Derive the color mode from the configured ambient type.

        Re-run on every coordinator update: the config may arrive after entity
        creation (the REST sync is best-effort) or change when the user
        reconfigures the capability in the app. HA syncs a changed
        supported_color_modes set to the entity registry on the next state
        write.
        """
        if self._white_only:
            mode = ColorMode.BRIGHTNESS
        else:
            match self._config_ambient_type():
                case AmbientType.WHITE:
                    mode = ColorMode.BRIGHTNESS
                case AmbientType.COLD_WARM_WHITE:
                    mode = ColorMode.COLOR_TEMP
                case _:  # rgb(0), rgbW(1), unknown or config not (yet) known
                    mode = ColorMode.RGBW
        self._attr_color_mode = mode
        self._attr_supported_color_modes = {mode}

    def _handle_coordinator_update(self) -> None:
        """Re-derive color mode; clear optimistic state once the echo confirms.

        The coordinator fires for every incoming MQTT message, so clearing
        unconditionally would bounce the UI back while the echo is still in
        flight over LTE-M. Without a command lock/timeout (unlike switch.py)
        the safety net is age-based: a stale optimistic state expires after
        OPTIMISTIC_MAX_AGE_S.
        """
        self._apply_color_mode()
        if self._optimistic_confirmed() or self._optimistic_expired():
            self._optimistic_state = None
            self._optimistic_brightness = None
            self._optimistic_rgbw = None
            self._optimistic_since = None
        super()._handle_coordinator_update()

    def _optimistic_expired(self) -> bool:
        """Return True when the optimistic state outlived its echo window."""
        return self._optimistic_since is not None and (time.monotonic() - self._optimistic_since) > OPTIMISTIC_MAX_AGE_S

    def _optimistic_confirmed(self) -> bool:
        """Return True once capability data matches every optimistic value."""
        if self._optimistic_state is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        state = cap.data.get("state")
        if state is None or bool(state) != self._optimistic_state:
            return False
        if self._optimistic_brightness is not None:
            bri = cap.data.get("bri")
            if bri is None or abs(round(bri * 255 / 100) - self._optimistic_brightness) > _BRIGHTNESS_CONFIRM_TOLERANCE:
                return False
        if self._optimistic_rgbw is not None:
            if cap.data.get("rval") is None:
                return False
            real = (
                cap.data.get("rval", 0),
                cap.data.get("gval", 0),
                cap.data.get("bval", 0),
                cap.data.get("wval", 0),
            )
            if real != self._optimistic_rgbw:
                return False
        return True

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the entity's color mode (RGBW, or BRIGHTNESS for white-only)."""
        cap = self._find_capability()
        if cap is None:
            return None
        return self._attr_color_mode

    @property
    def is_on(self) -> bool | None:
        """Return True if light is on (state == 1)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        cap = self._find_capability()
        if cap is None:
            return None
        state = cap.data.get("state")
        if state is None:
            return None
        return bool(state)

    @property
    def brightness(self) -> int | None:
        """Return HA brightness (0-255) from Revotion bri (0-100) per D-17."""
        if self._optimistic_brightness is not None:
            return self._optimistic_brightness
        cap = self._find_capability()
        if cap is None:
            return None
        bri = cap.data.get("bri")
        if bri is None:
            return None
        return round(bri * 255 / 100)

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """Return (rval, gval, bval, wval) tuple (RGBW mode only)."""
        if self._attr_color_mode != ColorMode.RGBW:
            return None
        if self._optimistic_rgbw is not None:
            return self._optimistic_rgbw
        cap = self._find_capability()
        if cap is None:
            return None
        data = cap.data
        rval = data.get("rval")
        if rval is None:
            return None
        return (
            data.get("rval", 0),
            data.get("gval", 0),
            data.get("bval", 0),
            data.get("wval", 0),
        )

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the cold/warm mix as kelvin (coldWarmWhite only).

        The hardware stores the mix in the color channels (rval = warm share,
        bval = cold share); kelvin is derived from rval alone, exactly like
        the app's temperature slider reconstructs its position.
        """
        if self._attr_color_mode != ColorMode.COLOR_TEMP:
            return None
        if self._optimistic_rgbw is not None:
            return _rval_to_kelvin(self._optimistic_rgbw[0])
        cap = self._find_capability()
        if cap is None:
            return None
        rval = cap.data.get("rval")
        if rval is None:
            return None
        return _rval_to_kelvin(rval)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes: timer_state, timer_remaining (D-15)."""
        cap = self._find_capability()
        if cap is None:
            return {}

        timer = cap.data.get("timer")
        return format_timer_attributes(timer)

    async def _publish_command(self, payload: dict[str, Any]) -> None:
        """Publish command to MQTT ctr_data topic.

        Raises if the Brain is on LTE-M (writes are WiFi-only, SIM cost guard).
        """
        self.coordinator.assert_commands_allowed()
        topic = TOPIC_CONTROL_DATA.format(mac=self._brain_mac)
        await self._mqtt_client.async_publish(topic, json.dumps(payload))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light with optional brightness, RGBW color or kelvin.

        Uses current capability data as defaults for unspecified kwargs.
        Brightness converts HA 0-255 to Revotion 0-100 (D-17).
        """
        cap = self._find_capability()
        current_data = cap.data if cap else {}

        payload = self._build_base_payload()
        payload["state"] = 1

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            # coldWarmWhite: kelvin -> warm/cold channel mix, mirroring the
            # app's slider (rval = warm share, bval = complement, gval = 0).
            rval = _kelvin_to_rval(kwargs[ATTR_COLOR_TEMP_KELVIN])
            payload["rval"] = rval
            payload["gval"] = 0
            payload["bval"] = 255 - rval
            payload["wval"] = current_data.get("wval", 0)
        elif ATTR_RGBW_COLOR in kwargs:
            r, g, b, w = kwargs[ATTR_RGBW_COLOR]
            payload["rval"] = r
            payload["gval"] = g
            payload["bval"] = b
            payload["wval"] = w
        else:
            payload["rval"] = current_data.get("rval", 0)
            payload["gval"] = current_data.get("gval", 0)
            payload["bval"] = current_data.get("bval", 0)
            payload["wval"] = current_data.get("wval", 0)

        # Brightness: HA 0-255 -> Revotion 0-100 (D-17)
        if ATTR_BRIGHTNESS in kwargs:
            payload["bri"] = round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
        else:
            payload["bri"] = current_data.get("bri", 0)

        await self._publish_command(payload)

        # Set optimistic state
        self._optimistic_state = True
        self._optimistic_rgbw = (payload["rval"], payload["gval"], payload["bval"], payload["wval"])
        self._optimistic_brightness = round(payload["bri"] * 255 / 100) if payload["bri"] else 0
        self._optimistic_since = time.monotonic()
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light by publishing state=0 with current RGBW/bri."""
        cap = self._find_capability()
        current_data = cap.data if cap else {}

        payload = self._build_base_payload()
        payload["state"] = 0
        payload["rval"] = current_data.get("rval", 0)
        payload["gval"] = current_data.get("gval", 0)
        payload["bval"] = current_data.get("bval", 0)
        payload["wval"] = current_data.get("wval", 0)
        payload["bri"] = current_data.get("bri", 0)

        await self._publish_command(payload)

        self._optimistic_state = False
        self._optimistic_since = time.monotonic()
        if self.hass is not None:
            self.async_write_ha_state()


class RevotionConnectLight(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    LightEntity,
):
    """Descriptor-driven light on a Connect device (e.g. Dometic FreshJet).

    Distinct from the native Ambient (Type 4) RGBW light above: a Connect light
    is either plain on/off (``ColorMode.ONOFF``) or a coarse dimmable with a
    discrete level field (``ColorMode.BRIGHTNESS``). HA brightness 0..255 maps
    to/from the raw level 0..``level_max``. Writes publish on ``ctr_data`` with
    optimistic state (LTE-M round-trip up to 5 s), cleared on the MQTT echo.
    """

    _attr_has_entity_name = True
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        device_code: int,
        spec: LightSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize a descriptor-driven Connect light."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_state: bool | None = None
        self._optimistic_brightness: int | None = None
        self._dimmable = spec.level_path is not None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name
        self._attr_entity_category = resolve_entity_category(spec.entity_category)
        color_mode = ColorMode.BRIGHTNESS if self._dimmable else ColorMode.ONOFF
        self._attr_color_mode = color_mode
        self._attr_supported_color_modes = {color_mode}

    @property
    def available(self) -> bool:
        """Return True if the capability exists and (if gated) is available.

        ``available_path`` (FreshJet ``in_light_av``, Truma ``air_con.light_av``).
        These are *presence-gated*: the discovery listener removes the light
        outright when the flag is falsy (connect/discovery.py), so this is mainly
        a safety net for the brief window before removal. A missing flag is
        treated as available (shared is_path_available semantics).
        """
        cap = self._find_capability()
        if not (super().available and self._node_reachable() and cap is not None):
            return False
        return is_path_available(cap, self._spec.available_path)

    @property
    def is_on(self) -> bool | None:
        """Return True if the light is on (optimistic value wins until echo)."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        cap = self._find_capability()
        if cap is None:
            return None
        return int01_to_bool(read_dev_data_path(cap, self._spec.state_path))

    @property
    def brightness(self) -> int | None:
        """Return HA brightness 0..255 from the raw level 0..level_max."""
        if not self._dimmable:
            return None
        if self._optimistic_brightness is not None:
            return self._optimistic_brightness
        cap = self._find_capability()
        if cap is None:
            return None
        level = read_dev_data_path(cap, self._spec.level_path)
        if not isinstance(level, int) or self._spec.level_max <= 0:
            return None
        return round(255 * level / self._spec.level_max)

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumptions on command timeout."""
        self._optimistic_state = None
        self._optimistic_brightness = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once dev_data matches the optimistic on/off + level.

        The commanded level is the *clamped* one (level_min/level_max), so the
        optimistic HA brightness is mapped through the same clamp before
        comparing -- e.g. brightness 10 on a level_min=20 device echoes level
        20, not the raw conversion of 10.
        """
        if self._optimistic_state is None and self._optimistic_brightness is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        if self._optimistic_state is not None:
            real_on = int01_to_bool(read_dev_data_path(cap, self._spec.state_path))
            if real_on is None or real_on != self._optimistic_state:
                return False
        if self._dimmable and self._optimistic_brightness is not None:
            level = read_dev_data_path(cap, self._spec.level_path)
            if not isinstance(level, int):
                return False
            expected = 0 if self._optimistic_brightness == 0 else self._clamped_level(self._optimistic_brightness)
            if level != expected:
                return False
        return True

    def _clamped_level(self, brightness: int) -> int:
        """Map HA brightness 0..255 into the device's on-range [level_min, level_max].

        level_min defaults to 0, so devices without a floor keep the
        max(1, ...) behaviour; Truma CP+ (level_min=20) never receives a
        sub-20 dim level. Shared by the command builder and the echo
        confirmation so both sides agree on the wire value.
        """
        raw = round(brightness / 255 * self._spec.level_max)
        return min(self._spec.level_max, max(self._spec.level_min, 1, raw))

    def _build_light_command(self, *, on: bool, brightness: int | None) -> dict[str, Any]:
        """Build the control dev_data for the light.

        Plain on/off: writes the state field. Dimmable: writes the level
        (mapped from HA brightness) and the on/off state; turning off writes
        level 0. When turning on without a brightness kwarg, the level is left
        at its current value unless the light is currently off (then full).
        """
        dev_data: dict[str, Any] = {}
        if self._dimmable:
            assert self._spec.level_write_path is not None
            if not on:
                level = 0
            elif brightness is not None:
                level = self._clamped_level(brightness)
            else:
                cap = self._find_capability()
                current = read_dev_data_path(cap, self._spec.level_path) if cap is not None else None
                level = current if isinstance(current, int) and current > 0 else self._spec.level_max
            set_dev_data_path(dev_data, self._spec.level_write_path, level)
            set_dev_data_path(dev_data, self._spec.state_write_path, 1 if level > 0 else 0)
        else:
            set_dev_data_path(dev_data, self._spec.state_write_path, 1 if on else 0)
        for path, extra in self._spec.extra_command_fields.items():
            set_dev_data_path(dev_data, path, extra)
        return dev_data

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on (with optional brightness for dimmable lights)."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        await self._publish_connect_command(self._build_light_command(on=True, brightness=brightness))
        self._optimistic_state = True
        if self._dimmable and brightness is not None:
            self._optimistic_brightness = brightness
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        await self._publish_connect_command(self._build_light_command(on=False, brightness=None))
        self._optimistic_state = False
        if self._dimmable:
            self._optimistic_brightness = 0
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion light entities with dynamic discovery support.

    Creates light entities for Ambient/RGB (Type 4) capabilities, plus
    descriptor-driven Connect lights (Type 12, deferred). Native Ambient lights
    are untouched by the Connect branch.
    """
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    brain_norm = normalize_mac(brain_mac)
    known_node_macs: set[str] = set()

    def _check_device() -> None:
        """Check for new nodes and add light entities dynamically."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        new_macs = current_macs - known_node_macs
        if not new_macs:
            return
        known_node_macs.update(new_macs)

        entities: list[RevotionAmbientLight] = []
        for node in coordinator.data.nodes:
            if normalize_mac(node.mac_address) not in new_macs:
                continue
            register_node_device(hass, entry, node, brain_mac)

            for capability in node.capabilities:
                match capability.capability_type:
                    case CapabilityType.AMBIENT:
                        entities.append(
                            RevotionAmbientLight(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                mqtt_client=mqtt_client,
                                config_name=capability.config.name,
                            )
                        )
                    case CapabilityType.AMB_3CH:
                        # Multiwhite (Type 13): N per-channel caps, each
                        # byte-identical to an Ambient light on the wire, but
                        # the hardware only dims white (app forces
                        # AmbientType.white) -> brightness-only, no color
                        # picker. channel is 1-based for "Channel {channel}".
                        entities.append(
                            RevotionAmbientLight(
                                coordinator=coordinator,
                                brain_mac=brain_mac,
                                node_mac=node.mac_address,
                                cap_index=capability.capability_index,
                                mqtt_client=mqtt_client,
                                config_name=capability.config.name,
                                channel=capability.capability_index + 1,
                                white_only=True,
                            )
                        )

        if entities:
            async_add_entities(entities)

    # Connect (Type 12) lights use deferred discovery (descriptor + dev_data
    # only known after the first /data message) and are presence-gated: each
    # LightSpec's available_path (FreshJet in_light_av, Truma air_con.light_av)
    # decides whether the light should exist *now*, so the listener adds it
    # when the flag turns on and removes it (live + registry) when it turns off.
    # Tracked at (node, cap, key).
    connect_entities: dict[tuple[str, int, str], RevotionConnectLight] = {}

    def _make_connect_light(node, capability, device_code, spec):
        """Bind a per-spec factory (own scope avoids late-binding in the loop)."""

        def factory() -> RevotionConnectLight:
            register_node_device(hass, entry, node, brain_mac)
            return RevotionConnectLight(
                coordinator=coordinator,
                brain_mac=brain_mac,
                node_mac=node.mac_address,
                cap_index=capability.capability_index,
                device_code=device_code,
                spec=spec,
                mqtt_client=mqtt_client,
                config_name=capability.config.name,
            )

        return factory

    def _check_connect() -> None:
        """Reconcile presence-gated Connect lights once a device code resolves.

        Native Ambient (Type 4) lights above are untouched; devices without a
        descriptor never reach this branch (their dev_data is mirrored as
        read-only sensors).
        """
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        candidates = []
        for node in coordinator.data.nodes:
            node_mac = normalize_mac(node.mac_address)
            for capability in node.capabilities:
                if capability.capability_type != CapabilityType.CONNECT:
                    continue
                device_code = resolve_connect_device(capability)
                if device_code is None or not has_descriptor(device_code):
                    continue
                descriptor = get_descriptor(device_code)
                assert descriptor is not None
                for spec in descriptor.lights:
                    key = (node_mac, capability.capability_index, spec.key)
                    present = is_path_available(capability, spec.available_path)
                    unique_id = f"revotion_{brain_norm}_{node_mac}_{capability.capability_index}_{spec.key}"
                    candidates.append(
                        (key, present, unique_id, _make_connect_light(node, capability, device_code, spec))
                    )

        reconcile_gated_entities(
            hass=hass,
            entity_domain="light",
            entities=connect_entities,
            current_macs=current_macs,
            candidates=candidates,
            async_add_entities=async_add_entities,
        )

    def _on_update() -> None:
        """Coordinator listener: run native and Connect light discovery."""
        _check_device()
        _check_connect()

    _on_update()  # Initial entity creation
    entry.async_on_unload(coordinator.async_add_listener(_on_update))

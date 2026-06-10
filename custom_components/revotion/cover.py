"""Cover platform for the Revotion integration.

Implements the Two-Way (Type 11) capability as a HA cover. The firmware exposes
a Two-Way as **two independent, mutually-exclusive outputs** -- ``state_l`` (one
travel direction) and ``state_r`` (the other) plus a ``fuse`` flag -- with three
valid states: left active ``(1,0)``, right active ``(0,1)``, and both off
``(0,0)``. The Revotion app renders this as a two-button widget (left/right,
exclusive); the natural HA equivalent is a cover with OPEN / CLOSE / STOP:

    OPEN  -> state_l=1, state_r=0   (travel one way)
    CLOSE -> state_l=0, state_r=1   (travel the other way)
    STOP  -> state_l=0, state_r=0   (both off)

The earlier modelling as a single on/off switch (``RevotionTwoWaySwitch``) only
ever reached two of the three states and hid the second direction entirely.

Commands are issued discretely (one MQTT publish each), identical for the
``sw_*_type`` switch and push-button modes: over the current LTE-M transport
(round-trips up to ~5 s) holding a momentary button is impractical, so OPEN
starts travel and the user presses STOP to halt -- the standard HA pattern for
position-less covers.

State is **assumed** (``assumed_state = True``): the device reports only the live
travel direction (state_l/state_r), never an open/closed position, so HA renders
the open/stop/close buttons rather than a position slider. Optimistic direction
is set on send and cleared on the real MQTT echo (or after the 60 s timeout) to
avoid UI bounce-back over the slow link -- see :class:`..models.RevotionCommandMixin`.

``device_class`` is derived from the app's ``app_sw_type`` word-pair so genuine
awnings/windows/shutters get the right icon; generic pairs (On/Off, Start/Stop,
...) stay a plain cover.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BRAIN_MAC,
    DOMAIN,
    CapabilityType,
    RevotionConfigEntry,
)
from .coordinator import RevotionCoordinator
from .models import (
    RevotionCapabilityMixin,
    RevotionCommandMixin,
    format_mac_for_display,
    normalize_mac,
    register_node_device,
)
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)

# Optimistic travel-direction sentinels (host-owned optimistic value).
_DIR_OPENING = "opening"
_DIR_CLOSING = "closing"
_DIR_STOPPED = "stopped"

# app_sw_type (TwoWayWordPair in the Revotion app) -> cover device_class.
# 0 On/Off, 1 Into/Out, 2 Open/Close, 3 Blow/Suck, 4 Up/Down, 5 Left/Right,
# 6 Start/Stop. Only the position-shaped pairs map to a class; the rest stay a
# generic cover (still rendered with the ▲ ■ ▼ buttons). Cosmetic only --
# refine freely as real device labels are confirmed.
_DEVICE_CLASS_BY_APP_SW_TYPE: dict[int, CoverDeviceClass] = {
    1: CoverDeviceClass.AWNING,  # Into/Out -- awning extends/retracts
    2: CoverDeviceClass.WINDOW,  # Open/Close -- window/awning open/shut
    4: CoverDeviceClass.SHUTTER,  # Up/Down -- shutter raises/lowers
}


def _format_fuse(value: int | None) -> str | None:
    """Map fuse value: 0 -> 'ok', 1 -> 'blown', None -> None."""
    if value is None:
        return None
    return "blown" if value else "ok"


class RevotionTwoWayCover(
    RevotionCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    CoverEntity,
):
    """Two-Way (Type 11) capability rendered as an OPEN/CLOSE/STOP cover."""

    _attr_has_entity_name = True
    # No position feedback -> assumed_state renders the open/stop/close buttons
    # (not a slider) and the optimistic anti-bounce-back is handled by
    # _optimistic_direction winning in the state properties.
    _attr_assumed_state = True
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
        node_mac: str,
        cap_index: int,
        mqtt_client: RevotionMqttClient,
        translation_key: str,
        config_name: str = "",
    ) -> None:
        """Initialize the Two-Way cover entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._mqtt_client = mqtt_client
        self._init_command_state()
        # Host-owned optimistic value: which way we just told it to travel
        # (or that we stopped it), until the MQTT echo lands.
        self._optimistic_direction: str | None = None
        if config_name:
            self._attr_name = config_name
        else:
            self._attr_translation_key = translation_key
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}

    def _handle_coordinator_update(self) -> None:
        """Clear the command lock + optimistic direction once the echo confirms."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic direction (called on command timeout)."""
        self._optimistic_direction = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once state_l/state_r match the optimistic direction."""
        if self._optimistic_direction is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        left = bool(cap.data.get("state_l"))
        right = bool(cap.data.get("state_r"))
        if self._optimistic_direction == _DIR_OPENING:
            return left and not right
        if self._optimistic_direction == _DIR_CLOSING:
            return right and not left
        return not left and not right  # _DIR_STOPPED

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def device_class(self) -> CoverDeviceClass | None:
        """Derive the cover device_class from the app's app_sw_type word-pair."""
        cap = self._find_capability()
        if cap is None:
            return None
        raw = cap.config.data.get("app_sw_type")
        try:
            app_sw_type = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            app_sw_type = 0
        return _DEVICE_CLASS_BY_APP_SW_TYPE.get(app_sw_type)

    @property
    def is_opening(self) -> bool:
        """Return True while travelling in the open direction (state_l)."""
        if self._optimistic_direction is not None:
            return self._optimistic_direction == _DIR_OPENING
        cap = self._find_capability()
        if cap is None:
            return False
        return bool(cap.data.get("state_l"))

    @property
    def is_closing(self) -> bool:
        """Return True while travelling in the close direction (state_r)."""
        if self._optimistic_direction is not None:
            return self._optimistic_direction == _DIR_CLOSING
        cap = self._find_capability()
        if cap is None:
            return False
        return bool(cap.data.get("state_r"))

    @property
    def is_closed(self) -> bool | None:
        """Return None -- position is unknown.

        The device reports only the live travel direction (state_l/state_r),
        never an open/closed position, so we cannot honestly report closed/open.
        Returning None keeps the state truthful ("unknown" when idle); with
        assumed_state the open/stop/close buttons are shown regardless.
        """
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose raw state_l/state_r and the decoded fuse status."""
        cap = self._find_capability()
        if cap is None:
            return {}
        attrs: dict[str, Any] = {}
        data = cap.data
        if "state_l" in data:
            attrs["state_l"] = bool(data["state_l"])
        if "state_r" in data:
            attrs["state_r"] = bool(data["state_r"])
        if "fuse" in data:
            attrs["fuse"] = _format_fuse(data["fuse"])
        return attrs

    def _set_optimistic_direction(self, direction: str) -> None:
        """Set the optimistic travel direction and push to HA if registered."""
        self._optimistic_direction = direction
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open: publish state_l=1, state_r=0."""
        payload = self._build_base_payload()
        payload["state_l"] = 1
        payload["state_r"] = 0
        await self._publish_command(payload)
        self._set_optimistic_direction(_DIR_OPENING)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close: publish state_l=0, state_r=1."""
        payload = self._build_base_payload()
        payload["state_l"] = 0
        payload["state_r"] = 1
        await self._publish_command(payload)
        self._set_optimistic_direction(_DIR_CLOSING)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop: publish state_l=0, state_r=0 (both outputs off)."""
        payload = self._build_base_payload()
        payload["state_l"] = 0
        payload["state_r"] = 0
        await self._publish_command(payload)
        self._set_optimistic_direction(_DIR_STOPPED)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion cover entities with dynamic discovery.

    Creates one :class:`RevotionTwoWayCover` per Two-Way (Type 11) capability.
    Additive discovery mirrors switch.py: a coordinator listener adds covers for
    nodes not seen before (e.g. after a pair event + REST sync).
    """
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known_node_macs: set[str] = set()

    @callback
    def _check_device() -> None:
        """Add cover entities for newly discovered nodes."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        new_macs = current_macs - known_node_macs
        if not new_macs:
            return
        known_node_macs.update(new_macs)

        entities: list[RevotionTwoWayCover] = []
        for node in coordinator.data.nodes:
            if normalize_mac(node.mac_address) not in new_macs:
                continue
            register_node_device(hass, entry, node, brain_mac)

            for capability in node.capabilities:
                if capability.capability_type == CapabilityType.TWO_WAY:
                    entities.append(
                        RevotionTwoWayCover(
                            coordinator=coordinator,
                            brain_mac=brain_mac,
                            node_mac=node.mac_address,
                            cap_index=capability.capability_index,
                            mqtt_client=mqtt_client,
                            translation_key="two_way",
                            config_name=capability.config.name,
                        )
                    )

        if entities:
            async_add_entities(entities)

    _check_device()  # Initial entity creation
    entry.async_on_unload(coordinator.async_add_listener(_check_device))

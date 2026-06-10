"""Device tracker platform for the Revotion integration."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BRAIN_MAC, DOMAIN, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import normalize_mac


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Revotion device tracker entities."""
    coordinator = entry.runtime_data.coordinator
    brain_mac = entry.data[CONF_BRAIN_MAC]
    async_add_entities([RevotionDeviceTracker(coordinator, brain_mac)])


class RevotionDeviceTracker(CoordinatorEntity[RevotionCoordinator], TrackerEntity):
    """GPS device tracker entity for a Revotion Brain."""

    _attr_has_entity_name = True
    _attr_translation_key = "gps_position"
    _attr_entity_category = None

    def __init__(
        self,
        coordinator: RevotionCoordinator,
        brain_mac: str,
    ) -> None:
        """Initialize the GPS device tracker.

        Args:
            coordinator: The Revotion data coordinator.
            brain_mac: Brain MAC address for unique ID and device info.

        """
        super().__init__(coordinator)
        normalized_mac = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{normalized_mac}_gps"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, normalized_mac)},
        }

    @property
    def source_type(self) -> SourceType:
        """Return the source type as GPS."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude from GPS data."""
        if self.coordinator._gps_data is None:
            return None
        return self.coordinator._gps_data.get("Lat")

    @property
    def longitude(self) -> float | None:
        """Return longitude from GPS data."""
        if self.coordinator._gps_data is None:
            return None
        return self.coordinator._gps_data.get("Lon")

    @property
    def location_accuracy(self) -> float:
        """Return raw HDOP as location accuracy."""
        if self.coordinator._gps_data is None:
            return 0
        return float(self.coordinator._gps_data.get("HDOP", 0) or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra GPS attributes: altitude, speed, gps_stale."""
        if self.coordinator._gps_data is None:
            return {}

        attrs: dict[str, Any] = {}

        if "Alt" in self.coordinator._gps_data:
            attrs["altitude"] = self.coordinator._gps_data["Alt"]

        if "Speed" in self.coordinator._gps_data:
            attrs["speed"] = self.coordinator._gps_data["Speed"]

        utc = self.coordinator._gps_data.get("UTC", 0)
        if utc > 0:
            attrs["gps_stale"] = int(time.time()) - utc

        if "COG" in self.coordinator._gps_data:
            attrs["course"] = self.coordinator._gps_data["COG"]

        if "HDOP" in self.coordinator._gps_data:
            attrs["hdop"] = self.coordinator._gps_data["HDOP"]

        return attrs

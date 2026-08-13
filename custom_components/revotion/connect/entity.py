"""Shared HA-enum resolution for descriptor-driven Connect entities.

Descriptor specs (:mod:`.descriptors`) carry ``device_class`` / ``entity_category``
as plain strings so that module stays free of Home Assistant imports. The
platform files turn those strings into the real HA enums. Centralising the
conversion here keeps it in one place across sensor / binary_sensor (and the
climate/select/number platforms that land in Phases 2-5), instead of repeating
``SomeEnum(value) if value is not None else None`` at every call site.

The resolvers are deliberately strict: an unknown string raises ``ValueError``
from the enum constructor at startup, surfacing a descriptor typo immediately
rather than silently dropping the attribute.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.climate import HVACMode
from homeassistant.components.number import NumberDeviceClass, NumberMode
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.const import EntityCategory


def resolve_sensor_device_class(value: str | None) -> SensorDeviceClass | None:
    """Map a descriptor ``device_class`` string to ``SensorDeviceClass``."""
    return SensorDeviceClass(value) if value is not None else None


def resolve_sensor_state_class(value: str | None) -> SensorStateClass | None:
    """Map a descriptor ``state_class`` string to ``SensorStateClass``."""
    return SensorStateClass(value) if value is not None else None


def resolve_binary_sensor_device_class(value: str | None) -> BinarySensorDeviceClass | None:
    """Map a descriptor ``device_class`` string to ``BinarySensorDeviceClass``."""
    return BinarySensorDeviceClass(value) if value is not None else None


def resolve_switch_device_class(value: str | None) -> SwitchDeviceClass | None:
    """Map a descriptor ``device_class`` string to ``SwitchDeviceClass``."""
    return SwitchDeviceClass(value) if value is not None else None


def resolve_entity_category(value: str | None) -> EntityCategory | None:
    """Map a descriptor ``entity_category`` string to ``EntityCategory``."""
    return EntityCategory(value) if value is not None else None


def resolve_hvac_mode(value: str) -> HVACMode:
    """Map a descriptor HVAC-mode string to ``HVACMode`` (strict)."""
    return HVACMode(value)


def resolve_number_device_class(value: str | None) -> NumberDeviceClass | None:
    """Map a descriptor ``device_class`` string to ``NumberDeviceClass``."""
    return NumberDeviceClass(value) if value is not None else None


def resolve_number_mode(value: str) -> NumberMode:
    """Map a descriptor number-mode string to ``NumberMode`` (strict)."""
    return NumberMode(value)

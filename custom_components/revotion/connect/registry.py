"""Descriptor registry for Connect device codes.

A *descriptor* (:class:`.descriptors.ConnectDeviceDescriptor`) declaratively
describes which platform entities a given ``device`` code produces and how each
maps onto a ``dev_data`` path (see Ha-Integration-Docs/connect-integration.md §5). This module
is the single lookup point every Connect-aware platform consults.

:func:`has_descriptor` is the gate that decides "tailored vs. generic": when a
device has a descriptor, the sensor/binary_sensor platforms build the named
entities from it; when it does not, they fall back to the Phase 0 generic
read-only mirror. Phase 1 registers Thitronik, Phase 2 adds Airtronic 3,
Phase 3 adds Truma Combi, Phase 4 adds Dometic FreshJet, Phase 5 adds Alde 3030
+ EcoFlow PowerKit, Phase 6 adds Dometic Fridge + Absorber, Phase 7 adds Truma
CP+; later phases add one line each.
"""

from __future__ import annotations

from .descriptors import ConnectDeviceDescriptor
from .devices.airtronic3 import AIRTRONIC3_DESCRIPTOR
from .devices.alde import ALDE_DESCRIPTOR
from .devices.dometic_absorber import ABSORBER_DESCRIPTOR
from .devices.dometic_freshjet import FRESHJET_DESCRIPTOR
from .devices.dometic_fridge import FRIDGE_DESCRIPTOR
from .devices.ecoflow import ECOFLOW_DESCRIPTOR
from .devices.thitronik import THITRONIK_DESCRIPTOR
from .devices.truma_combi import TRUMA_COMBI_DESCRIPTOR
from .devices.truma_cpp import TRUMA_CPP_DESCRIPTOR

# device_code -> descriptor. Populated as each device category is implemented.
DEVICE_REGISTRY: dict[int, ConnectDeviceDescriptor] = {
    THITRONIK_DESCRIPTOR.device: THITRONIK_DESCRIPTOR,
    AIRTRONIC3_DESCRIPTOR.device: AIRTRONIC3_DESCRIPTOR,
    TRUMA_COMBI_DESCRIPTOR.device: TRUMA_COMBI_DESCRIPTOR,
    FRESHJET_DESCRIPTOR.device: FRESHJET_DESCRIPTOR,
    ALDE_DESCRIPTOR.device: ALDE_DESCRIPTOR,
    ECOFLOW_DESCRIPTOR.device: ECOFLOW_DESCRIPTOR,
    TRUMA_CPP_DESCRIPTOR.device: TRUMA_CPP_DESCRIPTOR,
    FRIDGE_DESCRIPTOR.device: FRIDGE_DESCRIPTOR,
    ABSORBER_DESCRIPTOR.device: ABSORBER_DESCRIPTOR,
}


def has_descriptor(device_code: int) -> bool:
    """Return True if a device code has a tailored descriptor.

    Devices with a descriptor get their named entities built from it (and the
    generic read-only mirror is suppressed for them); devices without fall back
    to the generic mirror.
    """
    return device_code in DEVICE_REGISTRY


def get_descriptor(device_code: int) -> ConnectDeviceDescriptor | None:
    """Return the descriptor for a device code, or ``None`` if unregistered."""
    return DEVICE_REGISTRY.get(device_code)

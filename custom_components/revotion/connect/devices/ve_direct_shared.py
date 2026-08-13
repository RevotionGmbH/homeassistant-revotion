"""Shared enum tables for the Victron VE.Direct family (devices 768-773).

The brain forwards the raw numeric Victron ``CS`` / ``MPPT`` / ``MODE`` codes
unmapped; the app renders localized labels (``ve_direct_states.dart`` is the
source of truth mirrored here). Every family member shares the same tables, so
each descriptor builds its enum specs through these factories -- one
``translation_key`` per table, shared across devices, and HA translates the
states via ``translations/{en,de}.json``.

Codes that fold into one option (``MODE`` 0/4 -> ``off``) rely on
``EnumSensorSpec.options()`` deduplicating.
"""

from __future__ import annotations

from ..descriptors import EnumSensorSpec

# Victron charge / operating state (`CS`) -- shared across MPPT, Orion XS and
# both Phoenix devices (the inverter reports 1 = low power / eco search and
# 9 = inverting from the same table).
_CHARGE_STATE_MAP: tuple[tuple[int, str], ...] = (
    (0, "off"),
    (1, "low_power"),
    (2, "fault"),
    (3, "bulk"),
    (4, "absorption"),
    (5, "float"),
    (6, "storage"),
    (7, "equalize"),
    (9, "inverting"),
    (11, "power_supply"),
    (245, "starting_up"),
    (246, "absorption"),  # repeated absorption -- same label as 4 (app parity)
    (247, "auto_equalize"),
    (248, "battery_safe"),
    (252, "external_control"),
)

# MPPT tracker operating state (`MPPT`): 0 off / 1 V-or-I-limited / 2 tracking.
_TRACKER_STATE_MAP: tuple[tuple[int, str], ...] = (
    (0, "off"),
    (1, "limited"),
    (2, "active"),
)

# Device `MODE`: codes vary slightly by device (charger 1, inverter 2/3 = on);
# all fold into the same four labels, mirroring ``veDirectModeLabel``.
_MODE_MAP: tuple[tuple[int, str], ...] = (
    (0, "off"),
    (4, "off"),
    (1, "on"),
    (2, "on"),
    (3, "on"),
    (5, "eco"),
    (253, "hibernate"),
)


def charge_state_spec(*, path: str = "cs", key: str = "charge_state") -> EnumSensorSpec:
    """Build the shared Victron charge/operating-state enum sensor spec."""
    return EnumSensorSpec(
        path=path,
        key=key,
        name="Charge state",
        translation_key="ve_charge_state",
        value_map=_CHARGE_STATE_MAP,
    )


def tracker_state_spec(*, path: str = "mppt", key: str = "tracker_state") -> EnumSensorSpec:
    """Build the MPPT tracker operating-state enum sensor spec."""
    return EnumSensorSpec(
        path=path,
        key=key,
        name="Tracker state",
        translation_key="ve_tracker_state",
        value_map=_TRACKER_STATE_MAP,
    )


def mode_spec(*, path: str = "mode", key: str = "device_mode") -> EnumSensorSpec:
    """Build the read-only device MODE enum sensor spec (diagnostic)."""
    return EnumSensorSpec(
        path=path,
        key=key,
        name="Device mode",
        translation_key="ve_mode",
        value_map=_MODE_MAP,
        entity_category="diagnostic",
    )

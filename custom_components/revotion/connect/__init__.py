"""Connect capability (cap 12) support package.

Houses the device-agnostic plumbing for the polymorphic Connect capability:
wire-encoding quirks (:mod:`.coding`), ``dev_data`` flattening
(:mod:`.flatten`), and the device-code descriptor registry
(:mod:`.registry`). The HA platform files (sensor, binary_sensor, ...) import
from here; the per-device descriptors land in Phase 1+.

See Ha-Integration-Docs/connect-integration.md for the full concept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..const import CONNECT_DEVICE_LABELS, ConnectDevice
from .coding import bool_to_int01, int01_to_bool, is_boolish
from .descriptors import ConnectDeviceDescriptor
from .discovery import reconcile_gated_entities, remove_gated_entity
from .flatten import flatten_dev_data
from .registry import get_descriptor, has_descriptor

if TYPE_CHECKING:
    from ..models import Capability

# Keys carried alongside ``dev_data`` in a Connect /data payload that are not
# part of the device data itself (and so must not be mirrored as entities).
CONNECT_META_KEYS = ("device", "dev_data")

# Field name inside ``dev_data`` holding the firmware's overall device status
# enum (surfaced as a diagnostic sensor). See concept §6 / §8.2.
DEV_STAT_KEY = "dev_stat"

# ``dev_stat`` enum values that lock remote control: the device's own panel is
# in control (firmware/app: 6 = main panel in use, 7 = main panel + active
# error). Mirrors the app's ``isConnectivityControllable`` gate -- only the
# lockable descriptors (Alde, Truma, Airtronic3, Thitronik) carry a lock path,
# so Dometic devices (which the app never locks on 6/7) are unaffected.
CONTROL_LOCK_MAIN_PANEL = 6
CONTROL_LOCK_MAIN_PANEL_ERROR = 7

__all__ = [
    "CONNECT_META_KEYS",
    "CONTROL_LOCK_MAIN_PANEL",
    "CONTROL_LOCK_MAIN_PANEL_ERROR",
    "DEV_STAT_KEY",
    "ConnectDeviceDescriptor",
    "bool_to_int01",
    "connect_device_label",
    "control_lock_reason",
    "flatten_connect_capability",
    "flatten_dev_data",
    "get_descriptor",
    "has_descriptor",
    "humanize_path",
    "int01_to_bool",
    "is_boolish",
    "is_path_available",
    "read_dev_data_path",
    "reconcile_gated_entities",
    "remove_gated_entity",
    "resolve_connect_device",
]


def connect_device_label(device_code: int | None) -> str:
    """Return a plain-name prefix for a Connect device code (or "Connect").

    Used by the generic read-only mirror to name entities before an
    app-configured name is available. Unknown/unresolved codes fall back to
    the neutral "Connect".
    """
    if device_code is None:
        return "Connect"
    try:
        return CONNECT_DEVICE_LABELS.get(ConnectDevice(device_code), "Connect")
    except ValueError:
        return "Connect"


def resolve_connect_device(capability: Capability) -> int | None:
    """Return the resolved Connect ``device`` code, or ``None`` if not yet known.

    The device code only appears once the first /data (or /sync) message lands
    in ``capability.data`` (see concept §2.4). Until then a Connect cap carries
    no device and must not be expanded into entities (deferred discovery).
    """
    device = capability.data.get("device")
    if device is None:
        return None
    try:
        return int(device)
    except (TypeError, ValueError):
        return None


def flatten_connect_capability(capability: Capability) -> dict[str, Any]:
    """Flatten a Connect capability's ``dev_data`` into ``{path: value}``.

    Reads ``dev_data`` from ``capability.data`` and delegates to
    :func:`flatten_dev_data`. Returns an empty mapping when ``dev_data`` is
    absent (device resolved but no data yet) so callers can treat "no paths"
    as "nothing to mirror yet".
    """
    dev_data = capability.data.get("dev_data")
    if dev_data is None:
        return {}
    return flatten_dev_data(dev_data)


def humanize_path(path: str) -> str:
    """Turn a flat dev_data path into a readable entity name.

    ``"comb_water.state"`` -> ``"Comb Water State"``,
    ``"pwr.0"`` -> ``"Pwr 0"``. Purely cosmetic; the unique_id keeps the raw
    path so renames here never break entity identity.
    """
    return path.replace("_", " ").replace(".", " ").title()


def read_dev_data_path(capability: Capability, path: str) -> Any:
    """Read a single dotted ``dev_data`` leaf path, or ``None`` if absent.

    Descriptor-driven entities read exactly the path their spec names rather
    than flattening the whole object on every state read. Path segments are the
    dotted form from :func:`flatten_dev_data`: dict keys and integer list
    indices (``"armed"``, ``"comb_water.state"``, ``"stat.0"``). Any missing or
    type-mismatched segment yields ``None`` (treated as "unknown").
    """
    node: Any = capability.data.get("dev_data")
    for segment in path.split("."):
        if isinstance(node, dict):
            node = node.get(segment)
        elif isinstance(node, list):
            try:
                index = int(segment)
            except ValueError:
                return None
            if 0 <= index < len(node):
                node = node[index]
            else:
                return None
        else:
            return None
    return node


# Wire values that an availability flag treats as "not available". A 0/1 flag
# present and falsy means the unit is absent; anything else (including a missing
# flag) is treated as available -- so a device that simply omits the flag is not
# hidden. Shared by the climate/light/switch ``available`` properties.
_UNAVAILABLE_VALUES = (0, False, "0")


def is_path_available(capability: Capability, path: str | None) -> bool:
    """Return whether an optional ``available_path`` flag permits the entity.

    The single source of truth for descriptor ``available_path`` gating
    (climate/light/switch). Semantics (robust / light.py-style):

    - ``path`` is ``None`` (spec has no gate) -> available.
    - the leaf is missing / ``None`` (flag not reported yet) -> available.
    - the leaf is an explicit falsy 0/1 flag (``0``/``False``/``"0"``) ->
      unavailable.
    - any other value -> available.
    """
    if path is None:
        return True
    return read_dev_data_path(capability, path) not in _UNAVAILABLE_VALUES


def control_lock_reason(capability: Capability, lock_path: str | None) -> str | None:
    """Return why a device's control is locked, or ``None`` if it is operable.

    Reads the ``dev_stat`` enum at ``lock_path`` and maps the two "panel in
    control" states to a stable reason string (used for the user-facing error
    message and the ``lock_reason`` attribute):

    - ``7`` (main panel + active error) -> ``"main_panel_error"``
    - ``6`` (main panel in use) -> ``"main_panel"``
    - anything else, a missing value, or ``lock_path is None`` -> ``None``
      (operable). A device that simply has not reported ``dev_stat`` yet is
      treated as operable, never locked-by-default.

    The value is coerced to int so a wire ``"6"``/``6`` both match; a
    non-numeric value reads as operable.
    """
    if lock_path is None:
        return None
    try:
        code = int(read_dev_data_path(capability, lock_path))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if code == CONTROL_LOCK_MAIN_PANEL_ERROR:
        return "main_panel_error"
    if code == CONTROL_LOCK_MAIN_PANEL:
        return "main_panel"
    return None

"""Wire-encoding helpers for the Connect capability (cap 12).

The Connect wire-contract has a few serialization quirks that the rest of the
integration must not assume away (see Ha-Integration-Docs/connect-integration.md §2.3):

- **bool is serialized as the number 0/1**, never ``true``/``false``. Read and
  write both directions through :func:`int01_to_bool` / :func:`bool_to_int01`.
- floats are rounded to 2 decimals (``float2``) on the wire.
- parsers ignore unknown keys (forward-compat); HA may receive extra keys.

Phase 0 only reads, so the write helper is provided for the Connect control
plumbing in later phases but is unused here.
"""

from __future__ import annotations

from typing import Any


def int01_to_bool(value: Any) -> bool | None:
    """Decode a wire 0/1 integer into a Python bool.

    Returns ``None`` when the value is missing (so a real ``0`` is never
    confused with "no data"). Accepts the numeric/string forms the firmware
    may emit; anything non-coercible decodes as ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in ("0", "1"):
            return stripped == "1"
        lowered = stripped.lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    return None


def bool_to_int01(value: bool) -> int:
    """Encode a Python bool into the wire 0/1 integer."""
    return 1 if value else 0


def is_boolish(value: Any) -> bool:
    """Return True when a flattened value looks like a 0/1 boolean flag.

    Used by the generic read-only mirror to route a field to binary_sensor
    vs. sensor: a bare ``0`` or ``1`` (int or matching numeric string) is
    treated as a flag. Real bools also count. Floats like ``1.0`` are left
    as numeric sensors -- the firmware sends flags as plain integers.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return value in (0, 1)
    if isinstance(value, str):
        return value.strip() in ("0", "1")
    return False

"""Flatten a Connect ``dev_data`` object into scalar leaf paths.

The Connect wire-contract reuses the same (possibly nested) ``dev_data`` object
for status and control (see Ha-Integration-Docs/connect-integration.md §2). For the Phase 0
generic read-only mirror we turn that object into a flat ``path -> value`` map,
one entry per scalar leaf, so each leaf can back exactly one HA entity.

Design choice for arrays (e.g. ``bat``, ``stat``, ``pwr``): they are expanded
with **index segments** (``pwr.0``, ``bat.1.volt``), mirroring the existing
dynamic battery current-channel pattern (one entity per array element). This
keeps every scalar individually addressable and lets later /data messages grow
the set of mirrored entities. Containers (dict / list) are never emitted as
leaves themselves -- only their scalar contents are.

Empty containers produce no path. ``None`` leaves are kept (they back an
"unknown" entity rather than silently disappearing).
"""

from __future__ import annotations

from typing import Any

# Joins path segments. A dot reads well in HA entity names and matches the
# "comb_water.state" style used throughout the Connect concept doc.
PATH_SEP = "."


def flatten_dev_data(dev_data: Any, _prefix: str = "") -> dict[str, Any]:
    """Recursively flatten ``dev_data`` to ``{flat_path: scalar_value}``.

    Args:
        dev_data: The ``dev_data`` object from a Connect /data payload. Usually
            a dict, but tolerated as a bare scalar/list for robustness.
        _prefix: Internal accumulator for the current path; callers pass "".

    Returns:
        Mapping from dotted leaf path to scalar value. Dicts recurse by key,
        lists recurse by integer index, everything else is a scalar leaf.

    """
    result: dict[str, Any] = {}

    if isinstance(dev_data, dict):
        for key, value in dev_data.items():
            child_prefix = f"{_prefix}{PATH_SEP}{key}" if _prefix else str(key)
            result.update(flatten_dev_data(value, child_prefix))
    elif isinstance(dev_data, list):
        for index, value in enumerate(dev_data):
            child_prefix = f"{_prefix}{PATH_SEP}{index}" if _prefix else str(index)
            result.update(flatten_dev_data(value, child_prefix))
    else:
        # Scalar leaf (int/float/str/bool/None). A top-level scalar with no
        # prefix is unexpected for dev_data; skip it rather than emit "".
        if _prefix:
            result[_prefix] = dev_data

    return result

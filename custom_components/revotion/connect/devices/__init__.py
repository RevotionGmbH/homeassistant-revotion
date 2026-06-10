"""Per-device Connect descriptors.

Each module here defines exactly one :class:`..descriptors.ConnectDeviceDescriptor`
for a single ``ConnectDevice`` code and is wired into the registry in
:mod:`..registry`. Adding a new device category is "add a module + one registry
line", never a change to the platform files.
"""

from __future__ import annotations

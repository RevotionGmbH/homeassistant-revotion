"""Lock platform for the Revotion integration.

Hosts Connect-device locks (Phase 1: Thitronik WiPro III SafeLock, device 1024).
Generic registry dispatch, mirroring alarm_control_panel.py: any Connect device
whose descriptor has a ``lock`` spec produces one :class:`RevotionConnectLock`.

SafeLock gating (descriptor ``LockSpec.config_flag``): the Thitronik lock entity
should only exist when the device is configured with SafeLock. That flag lives
in the app's ``dev_conf.locked`` ("1"/"0"). Whether the HA sync/config endpoint
surfaces it top-level in ``capability.config.data`` or nested under ``dev_conf``
is not verifiable without a live device, so :func:`_safelock_configured`
checks both shapes and, when the flag is absent entirely, errs on the side of
creating the lock (better a harmless extra entity than a missing control). The
"absent -> create" path is a TO-VERIFY point (see module summary).

Locking/unlocking publishes a ``ctr_data`` command via ConnectCommandMixin with
optimistic state (LTE-M round-trip up to 5 s).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connect import get_descriptor, has_descriptor, int01_to_bool, read_dev_data_path, resolve_connect_device
from .connect.control import ConnectCommandMixin, connect_command_dev_data
from .connect.descriptors import LockSpec
from .const import CONF_BRAIN_MAC, DOMAIN, CapabilityType, RevotionConfigEntry
from .coordinator import RevotionCoordinator
from .models import Capability, RevotionCapabilityMixin, format_mac_for_display, normalize_mac, register_node_device
from .mqtt_client import RevotionMqttClient

_LOGGER = logging.getLogger(__name__)


def _safelock_configured(capability: Capability, config_flag: str | None) -> bool:
    """Return whether the lock entity should be created for this capability.

    ``config_flag`` is the ``capability.config.data`` key gating the lock
    (Thitronik: ``"locked"``). The flag may arrive top-level or nested under
    ``dev_conf`` depending on what the sync endpoint emits; both shapes are
    checked.

    The gate is opt-in: only an explicit ``"1"`` creates the lock. When the
    flag is absent (e.g. config not synced yet) we do NOT create it -- the app
    default is hasSafeLock=false, so a Thitronik without SafeLock must not show
    a lock. A later config-sync that sets the flag pulls the entity in via the
    deferred-discovery listener (review S2). A descriptor with ``config_flag``
    None has no gate and always creates the lock.
    """
    if config_flag is None:
        return True
    config_data = capability.config.data
    if config_flag in config_data:
        return str(config_data[config_flag]) == "1"
    dev_conf = config_data.get("dev_conf")
    if isinstance(dev_conf, dict) and config_flag in dev_conf:
        return str(dev_conf[config_flag]) == "1"
    # Flag not present in any known shape -> do not create (opt-in default).
    return False


class RevotionConnectLock(
    ConnectCommandMixin,
    RevotionCapabilityMixin,
    CoordinatorEntity[RevotionCoordinator],
    LockEntity,
):
    """Lock for a Connect device with a SafeLock (e.g. Thitronik).

    ``is_locked`` derives from a 0/1 ``dev_data`` flag; lock/unlock publish a
    command code on ``ctr_data`` and assume the new state optimistically until
    the MQTT echo arrives (cleared in ``_handle_coordinator_update``).
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
        spec: LockSpec,
        mqtt_client: RevotionMqttClient,
        config_name: str = "",
    ) -> None:
        """Initialize the lock entity."""
        super().__init__(coordinator)
        self._brain_mac = format_mac_for_display(brain_mac)
        self._node_mac = normalize_mac(node_mac)
        self._cap_index = cap_index
        self._device_code = device_code
        self._spec = spec
        self._mqtt_client = mqtt_client
        self._init_connect_command_state()
        self._optimistic_locked: bool | None = None
        brain_mac_normalized = normalize_mac(brain_mac)
        self._attr_unique_id = f"revotion_{brain_mac_normalized}_{self._node_mac}_{cap_index}_{spec.key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._node_mac)}}
        # has_entity_name=True: device carries the name, entity is just the field.
        self._attr_name = spec.name

    @property
    def available(self) -> bool:
        """Return True if the node is reachable and the capability exists."""
        return super().available and self._node_reachable() and self._find_capability() is not None

    @property
    def is_locked(self) -> bool | None:
        """Return the locked flag from dev_data, or the optimistic assumption."""
        if self._optimistic_locked is not None:
            return self._optimistic_locked
        cap = self._find_capability()
        if cap is None:
            return None
        return int01_to_bool(read_dev_data_path(cap, self._spec.locked_path))

    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic state and command lock once real data confirms it."""
        self._sync_command_state()
        super()._handle_coordinator_update()

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption on command timeout."""
        self._optimistic_locked = None

    def _optimistic_confirmed(self) -> bool:
        """Return True once the dev_data locked flag matches the optimistic one."""
        if self._optimistic_locked is None:
            return True
        cap = self._find_capability()
        if cap is None:
            return False
        real = int01_to_bool(read_dev_data_path(cap, self._spec.locked_path))
        return real is not None and real == self._optimistic_locked

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock by publishing the lock command."""
        await self._publish_connect_command(connect_command_dev_data(self._spec.lock_command))
        self._optimistic_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock by publishing the unlock command."""
        await self._publish_connect_command(connect_command_dev_data(self._spec.unlock_command))
        self._optimistic_locked = False
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RevotionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Connect locks via deferred descriptor dispatch."""
    coordinator = entry.runtime_data.coordinator
    mqtt_client = entry.runtime_data.mqtt_client
    brain_mac = entry.data[CONF_BRAIN_MAC]
    known: set[tuple[str, int, str]] = set()

    def _check_connect() -> None:
        """Create locks for Connect devices whose descriptor defines one (if configured)."""
        if coordinator.data is None:
            return
        current_macs = {normalize_mac(n.mac_address) for n in coordinator.data.nodes}
        stale = {key for key in known if key[0] not in current_macs}
        known.difference_update(stale)

        entities: list[RevotionConnectLock] = []
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
                if descriptor.lock is None:
                    continue
                key = (node_mac, capability.capability_index, descriptor.lock.key)
                if key in known:
                    continue
                if not _safelock_configured(capability, descriptor.lock.config_flag):
                    # SafeLock not (yet) configured -> no lock entity. Do NOT mark
                    # as handled: a later config-sync that sets the flag must be
                    # able to pull the entity in on a subsequent update (S2).
                    continue
                known.add(key)
                register_node_device(hass, entry, node, brain_mac)
                entities.append(
                    RevotionConnectLock(
                        coordinator=coordinator,
                        brain_mac=brain_mac,
                        node_mac=node.mac_address,
                        cap_index=capability.capability_index,
                        device_code=device_code,
                        spec=descriptor.lock,
                        mqtt_client=mqtt_client,
                        config_name=capability.config.name,
                    )
                )

        if entities:
            async_add_entities(entities)

    _check_connect()
    entry.async_on_unload(coordinator.async_add_listener(_check_connect))

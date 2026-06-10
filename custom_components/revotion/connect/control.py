"""Command publishing + optimistic-state mixin for writable Connect entities.

Connect control mirrors the proven switch.py mechanism (command lock + 60 s
timeout + optimistic state cleared on the real MQTT echo) so it can be reused by
every writable Connect entity across Phases 1-5 (alarm_control_panel, lock, and
later climate/select/number/switch). The rationale is the LTE-M round-trip of up
to 5 s: without an optimistic assumption the coordinator overwrites the UI
before the echo arrives and the control bounces back (see
Ha-Integration-Docs/connect-integration.md §2.2 / §3.3).

What this mixin adds on top of :class:`..models.RevotionCapabilityMixin`:

- ``_publish_connect_command(dev_data)`` -- builds the ``ctr_data`` payload
  (``{"MAC": node-mac, "cap_index": idx, "device": <code>, "dev_data": {...}}``
  on ``{brain-mac}/ctr_data``) and publishes it under a single-command lock with
  a timeout that reverts the optimistic state and notifies the user.
- ``_sync_command_state`` / ``_optimistic_confirmed`` / ``_clear_command_lock``
  -- optimistic-state bookkeeping. The owning entity stores *what* it is
  optimistic about (a state enum, a bool, ...) in its own attribute and consults
  it in its state property; this mixin only owns the lifecycle (set on send,
  clear once the echo confirms, revert on timeout) and the TURNAROUND/TIMEOUT
  logging.

Thitronik commands are the simplest shape: ``dev_data`` is just
``{"command": <code>}`` (no field mirror). Later devices pass the mirrored
``dev_data`` fields they are setting; the publish/lock/timeout machinery is
identical.

Entities mix this in *and* override ``_handle_coordinator_update`` to call
:meth:`_sync_command_state` before delegating to ``super()`` so the optimistic
value drops as soon as real data *confirms* the command (each entity overrides
:meth:`_optimistic_confirmed` with its value comparison). Clearing on just any
update would bounce the UI back: the coordinator fires for every MQTT message,
not only for this entity's echo.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.helpers.event import async_call_later

from ..const import COMMAND_TIMEOUT_MESSAGE, DOMAIN, TOPIC_CONTROL_DATA

_LOGGER = logging.getLogger(__name__)

# Mirror switch.py: revert the optimistic assumption if no MQTT echo lands
# within this window (LTE-M round-trips run up to ~5 s, so 60 s is generous).
COMMAND_TIMEOUT_S = 60

# English fallback text (logs / missing translation); the UI message comes
# from the exceptions.control_locked_* keys in strings.json/translations.
_CONTROL_LOCK_FALLBACKS = {
    "main_panel": "Controls are locked - the device is currently being operated at its main panel.",
    "main_panel_error": "Controls are locked - device error, check the main panel.",
}


class ConnectCommandMixin:
    """Optimistic command publishing for writable Connect entities.

    Expected to be combined with :class:`..models.RevotionCapabilityMixin`
    (for ``_build_base_payload``/``_find_capability``) and a HA
    ``CoordinatorEntity`` subclass. Requires the host entity to set, in its
    ``__init__``:

    - ``self._brain_mac`` -- brain MAC in display format (with colons), the
      ``ctr_data`` topic owner.
    - ``self._device_code`` -- the Connect ``device`` code (int) to stamp into
      every command payload.
    - ``self._mqtt_client`` -- the :class:`..mqtt_client.RevotionMqttClient`.

    and to call :meth:`_init_connect_command_state` from its ``__init__``.
    """

    _brain_mac: str
    _device_code: int

    def _init_connect_command_state(self) -> None:
        """Initialise optimistic-command bookkeeping. Call from ``__init__``."""
        self._command_sent_at: float | None = None
        self._command_pending: bool = False
        self._timeout_cancel: CALLBACK_TYPE | None = None

    # --- optimistic-state lifecycle -------------------------------------------

    def _cancel_command_timeout(self) -> None:
        """Cancel the pending command-timeout callback, if any."""
        if self._timeout_cancel is not None:
            self._timeout_cancel()
            self._timeout_cancel = None

    def _clear_command_lock(self) -> None:
        """Release the command lock + timeout and log the turnaround.

        Invoked via :meth:`_sync_command_state` once the echo confirms the
        command. The entity clears its own optimistic value (via
        :meth:`_revert_optimistic`); this clears the shared lock/timeout so a
        new command can be sent.
        """
        if self._command_sent_at is not None:
            elapsed = time.monotonic() - self._command_sent_at
            _LOGGER.debug(
                "TURNAROUND %s: %.2fs (command -> MQTT echo)",
                self.entity_id or self.unique_id,  # type: ignore[attr-defined]
                elapsed,
            )
            self._command_sent_at = None
        self._cancel_command_timeout()
        self._command_pending = False

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any in-flight command timeout when the entity is removed.

        The 60 s timeout is scheduled via ``async_call_later``; without this it
        would survive a reload/unpair and fire against a dead entity. Symmetric
        to the timeout scheduled in :meth:`_publish_connect_command`. Hosts that
        override this must call ``super().async_will_remove_from_hass()``.
        """
        self._cancel_command_timeout()
        self._command_pending = False
        self._command_sent_at = None
        await super().async_will_remove_from_hass()  # type: ignore[misc]

    @callback
    def _on_command_timeout(self, _now: Any) -> None:
        """Revert optimistic state and notify when no echo arrives in time.

        The entity overrides :meth:`_revert_optimistic` to drop whatever it was
        optimistic about; this then writes the reverted state and posts a
        persistent notification (mirrors switch.py).
        """
        self._timeout_cancel = None
        self._command_pending = False
        self._command_sent_at = None
        self._revert_optimistic()
        entity_name = self.name or self.entity_id or self.unique_id  # type: ignore[attr-defined]
        _LOGGER.warning(
            "TIMEOUT %s: no MQTT echo within %ds",
            entity_name,
            COMMAND_TIMEOUT_S,
        )
        if self.hass is not None:  # type: ignore[attr-defined]
            # Schedule the service call instead of calling it synchronously: this
            # runs in an @callback context (no blocking / no awaiting allowed).
            self.hass.async_create_task(  # type: ignore[attr-defined]
                self.hass.services.async_call(  # type: ignore[attr-defined]
                    "persistent_notification",
                    "create",
                    {
                        "title": "Revotion Connect Timeout",
                        "message": COMMAND_TIMEOUT_MESSAGE.format(entity=entity_name, timeout=COMMAND_TIMEOUT_S),
                        "notification_id": f"revotion_connect_timeout_{self.unique_id}",  # type: ignore[attr-defined]
                    },
                )
            )
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _revert_optimistic(self) -> None:
        """Drop the optimistic assumption. Overridden by the host entity.

        Default is a no-op so a host that has nothing to revert (rare) need not
        override it. Most entities reset their ``_optimistic_*`` attribute here.
        """

    def _optimistic_confirmed(self) -> bool:
        """Return True once real data confirms the optimistic assumption.

        Overridden by the host entity to compare its ``_optimistic_*`` value(s)
        against the freshly arrived dev_data. Must return True when no
        optimistic value is active (nothing to confirm). The default True keeps
        hosts without an override on the old clear-on-any-update behaviour.
        """
        return True

    def _sync_command_state(self) -> None:
        """Release lock + optimistic state once the echo confirms the command.

        Call from the entity's ``_handle_coordinator_update`` *before*
        ``super()._handle_coordinator_update()``. The coordinator pushes an
        update for *every* incoming MQTT message (any capability, status, GPS),
        so clearing unconditionally would drop the optimistic value while the
        real echo is still in flight over LTE-M -- the UI would bounce back to
        the stale value. Instead the state is kept until the data actually
        matches (or the 60 s timeout reverts it).
        """
        if self._optimistic_confirmed():
            self._clear_command_lock()
            self._revert_optimistic()

    # --- control-lock (panel in use / error) ----------------------------------

    def _resolve_lock_path(self) -> str | None:
        """Return the ``dev_stat`` path that locks this entity's control, or None.

        A spec-level ``lock_path`` (Truma CP+ heater/air_con sub-devices) wins;
        otherwise the device-wide ``control_lock_path`` from the descriptor
        (Alde, Truma Combi, Airtronic3, Thitronik). ``None`` -> the device never
        locks on the panel (e.g. Dometic). Imported locally to avoid any
        package import cycle (``connect`` -> ``connect.control``).
        """
        from . import get_descriptor

        spec = getattr(self, "_spec", None)
        spec_lock = getattr(spec, "lock_path", None)
        if spec_lock is not None:
            return spec_lock
        descriptor = get_descriptor(self._device_code)
        return descriptor.control_lock_path if descriptor is not None else None

    def _connect_lock_reason(self) -> str | None:
        """Return the active control-lock reason for this entity, or ``None``.

        ``None`` when the device is operable, not lockable, or has not reported
        ``dev_stat`` yet. See :func:`..control_lock_reason`.
        """
        from . import control_lock_reason

        lock_path = self._resolve_lock_path()
        if lock_path is None:
            return None
        cap = self._find_capability()  # type: ignore[attr-defined]
        if cap is None:
            return None
        return control_lock_reason(cap, lock_path)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the control-lock state on lockable Connect entities.

        Returns ``{"control_locked": bool, "lock_reason": str | None}`` for
        entities whose device locks on the panel (Alde, Truma, Airtronic3,
        Thitronik), and ``None`` otherwise so non-lockable Connect entities keep
        the HA default (no attributes). No Connect writable entity defines its
        own ``extra_state_attributes``; one that later needs to must merge this.
        """
        lock_path = self._resolve_lock_path()
        if lock_path is None:
            return None
        reason = self._connect_lock_reason()
        return {"control_locked": reason is not None, "lock_reason": reason}

    # --- publishing -----------------------------------------------------------

    async def _publish_connect_command(self, dev_data: dict[str, Any]) -> None:
        """Publish a ``ctr_data`` command for this Connect device.

        Builds ``{"MAC": node-mac, "cap_index": idx, "device": <code>,
        "dev_data": dev_data}`` and publishes it on ``{brain-mac}/ctr_data``
        under the single-command lock. Schedules a timeout that reverts the
        optimistic state. Raises ``HomeAssistantError`` if a command is still
        pending (no echo yet) so the UI surfaces "please wait".

        Args:
            dev_data: The device-specific control object. For Thitronik this is
                ``{"command": <code>}``; later devices mirror the fields they
                set.

        Raises ``HomeAssistantError`` if the device's own panel currently holds
        control (dev_stat 6/7) -- the command would be rejected by the device
        anyway, so blocking it avoids a wasted LTE-M round-trip and the 60 s
        optimistic-state revert. Mirrors the app's locked-control behaviour.
        Also raises if the Brain is on LTE-M (writes are WiFi-only, SIM cost
        guard).
        """
        from homeassistant.exceptions import HomeAssistantError

        self.coordinator.assert_commands_allowed()  # type: ignore[attr-defined]
        lock_reason = self._connect_lock_reason()
        if lock_reason is not None:
            raise HomeAssistantError(
                _CONTROL_LOCK_FALLBACKS[lock_reason],
                translation_domain=DOMAIN,
                translation_key=f"control_locked_{lock_reason}",
            )

        if self._command_pending:
            entity_name = str(self.name or self.entity_id)  # type: ignore[attr-defined]
            raise HomeAssistantError(
                f"A command for {entity_name} is still pending. Please wait.",
                translation_domain=DOMAIN,
                translation_key="command_pending",
                translation_placeholders={"entity": entity_name},
            )

        payload = self._build_base_payload()  # type: ignore[attr-defined]
        payload["device"] = self._device_code
        payload["dev_data"] = dev_data

        topic = TOPIC_CONTROL_DATA.format(mac=self._brain_mac)
        self._command_sent_at = time.monotonic()
        self._command_pending = True
        self._cancel_command_timeout()
        if self.hass is not None:  # type: ignore[attr-defined]
            self._timeout_cancel = async_call_later(
                self.hass,  # type: ignore[attr-defined]
                COMMAND_TIMEOUT_S,
                self._on_command_timeout,
            )
        _LOGGER.debug(
            "COMMAND SENT %s -> %s: %s",
            self.entity_id or self.unique_id,  # type: ignore[attr-defined]
            topic,
            payload,
        )
        try:
            await self._mqtt_client.async_publish(topic, json.dumps(payload))  # type: ignore[attr-defined]
        except Exception:
            self._command_pending = False
            self._command_sent_at = None
            self._cancel_command_timeout()
            raise


def connect_command_dev_data(command: int) -> dict[str, Any]:
    """Build the Thitronik-style ``dev_data`` for a bare command code.

    Several Connect devices (Thitronik especially) control via a single opaque
    ``{"command": <code>}`` object rather than mirroring status fields. Kept as
    a tiny helper so the command shape lives in one place.
    """
    return {"command": command}


def set_dev_data_path(dev_data: dict[str, Any], path: str, value: Any) -> None:
    """Set a dotted control path into ``dev_data``, creating/merging nested dicts.

    The write counterpart to :func:`..read_dev_data_path`. A flat key
    (``"energy_sel"``) sets a top-level field; a dotted key
    (``"comb_air.state"``) descends, creating intermediate dicts and *merging*
    into any already present -- so several fields of the same branch
    accumulate into one object::

        d = {}
        set_dev_data_path(d, "comb_air.state", 1)
        set_dev_data_path(d, "comb_air.target_temp", 22.0)
        # d == {"comb_air": {"state": 1, "target_temp": 22.0}}

    This fixes the Phase 2 ``split(".", 1)[0]`` shortcut that wrote nested
    control fields to the wrong (top) level (review finding). Intermediate
    segments are always integer-free dict keys; Connect control payloads never
    address list indices on the write side, so no list handling is needed.
    """
    segments = path.split(".")
    node = dev_data
    for segment in segments[:-1]:
        existing = node.get(segment)
        if not isinstance(existing, dict):
            existing = {}
            node[segment] = existing
        node = existing
    node[segments[-1]] = value

"""MQTT client for the Revotion integration."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from collections.abc import Callable

import aiomqtt

from .const import (
    TOPIC_ACK,
    TOPIC_CONFIG,
    TOPIC_DATA,
    TOPIC_ERROR,
    TOPIC_GPS,
    TOPIC_PAIR,
    TOPIC_STATUS,
)

_LOGGER = logging.getLogger(__name__)


class StaleConnectionError(Exception):
    """Raised when a connection stops delivering messages (half-open session)."""


class RevotionMqttClient:
    """Manages MQTT connection to the Revotion VerneMQ broker.

    Handles persistent TLS connection, automatic reconnection with
    exponential backoff, topic subscriptions, and message dispatch
    via callbacks.
    """

    INITIAL_BACKOFF = 5
    MAX_BACKOFF = 300
    BACKOFF_FACTOR = 2
    # Explicit broker keepalive (aiomqtt default is 60s): on a dead TCP path the
    # missing PINGRESP surfaces as MqttError within ~2x this interval and forces
    # a reconnect instead of waiting on the half-open socket indefinitely.
    KEEPALIVE = 30
    # Receive watchdog: keepalive pings cannot detect a session where TCP is
    # alive but the broker no longer delivers messages (observed live: sensor
    # "connected", publishes fine, zero inbound for >15 min). Silence alone
    # proves nothing, though: the firmware has no heartbeat and publishes only
    # on change, so a parked brain is legitimately quiet for hours (tearing the
    # session down on silence caused ~45 reconnects + REST syncs per day). After
    # RECEIVE_WATCHDOG_TIMEOUT of silence the delivery path is therefore probed
    # instead: re-subscribing {mac}/status makes the broker re-send its retained
    # message (online payload or LWT) on the live session -- MQTT 3.1.1 §3.8.4,
    # VerneMQ vmq_reg:subscribe_op does so for every SUBSCRIBE. Any message
    # within PROBE_TIMEOUT proves the session healthy; only an unanswered probe
    # rebuilds the connection.
    RECEIVE_WATCHDOG_TIMEOUT = 600
    PROBE_TIMEOUT = 30

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        tls_context: ssl.SSLContext,
        brain_mac: str,
        on_message: Callable[[str, bytes, bool], None],
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
    ) -> None:
        """Initialize the MQTT client.

        Args:
            host: MQTT broker hostname (mqtt-ha.revotion.net).
            port: MQTT broker port (8885 for TLS).
            username: Brain MAC with colons (aa:bb:cc:dd:ee:ff).
            password: Authentication token.
            tls_context: SSL context for TLS connection.
            brain_mac: Normalized MAC for topic construction (aabbccddeeff).
            on_message: Callback for incoming messages (topic, payload, retained).
                ``retained`` is True for a retained message the broker re-sent
                because of a (re)subscribe, False for a live publish.
            on_connected: Callback fired after successful connection.
            on_disconnected: Callback fired after disconnection.
        """
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._tls_context = tls_context
        self._brain_mac = brain_mac
        self._on_message = on_message
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._is_connected = False
        self._client: aiomqtt.Client | None = None
        self._message_count: int = 0
        self._reconnect_count: int = 0
        self._stale_reconnect_count: int = 0
        self._probe_count: int = 0
        self._last_message_monotonic: float | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether the MQTT client is currently connected.

        A connection that has not delivered a single message within the
        receive-watchdog window plus the probe window is reported as
        disconnected even if the socket is still open (half-open session).
        A merely quiet session is answered by the probe well within that.
        """
        if not self._is_connected:
            return False
        if self._last_message_monotonic is None:
            return True
        stale_after = self.RECEIVE_WATCHDOG_TIMEOUT + self.PROBE_TIMEOUT
        return time.monotonic() - self._last_message_monotonic < stale_after

    @property
    def message_count(self) -> int:
        """Return total MQTT messages received since startup."""
        return self._message_count

    @property
    def reconnect_count(self) -> int:
        """Return total MQTT reconnections since startup."""
        return self._reconnect_count

    @property
    def stale_reconnect_count(self) -> int:
        """Return reconnections forced by an unanswered watchdog probe."""
        return self._stale_reconnect_count

    @property
    def probe_count(self) -> int:
        """Return delivery probes sent by the receive watchdog."""
        return self._probe_count

    @property
    def seconds_since_last_message(self) -> float | None:
        """Return seconds since the last received message.

        The timer also resets when a connection is established, so on a
        fresh connection this is the connection age until the first
        message (usually the retained GPS) arrives.
        """
        if self._last_message_monotonic is None:
            return None
        return round(time.monotonic() - self._last_message_monotonic, 1)

    @property
    def _topics(self) -> list[str]:
        """Return list of 7 MQTT topics to subscribe to."""
        return [
            TOPIC_DATA.format(mac=self._brain_mac),
            TOPIC_CONFIG.format(mac=self._brain_mac),
            TOPIC_STATUS.format(mac=self._brain_mac),
            TOPIC_GPS.format(mac=self._brain_mac),
            TOPIC_ERROR.format(mac=self._brain_mac),
            TOPIC_PAIR.format(mac=self._brain_mac),
            # Command ACKs (Brain >= 2.3.3); older brains never publish here.
            TOPIC_ACK.format(mac=self._brain_mac),
        ]

    async def _connection_loop(self) -> None:
        """Reconnect loop with exponential backoff.

        Creates a new aiomqtt.Client for each connection attempt.
        On successful connection, subscribes to all topics and processes
        incoming messages. After RECEIVE_WATCHDOG_TIMEOUT without a message a
        receive watchdog probes delivery by re-subscribing the retained status
        topic; if nothing arrives within PROBE_TIMEOUT either, the session is
        half-open and torn down. On MqttError, backs off exponentially.
        On CancelledError, exits cleanly.
        """
        backoff = self.INITIAL_BACKOFF
        while True:
            try:
                client = aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    tls_context=self._tls_context,
                    keepalive=self.KEEPALIVE,
                )
                async with client:
                    self._client = client
                    self._is_connected = True
                    self._last_message_monotonic = time.monotonic()
                    backoff = self.INITIAL_BACKOFF

                    for topic in self._topics:
                        await client.subscribe(topic)

                    _LOGGER.info(
                        "Connected to MQTT broker %s:%s for brain %s",
                        self._host,
                        self._port,
                        self._brain_mac,
                    )
                    self._reconnect_count += 1
                    self._on_connected()

                    messages = aiter(client.messages)
                    probing = False
                    while True:
                        try:
                            async with asyncio.timeout(
                                self.PROBE_TIMEOUT if probing else self.RECEIVE_WATCHDOG_TIMEOUT
                            ):
                                message = await anext(messages)
                        except TimeoutError as err:
                            if probing:
                                raise StaleConnectionError(
                                    f"no message received for {self.RECEIVE_WATCHDOG_TIMEOUT}s "
                                    f"and status probe unanswered for {self.PROBE_TIMEOUT}s"
                                ) from err
                            probing = True
                            self._probe_count += 1
                            _LOGGER.debug(
                                "No MQTT message for brain %s in %ss, probing delivery via retained status",
                                self._brain_mac,
                                self.RECEIVE_WATCHDOG_TIMEOUT,
                            )
                            await client.subscribe(TOPIC_STATUS.format(mac=self._brain_mac))
                            continue
                        except StopAsyncIteration:
                            raise aiomqtt.MqttError("Message stream ended unexpectedly") from None
                        probing = False
                        self._last_message_monotonic = time.monotonic()
                        _LOGGER.debug(
                            "MQTT message on %s: %d bytes",
                            message.topic.value,
                            len(message.payload) if isinstance(message.payload, bytes) else 0,
                        )
                        self._message_count += 1
                        try:
                            self._on_message(
                                message.topic.value,
                                message.payload if isinstance(message.payload, bytes) else b"",
                                bool(message.retain),
                            )
                        except Exception:
                            _LOGGER.exception(
                                "Error processing MQTT message on %s",
                                message.topic.value,
                            )
            except StaleConnectionError as err:
                self._client = None
                self._is_connected = False
                self._stale_reconnect_count += 1
                _LOGGER.warning(
                    "MQTT connection for brain %s looks half-open (%s), reconnecting now",
                    self._brain_mac,
                    err,
                )
                self._on_disconnected()
                # No backoff: the watchdog already waited RECEIVE_WATCHDOG_TIMEOUT
                # plus PROBE_TIMEOUT, so an immediate reconnect cannot tight-loop.
            except (aiomqtt.MqttError, OSError) as err:
                self._client = None
                self._is_connected = False
                _LOGGER.warning(
                    "MQTT connection lost for brain %s: %s. Reconnecting in %ds",
                    self._brain_mac,
                    err,
                    backoff,
                )
                self._on_disconnected()
                await asyncio.sleep(backoff)
                backoff = min(backoff * self.BACKOFF_FACTOR, self.MAX_BACKOFF)
            except asyncio.CancelledError:
                self._client = None
                self._is_connected = False
                _LOGGER.info(
                    "MQTT connection loop cancelled for brain %s",
                    self._brain_mac,
                )
                break

    async def async_publish(self, topic: str, payload: str) -> None:
        """Publish a message to the MQTT broker.

        Raises HomeAssistantError if not connected or publish fails.
        """
        if not self._is_connected or self._client is None:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("MQTT not connected, cannot send command")
        try:
            await self._client.publish(topic, payload.encode())
        except aiomqtt.MqttError as err:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(f"Failed to publish MQTT message: {err}") from err

    async def disconnect(self) -> None:
        """Mark client as disconnected.

        The actual loop termination is handled by cancelling the
        background task (via CancelledError in _connection_loop).
        """
        self._is_connected = False

"""Async REST API client for the Revotion API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import API_BASE_URL, API_TIMEOUT, CapabilityType
from .models import Brain, Capability, CapabilityConfig, Node, normalize_mac

_LOGGER = logging.getLogger(__name__)


class RevotionApiError(Exception):
    """Base exception for Revotion API errors."""


class RevotionAuthError(RevotionApiError):
    """Authentication failed (HTTP 401)."""


class RevotionSubscriptionError(RevotionApiError):
    """Subscription required (HTTP 403)."""


class RevotionConnectionError(RevotionApiError):
    """Connection error (timeout, DNS failure, HTTP 5xx)."""


class RevotionNotFoundError(RevotionApiError):
    """Brain not found (HTTP 404)."""


class InvalidJsonError(Exception):
    """Invalid JSON in API response or config input."""


class RevotionApiClient:
    """Async REST client for the Revotion API.

    Uses the shared aiohttp session from Home Assistant (obtained via
    async_get_clientsession(hass) in __init__.py) and authenticates
    with a Bearer token.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        brain_mac: str,
    ) -> None:
        """Initialize the API client.

        Args:
            session: Shared aiohttp session from HA (via async_get_clientsession).
            token: Bearer token for REST API authentication.
            brain_mac: Normalized MAC address of the Brain device.

        """
        self._session = session
        self._token = token
        self._brain_mac = brain_mac
        self._base_url = API_BASE_URL

    @property
    def _headers(self) -> dict[str, str]:
        """Return authentication headers for API requests."""
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str) -> Any:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g. /brain/status/{mac}).

        Returns:
            Parsed JSON response (dict for most endpoints, list for /brain/error).

        Raises:
            RevotionAuthError: On HTTP 401 (invalid/expired token).
            RevotionSubscriptionError: On HTTP 403 (premium required).
            RevotionNotFoundError: On HTTP 404 (brain not found).
            RevotionConnectionError: On HTTP 5xx, timeout, or network error.

        """
        url = f"{self._base_url}{path}"
        try:
            async with asyncio.timeout(API_TIMEOUT):
                response = await self._session.request(method, url, headers=self._headers)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise RevotionConnectionError(f"Error communicating with Revotion API: {err}") from err

        if response.status == 401:
            raise RevotionAuthError("Invalid or expired token")
        if response.status == 403:
            raise RevotionSubscriptionError("Premium subscription required")
        if response.status == 404:
            raise RevotionNotFoundError(f"Brain not found: {self._brain_mac}")
        if response.status >= 500:
            raise RevotionConnectionError(f"Server error: {response.status}")

        response.raise_for_status()
        return await response.json()

    async def async_get_brain_status(self, mac: str) -> Brain:
        """Get Brain status from the API.

        GET /brain/status/:mac

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            Brain dataclass with status data.

        """
        data = await self._request("GET", f"/brain/status/{mac}")
        return self._parse_brain_status(data)

    async def async_get_inventory(self, mac: str) -> list[Node]:
        """Get Brain inventory (list of paired Nodes) from the API.

        GET /brain/inventory/:mac

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            List of Node dataclasses with capabilities.

        """
        data = await self._request("GET", f"/brain/inventory/{mac}")
        return self._parse_inventory(data)

    async def async_get_node_data(self, mac: str) -> dict[str, Any]:
        """Get node capability data from the API.

        GET /brain/data/:mac/nodes

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            Raw dict response (coordinator in Phase 3 handles mapping).

        """
        return await self._request("GET", f"/brain/data/{mac}/nodes")

    async def async_get_brain_gps(self, mac: str) -> dict[str, Any]:
        """Get the latest GPS payload for a Brain via REST.

        GET /brain/gps/:mac

        Auth: Bearer token (BrainBearer) — same token used for all /brain/* endpoints.
        Requires an active subscription (403 otherwise).

        When GPS data has been recorded, returns a dict mirroring the MQTT gps topic:
            {"UTC": ..., "Lat": ..., "Lon": ..., "Alt": ..., "HDOP": ..., "COG": ..., "Speed": ...}
        When no GPS data has been recorded yet, returns {} — callers must treat {} as
        "no data" and not overwrite existing GPS data with it.

        The 401/403/404/5xx mapping is handled by _request.

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            GPS payload dict, or {} if no GPS recorded yet.

        """
        return await self._request("GET", f"/brain/gps/{mac}")

    async def async_get_sync(self, mac: str) -> dict[str, Any]:
        """Get Brain sync payload including capability config names.

        GET /brain/sync/:mac

        The sync endpoint returns user-configured capability names and images
        that are not available in the inventory endpoint.

        Response format:
            {
                "Nodes": [
                    {"MAC": "aa:bb:...", "cap_index": 0, "name": "Hauptschalter", "image": "switch", ...},
                    ...
                ],
                "Veh": {...},
                "MFC": "..."
            }

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            Raw dict response with Nodes, Veh, and MFC fields.

        """
        return await self._request("GET", f"/brain/sync/{mac}")

    async def async_get_errors(self, mac: str) -> list[dict[str, Any]]:
        """Get the persisted Brain + node error lists.

        GET /brain/error/:mac

        Returns one entry per device that has ever reported errors -- the Brain
        itself ({"MAC": ..., "User": [...], "Backend": [...], "ESPNOW": [...]})
        and each node ({"MAC": ..., "User": [...], "Dev": [...], "Cap_errors":
        [...]}). User-error 4101 in a node entry means the Brain cannot reach
        that node over ESP-NOW (see ERROR_CODE_NODE_NOT_AVAILABLE); an entry
        with an empty/4101-free "User" list means the node recovered. This is
        the persisted counterpart of the MQTT {mac}/error pushes, used to seed
        node reachability after a (re)start.

        Args:
            mac: Brain MAC address (normalized, lowercase, no separators).

        Returns:
            List of error entries; [] for a non-list response.

        """
        data = await self._request("GET", f"/brain/error/{mac}")
        if not isinstance(data, list):
            _LOGGER.warning("Error response is not a list: %s", type(data))
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    @staticmethod
    def _parse_brain_status(data: dict[str, Any]) -> Brain:
        """Parse API brain status response into Brain dataclass.

        Required keys (always present since v2.6.0): isOnline, lastConnection,
        'App Ver', 'Hard Rev'.
        Optional keys: variant (raw int), interface (0=cellular, 1=wifi).

        Note: macAddress and boardName are NOT in the status response —
        they come from config entry data instead.
        """
        return Brain(
            mac_address="",  # Not in status response; set by caller
            name="",  # Not in status response; use config entry name
            firmware_version=data.get("App Ver", ""),
            hardware_revision=data.get("Hard Rev", ""),
            is_online=bool(data.get("isOnline", 0)),
            nodes=[],
            last_connection=data.get("lastConnection"),
            connection_interface=data.get("interface"),
            variant=data.get("variant"),
        )

    @staticmethod
    def _parse_inventory(data: list[dict[str, Any]] | Any) -> list[Node]:
        """Parse API inventory response into list of Node dataclasses.

        API returns keys: 'Node Num', 'MAC', 'App Ver', 'Hard Rev', 'Capabilities'.
        'Capabilities' is a list of capability type integers (e.g. [2, 3, 5]),
        NOT a list of objects. Capability index = position in the list.
        Config data (name, image) arrives separately via MQTT /config topic.

        Battery/HighCurrent handling:
        The API inventory endpoint expands Battery (type 5) and HighCurrent
        (type 8) into repeated entries (e.g. [5,5,5,5,5]) to represent
        current channels. Since our sensor platform creates sub-entities
        from the data blob (cur array), we collapse these back to a single
        capability at index 0. The REST data endpoint only returns one entry
        per Battery/HighCurrent node (at cap_index 0).
        """
        if not isinstance(data, list):
            _LOGGER.warning("Inventory response is not a list: %s (type=%s)", data, type(data))
            return []

        # Capability types that get expanded by the API inventory endpoint
        # but should be collapsed to a single capability at index 0.
        _BATTERY_LIKE_TYPES = {
            CapabilityType.BATTERY,
            CapabilityType.HIGH_CURRENT,
        }

        nodes: list[Node] = []
        for node_data in data:
            # Parse capability type list into Capability objects
            cap_types = node_data.get("Capabilities", [])
            capabilities: list[Capability] = []
            seen_battery_types: set[int] = set()
            for i, cap_type_val in enumerate(cap_types):
                try:
                    cap_type = CapabilityType(int(cap_type_val))
                except (ValueError, TypeError):
                    _LOGGER.info(
                        "Unknown capability type %s on node %s, skipping",
                        cap_type_val,
                        node_data.get("MAC", "?"),
                    )
                    continue

                # Collapse Battery/HighCurrent expansions to single capability
                if cap_type in _BATTERY_LIKE_TYPES:
                    if cap_type.value in seen_battery_types:
                        continue  # Skip duplicate expanded entries
                    seen_battery_types.add(cap_type.value)
                    # Always use index 0 for Battery/HighCurrent
                    capabilities.append(
                        Capability(
                            capability_index=0,
                            capability_type=cap_type,
                            config=CapabilityConfig(),
                        )
                    )
                else:
                    capabilities.append(
                        Capability(
                            capability_index=i,
                            capability_type=cap_type,
                            config=CapabilityConfig(),
                        )
                    )

            nodes.append(
                Node(
                    mac_address=normalize_mac(node_data.get("MAC", "")),
                    firmware_version=node_data.get("App Ver", ""),
                    hardware_revision=node_data.get("Hard Rev", ""),
                    node_type="",
                    node_number=node_data.get("Node Num", 0),
                    capabilities=capabilities,
                )
            )

        _LOGGER.info(
            "Parsed inventory: %d nodes, capabilities: %s",
            len(nodes),
            [(n.node_number, len(n.capabilities)) for n in nodes],
        )

        return nodes

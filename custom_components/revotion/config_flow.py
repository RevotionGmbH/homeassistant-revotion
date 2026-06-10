"""Config flow for the Revotion integration."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from .api_client import (
    InvalidJsonError,
    RevotionApiClient,
    RevotionApiError,
    RevotionAuthError,
    RevotionConnectionError,
    RevotionNotFoundError,
    RevotionSubscriptionError,
)
from .const import (
    CONF_BRAIN_MAC,
    CONF_BRAIN_NAME,
    CONF_SUBSCRIPTION_EXPIRY,
    CONF_SUBSCRIPTION_TYPE,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
)
from .models import format_mac_for_display, normalize_mac

_LOGGER = logging.getLogger(__name__)

JSON_SCHEMA = vol.Schema(
    {
        vol.Required("json_config"): TextSelector(TextSelectorConfig(multiline=True)),
    }
)


def parse_revotion_json(raw: str) -> dict[str, Any]:
    """Parse the JSON configuration from the Revotion Flutter app.

    Expected format:
    {
      "backendAccessData": {
        "token": {"value": "...", "expiry": "2026-12-31T23:59:59.000Z"},
        "subscription": {"type": "PREMIUM", "expiresAt": "2026-12-31T..."}
      },
      "brain": {
        "macAddress": "AA:BB:CC:DD:EE:FF",
        "boardName": "My Camper",
        "brainVersion": "1.2.3"
      }
    }

    Returns flat dict with CONF_* keys for config entry storage.
    Raises InvalidJsonError on any parsing failure.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise InvalidJsonError(f"Invalid JSON: {err.msg} at line {err.lineno}") from err

    try:
        backend = data["backendAccessData"]
        brain = data["brain"]

        return {
            CONF_TOKEN: backend["token"]["value"],
            CONF_TOKEN_EXPIRY: backend["token"]["expiry"],
            CONF_BRAIN_MAC: brain["macAddress"],
            CONF_BRAIN_NAME: brain["boardName"],
            CONF_SUBSCRIPTION_TYPE: backend["subscription"]["type"],
            CONF_SUBSCRIPTION_EXPIRY: backend["subscription"]["expiresAt"],
        }
    except (KeyError, TypeError) as err:
        raise InvalidJsonError(f"Missing required field in JSON: {err}") from err


class RevotionConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Revotion."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._parsed_data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle the initial step: JSON paste."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parsed = parse_revotion_json(user_input["json_config"])
            except InvalidJsonError:
                errors["base"] = "invalid_json"
            else:
                # Set unique_id early for duplicate detection (before API call)
                mac = normalize_mac(parsed[CONF_BRAIN_MAC])
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

                # Validate token via REST API (use original MAC format for API)
                api_mac = parsed[CONF_BRAIN_MAC]
                try:
                    session = async_get_clientsession(self.hass)
                    client = RevotionApiClient(session, parsed[CONF_TOKEN], mac)
                    await client.async_get_brain_status(api_mac)
                except RevotionAuthError:
                    errors["base"] = "invalid_auth"
                except RevotionSubscriptionError:
                    errors["base"] = "no_subscription"
                except RevotionNotFoundError:
                    errors["base"] = "brain_not_found"
                except RevotionConnectionError:
                    errors["base"] = "cannot_connect"
                except RevotionApiError:
                    errors["base"] = "unknown"
                else:
                    self._parsed_data = parsed
                    return await self.async_step_confirm()

        return self.async_show_form(
            step_id="user",
            data_schema=JSON_SCHEMA,
            errors=errors,
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> config_entries.ConfigFlowResult:
        """Handle confirmation step."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._parsed_data[CONF_BRAIN_NAME],
                data=self._parsed_data,
            )

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "brain_name": self._parsed_data[CONF_BRAIN_NAME],
                "brain_mac": format_mac_for_display(self._parsed_data[CONF_BRAIN_MAC]),
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> config_entries.ConfigFlowResult:
        """Handle reauth triggered by ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reauth confirmation with new JSON paste."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                parsed = parse_revotion_json(user_input["json_config"])
            except InvalidJsonError:
                errors["base"] = "invalid_json"
            else:
                # Verify same Brain (MAC must match)
                reauth_entry = self._get_reauth_entry()
                mac = normalize_mac(parsed[CONF_BRAIN_MAC])
                if mac != reauth_entry.unique_id:
                    return self.async_abort(reason="wrong_brain")

                # Validate new token (use original MAC format for API)
                api_mac = parsed[CONF_BRAIN_MAC]
                try:
                    session = async_get_clientsession(self.hass)
                    client = RevotionApiClient(session, parsed[CONF_TOKEN], mac)
                    await client.async_get_brain_status(api_mac)
                except RevotionAuthError:
                    errors["base"] = "invalid_auth"
                except RevotionSubscriptionError:
                    errors["base"] = "no_subscription"
                except RevotionNotFoundError:
                    errors["base"] = "brain_not_found"
                except RevotionConnectionError:
                    errors["base"] = "cannot_connect"
                except RevotionApiError:
                    errors["base"] = "unknown"
                else:
                    _LOGGER.info(
                        "Reauth successful for Brain %s, token expires %s, subscription expires %s",
                        parsed.get(CONF_BRAIN_NAME),
                        parsed.get(CONF_TOKEN_EXPIRY),
                        parsed.get(CONF_SUBSCRIPTION_EXPIRY),
                    )
                    return self.async_update_reload_and_abort(
                        reauth_entry,
                        data_updates=parsed,
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=JSON_SCHEMA,
            errors=errors,
            description_placeholders={
                "brain_name": self._get_reauth_entry().data.get(CONF_BRAIN_NAME, "Unknown"),
            },
        )

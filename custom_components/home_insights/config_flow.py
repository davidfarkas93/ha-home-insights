"""Local-only, single-instance configuration. No credentials are requested."""

from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import DOMAIN


def schema(entities: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("monitored_entities", default=entities): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            ),
        }
    )


def self_reference(hass, entities: list[str]) -> bool:
    registry = er.async_get(hass)
    return any(
        (entry := registry.async_get(entity)) and entry.platform == DOMAIN for entity in entities
    )


class HomeInsightsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure native journal sources and optional explicitly selected entity facts."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            if self_reference(self.hass, user_input.get("monitored_entities", [])):
                errors["base"] = "self_reference"
            else:
                return self.async_create_entry(title="Home Insights", data=user_input)
        return self.async_show_form(step_id="user", data_schema=schema([]), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HomeInsightsOptionsFlow()


class HomeInsightsOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            if self_reference(self.hass, user_input.get("monitored_entities", [])):
                errors["base"] = "self_reference"
            else:
                return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            "monitored_entities", self.config_entry.data.get("monitored_entities", [])
        )
        return self.async_show_form(step_id="init", data_schema=schema(current), errors=errors)

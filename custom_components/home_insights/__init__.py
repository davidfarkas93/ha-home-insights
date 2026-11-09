"""Persistent, local Home Assistant observations and daily facts."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from . import api
from .const import PLATFORMS
from .coordinator import Coordinator

_LOGGER = logging.getLogger(__name__)
type HomeInsightsConfigEntry = ConfigEntry[Coordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    await api.async_setup(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomeInsightsConfigEntry) -> bool:
    coordinator = entry.runtime_data = Coordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as error:
        raise ConfigEntryNotReady("Could not open the local journal") from error
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_close()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: HomeInsightsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HomeInsightsConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_close()
        return True
    return False

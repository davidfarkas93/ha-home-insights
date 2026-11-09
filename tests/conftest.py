"""Use the actual target HA version, with no external devices or API credentials."""

from collections.abc import AsyncGenerator

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, issue_registry


@pytest.fixture
async def hass(tmp_path) -> AsyncGenerator[HomeAssistant]:
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    device_registry.async_setup(instance)
    await device_registry.async_load(instance)
    await entity_registry.async_load(instance)
    await issue_registry.async_load(instance)
    yield instance
    await instance.async_stop(force=True)
    await instance.async_block_till_done()


def config_entry(domain="home_insights", data=None, state=None):
    kwargs = {"state": state} if state is not None else {}
    return ConfigEntry(
        domain=domain,
        title="Home Insights" if domain == "home_insights" else "Test device",
        data=data or {},
        options={},
        source="user",
        version=1,
        minor_version=1,
        unique_id=domain,
        discovery_keys={},
        subentries_data=[],
        **kwargs,
    )

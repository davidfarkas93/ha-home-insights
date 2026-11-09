"""Load the actual custom integration and sensors through HA's entry lifecycle."""

import shutil
from pathlib import Path

import pytest
from homeassistant import core_config
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState
from homeassistant.helpers import (
    area_registry,
    entity,
    entity_registry,
    floor_registry,
    frame,
    label_registry,
    template,
    translation,
)


@pytest.fixture
async def full_hass(hass):
    source = Path(__file__).parents[1] / "custom_components"
    target = Path(hass.config.path("custom_components"))
    await hass.async_add_executor_job(
        lambda: shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    )
    frame.async_setup(hass)
    entity.async_setup(hass)
    template.async_setup(hass)
    translation.async_setup(hass)
    await floor_registry.async_load(hass)
    await area_registry.async_load(hass)
    await label_registry.async_load(hass)
    await core_config.async_process_ha_core_config(
        hass, {"time_zone": "Europe/Budapest", "auth_mfa_modules": []}
    )
    hass.set_state(CoreState.running)
    yield hass


async def test_real_flow_setup_sensor_creation_and_reload(full_hass):
    hass = full_hass
    result = await hass.config_entries.flow.async_init(
        "home_insights", context={"source": "user"}, data={}
    )
    assert result["type"] == "create_entry"
    entry = result["result"]
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    coordinator = entry.runtime_data
    await coordinator.async_flush()
    assert coordinator.data["status"] == "recording"
    registry = entity_registry.async_get(hass)
    entities = [item for item in registry.entities.values() if item.platform == "home_insights"]
    assert len(entities) == 3
    assert all(hass.states.get(item.entity_id) is not None for item in entities)

    from homeassistant.components import persistent_notification

    persistent_notification.async_create(hass, "Keep this episode", "Test", "persist")
    await coordinator.async_flush()
    episode = coordinator.data["open_episodes"][0]["id"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator.closed
    assert entry.runtime_data is not coordinator
    await entry.runtime_data.async_flush()
    assert entry.runtime_data.data["open_episodes"][0]["id"] == episode
    current = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert current.closed

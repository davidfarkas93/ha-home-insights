"""Small status entities; full history is paginated through the journal API."""

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, VERSION


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            InsightsSensor(entry, "status", SensorDeviceClass.ENUM),
            InsightsSensor(entry, "open_count", None),
            InsightsSensor(entry, "last_observed_at", SensorDeviceClass.TIMESTAMP),
            WeatherAdviceSensor(entry),
        ]
    )


class InsightsSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key: str, device_class) -> None:
        self.coordinator = entry.runtime_data
        self.key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Home Insights",
            manufacturer="Home Insights",
            model="Native observation journal",
            sw_version=VERSION,
        )
        if key == "status":
            self._attr_options = ["starting", "recording", "partial", "error", "stopped"]

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.coordinator.subscribe(self._changed))

    @callback
    def _changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if self.key == "status":
            return True
        return (
            self.coordinator.started
            and not self.coordinator.closed
            and not self.coordinator.failed
            and self.coordinator.data.get(self.key) is not None
        )

    @property
    def native_value(self):
        if self.key == "status" and self.coordinator.failed:
            return "error"
        value = self.coordinator.data.get(self.key)
        if self.key == "last_observed_at":
            return datetime.fromisoformat(value) if value else None
        return value

    @property
    def extra_state_attributes(self):
        if self.key != "status":
            return None
        data = self.coordinator.data
        return {key: data.get(key) for key in ("policy_hash", "event_count")}


class WeatherAdviceSensor(SensorEntity):
    """Small household-facing text; raw forecast/attempt data stays in native Store."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "weather_advice"
    _attr_icon = "mdi:hanger"

    def __init__(self, entry):
        self.coordinator = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_weather_advice"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self):
        self.async_on_remove(self.coordinator.subscribe(self.async_write_ha_state))

    @property
    def available(self):
        return not self.coordinator.closed and self.coordinator.weather_advice.current()

    @property
    def native_value(self):
        return self.coordinator.weather_advice.data.get("text") if self.available else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.weather_advice.data
        return {
            key: data.get(key)
            for key in (
                "status",
                "day",
                "generated_at",
                "valid_until",
                "weather_entity",
                "error",
                "forecast_detail_version",
                "periods",
                "hourly_status",
            )
        }

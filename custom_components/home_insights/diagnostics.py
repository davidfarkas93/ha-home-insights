"""Diagnostics contain only health metadata, never journal text or entity values."""

from .const import VERSION


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "version": VERSION,
        "status": "error" if coordinator.failed else data["status"],
        "queue_size": coordinator.queue.qsize(),
        "monitored_entity_count": len(coordinator.entities),
        "open_count": data.get("open_count"),
        "event_count": data.get("event_count"),
        "coverage_gap_count": len(data.get("coverage_gaps", [])),
    }

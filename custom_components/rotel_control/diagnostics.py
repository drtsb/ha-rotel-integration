"""Diagnostics support for the Rotel integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import RotelCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data
    return {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": f"{entry.version}.{entry.minor_version}",
        },
        "connection": {
            "host": coordinator.api.host,
            "port": coordinator.api.port,
            "connected": coordinator.api.connected,
            "last_update_success": coordinator.last_update_success,
            "consecutive_failures": coordinator.consecutive_failures,
        },
        "profile": {
            "key": coordinator.model.key,
            "name": coordinator.model.name,
            "rbc": coordinator.model.rbc,
            "volume_min_db": coordinator.model.volume_min_db,
            "volume_max_db": coordinator.model.volume_max_db,
            "volume_step_db": coordinator.model.volume_step_db,
            "volume_scale": coordinator.model.volume_scale.value,
            "inputs": [item.value for item in coordinator.model.inputs],
        },
        "state": {
            "power": data.power,
            "volume_db": data.volume_db,
            "mute": data.mute,
            "source": data.source.value if data.source else None,
            "record_source": data.record_source.value if data.record_source else None,
            "firmware": data.firmware,
            "model": data.device_model,
            "unsupported_queries": sorted(data.unsupported),
        },
        "repr": repr(coordinator.api),
        "asdict": asdict(data),
    }

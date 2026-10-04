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
    data = coordinator.known_state
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
            "tone_control": coordinator.model.tone_control,
            "speaker_groups": list(coordinator.model.speaker_groups),
            "dimmer": coordinator.model.dimmer,
        },
        "state": {
            "power": data.power,
            "volume_db": data.volume_db,
            "volume_raw": data.volume_raw,
            "mute": data.mute,
            "source": data.source.value if data.source else None,
            "record_source": data.record_source.value if data.record_source else None,
            "bass_db": data.bass_db,
            "treble_db": data.treble_db,
            "balance": data.balance,
            "tone_bypass": data.tone_bypass,
            "speaker_a": data.speaker_a,
            "speaker_b": data.speaker_b,
            "dimmer": data.dimmer,
            "firmware": data.firmware,
            "model": data.device_model,
            "unsupported_queries": sorted(data.unsupported),
        },
        "reported": coordinator.api.values,
        "repr": repr(coordinator.api),
        "asdict": asdict(data),
    }

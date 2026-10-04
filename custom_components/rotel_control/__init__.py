"""The Rotel Amplifier integration.

Every entry gets its own :class:`~custom_components.rotel_control.coordinator.RotelCoordinator`
(owning one TCP connection) and shares it between the entity platforms.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .api import RotelApi
from .const import (
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    DEFAULT_MODEL_PROFILE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import RotelConfigEntry, RotelCoordinator
from .protocol import get_model


async def async_setup_entry(hass: HomeAssistant, entry: RotelConfigEntry) -> bool:
    """Set up Rotel from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)
    # Options win over data so the options flow can retune a running device.
    model_key: str = entry.options.get(
        CONF_MODEL_PROFILE,
        entry.data.get(CONF_MODEL_PROFILE, DEFAULT_MODEL_PROFILE),
    )
    poll_interval: int = entry.options.get(
        CONF_POLL_INTERVAL,
        entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    )

    model = get_model(model_key)
    api = RotelApi(host, port, model)
    coordinator = RotelCoordinator(hass, entry, api, model, poll_interval)

    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RotelConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: RotelCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.api.async_disconnect()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)

"""Number platform: volume in decibel."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RotelConfigEntry, RotelCoordinator
from .entity import RotelEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RotelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rotel volume number from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RotelVolumeNumber(coordinator, entry)])


ROTEL_VOLUME = NumberEntityDescription(
    key="volume_db",
    translation_key="volume_db",
    native_unit_of_measurement="dB",
)


class RotelVolumeNumber(RotelEntity, NumberEntity):
    """Volume of the amplifier expressed in decibel.

    The media player exposes the usual 0..100 % slider; this entity is the
    precise equivalent, matching the 0.5 dB granularity of the front panel.
    """

    entity_description = ROTEL_VOLUME

    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the number entity."""
        super().__init__(coordinator, entry, ROTEL_VOLUME)
        model = coordinator.model
        self._attr_native_min_value = model.volume_min_db
        self._attr_native_max_value = model.volume_max_db
        self._attr_native_step = model.volume_step_db

    @property
    def native_value(self) -> float | None:
        """Return the current volume in dB."""
        return self.data.volume_db

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume in dB."""
        await self.coordinator.async_set_volume(value)

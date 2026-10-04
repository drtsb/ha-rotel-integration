"""Base entity for the Rotel integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import RotelConfigEntry, RotelCoordinator, RotelData


class RotelEntity(CoordinatorEntity[RotelCoordinator]):
    """Common behaviour of every Rotel entity.

    State is never cached in instance attributes: every property reads
    :attr:`RotelCoordinator.data`, so a coordinator update is enough to
    refresh all platforms.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
        description: EntityDescription,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-{description.key}"

    @property
    def data(self) -> RotelData:
        """Last known state of the amplifier, empty before the first update."""
        return self.coordinator.known_state

    @property
    def available(self) -> bool:
        """Entities are available as long as the amplifier answers."""
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> DeviceInfo:
        """Group all entities of one amplifier under a single device."""
        api = self.coordinator.api
        return DeviceInfo(
            identifiers={(DOMAIN, api.host)},
            manufacturer="Rotel",
            model=self.coordinator.model.name,
            name=self._entry.title,
            configuration_url=api.base_url,
            sw_version=self.data.firmware,
        )

"""Select platform: input and record-source selection."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
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
    """Set up the Rotel selects from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [RotelInputSelect(coordinator, entry)]
    if coordinator.model.record_inputs:
        entities.append(RotelRecordInputSelect(coordinator, entry))
    async_add_entities(entities)


ROTEL_INPUT = SelectEntityDescription(
    key="input",
    translation_key="input",
)


class RotelInputSelect(RotelEntity, SelectEntity):
    """Input selector, mirroring the media player source list."""

    entity_description = ROTEL_INPUT

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the input select."""
        super().__init__(coordinator, entry, ROTEL_INPUT)
        self._attr_options = [item.name for item in coordinator.model.inputs]

    @property
    def current_option(self) -> str | None:
        """Return the selected input."""
        return self.data.source.name if self.data.source else None

    async def async_select_option(self, option: str) -> None:
        """Select an input."""
        await self.coordinator.async_set_source(option)


ROTEL_RECORD_INPUT = SelectEntityDescription(
    key="record_input",
    translation_key="record_input",
)


class RotelRecordInputSelect(RotelEntity, SelectEntity):
    """Record source of processors such as the RCX-1500/1570."""

    entity_description = ROTEL_RECORD_INPUT
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the record input select."""
        super().__init__(coordinator, entry, ROTEL_RECORD_INPUT)
        self._attr_options = [item.name for item in coordinator.model.record_inputs]

    @property
    def current_option(self) -> str | None:
        """Return the selected record source."""
        record = self.data.record_source
        return record.name if record else None

    async def async_select_option(self, option: str) -> None:
        """Select the record source."""
        await self.coordinator.async_set_record_source(option)

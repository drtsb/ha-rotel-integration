"""Select platform: input, record-source and display-brightness selection.

The input selector is the bridge between the Matter world — where a ModeSelect
device exposes human-readable option labels — and the Rotel protocol, where a
command is a short token like ``opt1`` or ``coax1``. Every option the user sees
in Google Home is the ``RotelInput.name`` of the catalogue entry; the value the
device receives is ``RotelInput.value``. The coordinator resolves both names and
raw firmware tokens through the same ``match_input`` helper the media player
uses, so a firmware that answers ``source=aux1`` still lands on the ``AUX``
option.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DIMMER_OPTIONS, DOMAIN
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
    if coordinator.model.dimmer:
        entities.append(RotelDimmerSelect(coordinator, entry))
    async_add_entities(entities)


ROTEL_INPUT = SelectEntityDescription(
    key="input",
    translation_key="input",
)


class RotelInputSelect(RotelEntity, SelectEntity):
    """Input selector, mirrored on the media player's source list.

    The options are the human-readable names of the catalogue entries
    (``Phono``, ``Optical 1``, ``USB Audio`` …), not the protocol tokens the
    device speaks. Matter exposes this entity as a ModeSelect device, and
    Google Home renders those names as the selectable modes, so a voice
    command like "set the amplifier to USB Audio" reaches the device as
    ``usb!``.
    """

    entity_description = ROTEL_INPUT
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the input select."""
        super().__init__(coordinator, entry, ROTEL_INPUT)
        self._attr_options = [item.name for item in coordinator.model.inputs]

    @property
    def available(self) -> bool:
        """Available while the amplifier answers and reports an input."""
        return super().available and self.data.source is not None

    @property
    def current_option(self) -> str | None:
        """Return the human-readable name of the selected input."""
        source = self.data.source
        return source.name if source is not None else None

    async def async_select_option(self, option: str) -> None:
        """Translate a human-readable name back to a protocol token.

        ``option`` comes from the Matter ModeSelect list, so it is a catalogue
        name. The coordinator resolves it again through ``match_input`` — the
        same path the media player uses — so an alias a firmware reports still
        selects the right entry, and an unknown name raises a clear error
        instead of sending a raw string to the device.
        """
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


ROTEL_DISPLAY_DIMMER = SelectEntityDescription(
    key="display_dimmer",
    translation_key="display_dimmer",
    icon="mdi:brightness-5",
)


class RotelDimmerSelect(RotelEntity, SelectEntity):
    """Brightness of the front display.

    The device takes one command per level (``dimmer_0!`` is the brightest,
    ``dimmer_6!`` the dimmest), so the levels are offered as options instead of
    a slider. Their names are translated through
    ``entity.select.display_dimmer.state``.
    """

    entity_description = ROTEL_DISPLAY_DIMMER

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the display dimmer select."""
        super().__init__(coordinator, entry, ROTEL_DISPLAY_DIMMER)
        self._attr_options = list(DIMMER_OPTIONS)

    @property
    def current_option(self) -> str | None:
        """Return the brightness level currently set on the device."""
        level = self.data.dimmer
        return None if level is None else str(level)

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports the brightness."""
        return super().available and self.data.dimmer is not None

    async def async_select_option(self, option: str) -> None:
        """Set the display brightness."""
        await self.coordinator.async_set_dimmer(int(option))


__all__ = (
    "ROTEL_DISPLAY_DIMMER",
    "ROTEL_INPUT",
    "ROTEL_RECORD_INPUT",
    "RotelDimmerSelect",
    "RotelInputSelect",
    "RotelRecordInputSelect",
)

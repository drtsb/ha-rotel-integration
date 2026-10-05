"""Number platform: the tone controls of the front panel.

The volume deliberately has no number entity. It lives on the media player,
which runs on the volume scale of the device itself (the raw 0..96 of the front
panel for the units that report one), and a second volume in decibel next to it
would be a second source of truth for one setting. Bass, treble and balance do
mirror the tone block of the front panel.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BALANCE_MAX,
    BALANCE_MIN,
    BALANCE_STEP,
    DOMAIN,
    TONE_MAX_DB,
    TONE_MIN_DB,
    TONE_STEP_DB,
)
from .coordinator import RotelConfigEntry, RotelCoordinator
from .entity import RotelEntity
from .protocol import RotelCommand


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RotelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rotel numbers from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NumberEntity] = []
    if coordinator.model.tone_control:
        entities.append(RotelToneNumber(coordinator, entry, ROTEL_BASS))
        entities.append(RotelToneNumber(coordinator, entry, ROTEL_TREBLE))
        entities.append(RotelBalanceNumber(coordinator, entry))
    async_add_entities(entities)


ROTEL_BASS = NumberEntityDescription(
    key="bass_db",
    translation_key="bass",
    native_unit_of_measurement="dB",
    icon="mdi:equalizer",
)

ROTEL_TREBLE = NumberEntityDescription(
    key="treble_db",
    translation_key="treble",
    native_unit_of_measurement="dB",
    icon="mdi:equalizer",
)

#: The tone control each description drives.
_TONE_COMMANDS: dict[str, RotelCommand] = {
    ROTEL_BASS.key: RotelCommand.BASS,
    ROTEL_TREBLE.key: RotelCommand.TREBLE,
}


class RotelToneNumber(RotelEntity, NumberEntity):
    """Bass or treble of the tone block.

    The device only accepts whole decibel steps, so the slider is limited to
    the ``TONE_MIN_DB``..``TONE_MAX_DB`` range the front panel offers.
    """

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = float(TONE_MIN_DB)
    _attr_native_max_value = float(TONE_MAX_DB)
    _attr_native_step = float(TONE_STEP_DB)

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
        description: NumberEntityDescription,
    ) -> None:
        """Initialise the tone control."""
        super().__init__(coordinator, entry, description)

    @property
    def native_value(self) -> float | None:
        """Return the current level in dB, ``None`` until the device reports it."""
        value: int | None = getattr(self.data, self.entity_description.key)
        return None if value is None else float(value)

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports this control.

        Bass and treble also go unavailable while the tone block is bypassed.
        Rotel leaves that block out of the signal path at the factory and drops
        the commands it is given while it is, so a slider that cannot change
        anything should not look like it can — the value stays, and comes back
        as soon as the bypass is switched off.

        The balance is a control of its own and is not part of the tone block,
        which is why :class:`RotelBalanceNumber` does not share this rule.
        """
        if not (super().available and self.native_value is not None):
            return False
        return self.data.tone_bypass is not True

    async def async_set_native_value(self, value: float) -> None:
        """Set the level in dB."""
        command = _TONE_COMMANDS[self.entity_description.key]
        await self.coordinator.async_set_tone(command, value)


ROTEL_BALANCE = NumberEntityDescription(
    key="balance",
    translation_key="balance",
    icon="mdi:scale-balance",
)


class RotelBalanceNumber(RotelEntity, NumberEntity):
    """Channel balance, ``0`` being centred.

    The device reports the balance as ``L01``..``L15``/``R01``..``R15`` steps,
    which this entity exposes as ``-15``..``+15`` so the slider is centred.
    """

    entity_description = ROTEL_BALANCE

    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = float(BALANCE_MIN)
    _attr_native_max_value = float(BALANCE_MAX)
    _attr_native_step = float(BALANCE_STEP)

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the balance control."""
        super().__init__(coordinator, entry, ROTEL_BALANCE)

    @property
    def native_value(self) -> float | None:
        """Return the balance, negative meaning "to the left"."""
        value: int | None = self.data.balance
        return None if value is None else float(value)

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports the balance."""
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        """Set the balance."""
        await self.coordinator.async_set_balance(value)


__all__ = (
    "ROTEL_BALANCE",
    "ROTEL_BASS",
    "ROTEL_TREBLE",
    "RotelBalanceNumber",
    "RotelToneNumber",
)
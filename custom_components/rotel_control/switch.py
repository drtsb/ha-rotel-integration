"""Switch platform: speaker groups and the tone bypass.

Both are documented as toggling speaker output (``speaker_a!``) and bypassing
the tone block (``bypass_on!``); the explicit ``speaker_a_on!``/
``speaker_a_off!`` commands are used instead, because they do not depend on the
state the device happens to be in.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
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
    """Set up the Rotel switches from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []
    if coordinator.model.speaker_groups:
        entities.append(
            RotelSpeakerSwitch(coordinator, entry, ROTEL_SPEAKERS_A, "a")
        )
        if "b" in coordinator.model.speaker_groups:
            entities.append(
                RotelSpeakerSwitch(coordinator, entry, ROTEL_SPEAKERS_B, "b")
            )
    if coordinator.model.tone_bypass:
        entities.append(RotelToneBypassSwitch(coordinator, entry))
    async_add_entities(entities)


ROTEL_SPEAKERS_A = SwitchEntityDescription(
    key="speakers_a",
    translation_key="speakers_a",
    icon="mdi:speaker",
)

ROTEL_SPEAKERS_B = SwitchEntityDescription(
    key="speakers_b",
    translation_key="speakers_b",
    icon="mdi:speaker",
)

class RotelSpeakerSwitch(RotelEntity, SwitchEntity):
    """One of the switchable speaker groups."""

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
        description: SwitchEntityDescription,
        group: str,
    ) -> None:
        """Initialise a speaker group switch."""
        super().__init__(coordinator, entry, description)
        self._group = group

    @property
    def is_on(self) -> bool | None:
        """True while this speaker group is switched on."""
        return getattr(self.data, f"speaker_{self._group}")

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports the speaker state."""
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the speaker group on."""
        await self.coordinator.async_set_speaker(self._group, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the speaker group off."""
        await self.coordinator.async_set_speaker(self._group, False)


ROTEL_TONE_BYPASS = SwitchEntityDescription(
    key="tone_bypass",
    translation_key="tone_bypass",
    icon="mdi:power-plug-off",
)


class RotelToneBypassSwitch(RotelEntity, SwitchEntity):
    """The tone bypass of the front panel.

    Firmware before the ``bypass`` naming reports the same switch under
    ``tone``, where the sense of the answer is inverted (``tone_on!`` is the
    counterpart of ``bypass_off!``). The integration asks for both and uses
    whichever the device answers, translating in both directions, so this
    switch means the same thing — the tone block out of the signal path — on
    either generation.

    Rotel leaves this bypassed at the factory, and an amplifier that has the
    tone block out of the signal path drops bass and treble commands, so this
    switch has to be off before the tone controls do anything.
    """

    entity_description = ROTEL_TONE_BYPASS

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the tone bypass switch."""
        super().__init__(coordinator, entry, ROTEL_TONE_BYPASS)

    @property
    def is_on(self) -> bool | None:
        """True while the tone block is bypassed."""
        return self.data.tone_bypass

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports the bypass state."""
        return super().available and self.is_on is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Bypass the tone block."""
        await self.coordinator.async_set_tone_bypass(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Use the tone controls again."""
        await self.coordinator.async_set_tone_bypass(False)


__all__ = (
    "ROTEL_SPEAKERS_A",
    "ROTEL_SPEAKERS_B",
    "ROTEL_TONE_BYPASS",
    "RotelSpeakerSwitch",
    "RotelToneBypassSwitch",
)
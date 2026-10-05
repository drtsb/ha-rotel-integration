"""Media player platform for Rotel amplifiers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityDescription,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RotelConfigEntry, RotelCoordinator
from .entity import RotelEntity
from .protocol import volume_from_payload, volume_payload_range, volume_to_payload


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RotelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rotel media player from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RotelMediaPlayer(coordinator, entry)])


ROTEL_MEDIA_PLAYER = MediaPlayerEntityDescription(
    key="amplifier",
    translation_key="amplifier",
)

#: Home Assistant shows a volume as 0..1 (0..100 %) whatever the device counts.
#: A device scale that starts at zero is shown as the numbers of the device
#: itself, so setting 0.54 sends position 54 of a 0..96 front panel scale and
#: the slider reads the same value as the amplifier display. That leaves the
#: part of the slider above the highest position the device has unused — the
#: price of a slider that speaks the same language as the front panel instead
#: of reporting one percent less for every position it does not have.
SLIDER_STEPS = 100.0


class RotelMediaPlayer(RotelEntity, MediaPlayerEntity):
    """The amplifier itself: power, volume, mute and input selection.

    The volume is the only control of the amplifier shown here. It runs on the
    scale of the device itself — the raw 0..96 of the front panel for the units
    that report one, the decibel range for the processors that report decibels
    — which Home Assistant sees as the usual 0..1 fraction. Decibels are never
    a second, parallel volume control: a number entity in dB alongside this
    slider would only invite setting one and reading the other.
    """

    entity_description = ROTEL_MEDIA_PLAYER

    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the media player."""
        super().__init__(coordinator, entry, ROTEL_MEDIA_PLAYER)

    # --- volume scale ----------------------------------------------------

    @property
    def _scale(self) -> tuple[float, float]:
        """Lowest and highest position of the volume scale of the amplifier."""
        return volume_payload_range(self.coordinator.model)

    @property
    def _slider(self) -> tuple[float, float]:
        """Range of the slider that maps onto the volume scale.

        A scale that counts from zero is shown as its own numbers, so the
        slider and the amplifier display the same value. A scale of decibel
        values has no zero position to start from and is spread over its own
        range instead.
        """
        lowest, highest = self._scale
        if lowest == 0:
            return 0.0, SLIDER_STEPS
        return lowest, highest

    def _volume_at_position(self, position: float) -> float:
        """Return the volume the amplifier holds at ``position`` of its scale.

        The amplifier counts whole positions of its own scale, so the target
        is rounded onto the nearest one and clamped into the range the device
        actually has — a position outside it is a value the device ignores.
        """
        lowest, highest = self._scale
        wanted = min(max(round(position), lowest), highest)
        return volume_from_payload(wanted, self.coordinator.model)

    def _position_at_volume(self, volume: float) -> float:
        """Return the slider position the 0..1 ``volume`` stands for."""
        lowest, highest = self._slider
        return lowest + volume * (highest - lowest)

    # --- state -----------------------------------------------------------

    @property
    def state(self) -> MediaPlayerState:
        """Return the playback state derived from the power state."""
        if self.data.power:
            return MediaPlayerState.ON
        return MediaPlayerState.OFF

    @property
    def is_volume_muted(self) -> bool | None:
        """Return the mute state."""
        return self.data.mute

    @property
    def volume_level(self) -> float | None:
        """Return the volume as the 0..1 fraction Home Assistant expects."""
        if (volume := self.data.volume_db) is None:
            return None
        lowest, highest = self._slider
        position = float(volume_to_payload(volume, self.coordinator.model))
        return round((position - lowest) / (highest - lowest), 3)

    @property
    def volume_step(self) -> float:
        """Step used by volume_up/volume_down: one position of the scale."""
        lowest, highest = self._scale
        return 1 / (highest - lowest)

    @property
    def source(self) -> str | None:
        """Currently selected input."""
        return self.data.source.name if self.data.source else None

    @property
    def source_list(self) -> list[str]:
        """Inputs this model supports."""
        return [item.name for item in self.coordinator.model.inputs]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the volume as the device reported it plus the tone controls.

        Power, volume and input are the job of the media player itself;
        ``volume_raw`` is kept because it is the one value a user can compare
        with the display of the amplifier, and the values below it are what the
        switch and select entities act on, which makes a dashboard readable at
        a glance.
        """
        data = self.data
        attributes: dict[str, Any] = {}
        if data.volume_raw is not None:
            attributes["volume_raw"] = data.volume_raw
        if (record := data.record_source) and self.coordinator.model.record_inputs:
            attributes["record_source"] = record.name
        model = self.coordinator.model
        if model.tone_control:
            attributes["bass_db"] = data.bass_db
            attributes["treble_db"] = data.treble_db
            attributes["balance"] = data.balance
        if model.tone_bypass:
            attributes["tone_bypass"] = data.tone_bypass
        if model.speaker_groups:
            attributes["speakers_a"] = data.speaker_a
            attributes["speakers_b"] = data.speaker_b
        if model.dimmer:
            attributes["display_dimmer"] = data.dimmer
        return attributes

    # --- commands --------------------------------------------------------

    async def async_turn_on(self, **kwargs: bool) -> None:
        """Power the amplifier on."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs: bool) -> None:
        """Power the amplifier to standby."""
        await self.coordinator.async_set_power(False)

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the volume from the 0.0..1.0 fraction Home Assistant uses.

        The fraction is read as a position of the scale of the amplifier. On a
        unit with the 0..96 front panel scale, 0.54 is position 54 — the number
        the amplifier displays itself — so the amplifier always receives a
        whole number of its own scale instead of a fraction of it.
        """
        await self.coordinator.async_set_volume(
            self._volume_at_position(self._position_at_volume(volume))
        )

    async def async_volume_up(self, **kwargs: bool) -> None:
        """Raise the volume by one position of the device scale."""
        await self._async_step_volume(1)

    async def async_volume_down(self, **kwargs: bool) -> None:
        """Lower the volume by one position of the device scale."""
        await self._async_step_volume(-1)

    async def _async_step_volume(self, direction: int) -> None:
        """Move the volume by ``direction`` positions of the device scale."""
        if (current := self.data.volume_db) is None:
            # Nothing known yet: ask for a snapshot and retry once.
            await self.coordinator.async_request_refresh()
            if (current := self.data.volume_db) is None:
                return
        model = self.coordinator.model
        position = float(volume_to_payload(current, model))
        await self.coordinator.async_set_volume(
            self._volume_at_position(position + direction)
        )

    async def async_mute_volume(self, mute: bool, **kwargs: bool) -> None:
        """Mute or unmute the amplifier.

        Named ``async_mute_volume`` because that is the method the
        ``media_player.volume_mute`` service dispatches to.
        """
        await self.coordinator.async_set_mute(mute)

    async def async_select_source(self, source: str) -> None:
        """Select an input."""
        await self.coordinator.async_set_source(source)

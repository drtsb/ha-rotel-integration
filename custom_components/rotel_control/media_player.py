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
from .protocol import clamp_volume, snap_volume


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


class RotelMediaPlayer(RotelEntity, MediaPlayerEntity):
    """The amplifier itself: power, volume, mute and input selection."""

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
        self._volume_step = (
            coordinator.model.volume_step_db / coordinator.model.volume_range_db
        )

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
        if (volume_db := self.data.volume_db) is None:
            return None
        model = self.coordinator.model
        return round(
            (clamp_volume(volume_db, model) - model.volume_min_db)
            / model.volume_range_db,
            3,
        )

    @property
    def volume_step(self) -> float:
        """Step used by volume_up/volume_down (a single device step)."""
        return self._volume_step

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
        """Expose the raw volume, the record source and the tone controls.

        Power, volume and input are the job of the media player itself; the
        values below are what the number, switch and select entities act on,
        and having them here too makes a dashboard readable at a glance.
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
        """Set the volume from a 0..1 fraction."""
        model = self.coordinator.model
        target_db = model.volume_min_db + volume * model.volume_range_db
        await self.coordinator.async_set_volume(snap_volume(target_db, model))

    async def async_volume_up(self, **kwargs: bool) -> None:
        """Raise the volume by one device step."""
        await self._async_step_volume(1)

    async def async_volume_down(self, **kwargs: bool) -> None:
        """Lower the volume by one device step."""
        await self._async_step_volume(-1)

    async def _async_step_volume(self, direction: int) -> None:
        """Move the volume by ``direction`` device steps."""
        model = self.coordinator.model
        if (current := self.data.volume_db) is None:
            # Nothing known yet: ask for a snapshot and retry once.
            await self.coordinator.async_request_refresh()
            if (current := self.data.volume_db) is None:
                return
        target = snap_volume(
            current + direction * model.volume_step_db, model
        )
        await self.coordinator.async_set_volume(target)

    async def async_mute_volume(self, mute: bool, **kwargs: bool) -> None:
        """Mute or unmute the amplifier.

        Named ``async_mute_volume`` because that is the method the
        ``media_player.volume_mute`` service dispatches to.
        """
        await self.coordinator.async_set_mute(mute)

    async def async_select_source(self, source: str) -> None:
        """Select an input."""
        await self.coordinator.async_set_source(source)

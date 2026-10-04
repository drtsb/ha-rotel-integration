"""Data update coordinator for the Rotel integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    RotelApi,
    RotelApiConnectionError,
    RotelApiError,
    RotelApiProtocolError,
    RotelStatus,
)
from .const import DEFAULT_POLL_INTERVAL, DOMAIN
from .protocol import RotelInput, RotelModel, get_model

LOGGER: logging.Logger = logging.getLogger(__package__)


@dataclass(frozen=True, slots=True)
class RotelData:
    """Immutable state of an amplifier, as last seen by the coordinator."""

    power: bool | None = None
    volume_db: float | None = None
    mute: bool | None = None
    source: RotelInput | None = None
    record_source: RotelInput | None = None
    firmware: str | None = None
    device_model: str | None = None
    unsupported: frozenset[str] = frozenset()

    @classmethod
    def from_status(cls, status: RotelStatus) -> RotelData:
        """Build entity data from a raw API snapshot."""
        return cls(
            power=status.power,
            volume_db=status.volume_db,
            mute=status.mute,
            source=status.source,
            record_source=status.record_source,
            firmware=status.firmware,
            device_model=status.model,
            unsupported=status.unsupported,
        )

    def apply(
        self,
        *,
        power: bool | None = None,
        volume_db: float | None = None,
        mute: bool | None = None,
        source: RotelInput | None = None,
        record_source: RotelInput | None = None,
    ) -> RotelData:
        """Return a copy with the given fields replaced.

        Only fields that are not ``None`` are replaced, which lets callers
        optimistically update a single property after a command while keeping
        the values still unknown to the device.
        """
        return replace(
            self,
            power=self.power if power is None else power,
            volume_db=self.volume_db if volume_db is None else volume_db,
            mute=self.mute if mute is None else mute,
            source=self.source if source is None else source,
            record_source=self.record_source if record_source is None else record_source,
        )


RotelConfigEntry: TypeAlias = ConfigEntry[RotelApi]


class RotelCoordinator(DataUpdateCoordinator[RotelData]):
    """Poll an amplifier and share the result between all platforms."""

    config_entry: RotelConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RotelConfigEntry,
        api: RotelApi,
        model: RotelModel,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} ({config_entry.title})",
            update_interval=timedelta(seconds=poll_interval),
            always_update=False,
        )
        self.api = api
        self.model = model
        #: Consecutive failed polls, surfaced through diagnostics.
        self.consecutive_failures = 0

    async def _async_setup(self) -> None:
        """Open the connection once before the first poll."""
        try:
            await self.api.async_connect()
        except RotelApiError as err:
            # Do not abort setup: Home Assistant prefers unavailable entities
            # over a failing config entry, so the first refresh decides.
            LOGGER.warning("Initial connection to %s failed: %s", self.api.host, err)

    async def _async_update_data(self) -> RotelData:
        """Fetch the state of the amplifier."""
        try:
            status = await self.api.async_get_status()
        except RotelApiConnectionError as err:
            self.consecutive_failures += 1
            raise UpdateFailed(f"Connection to {self.api.host} failed: {err}") from err
        except RotelApiProtocolError as err:
            self.consecutive_failures += 1
            raise UpdateFailed(
                f"Unexpected reply from {self.api.host}: {err}"
            ) from err
        except RotelApiError as err:  # pragma: no cover - defensive
            self.consecutive_failures += 1
            raise UpdateFailed(f"Error talking to {self.api.host}: {err}") from err

        self.consecutive_failures = 0

        if not status.has_state:
            raise UpdateFailed(
                f"{self.api.host} did not report its power state; is the port "
                f"{self.api.port} a Rotel control port?"
            )

        if status.unsupported:
            LOGGER.debug(
                "%s does not answer: %s", self.model.name, ", ".join(status.unsupported)
            )
        return RotelData.from_status(status)

    # --- helpers for the entity platforms --------------------------------

    async def async_set_power(self, power: bool) -> None:
        """Switch the amplifier on/off and refresh."""
        await self.api.async_set_power(power)
        self.async_set_updated_data(
            self.data.apply(power=power, mute=False if power else self.data.mute)
        )
        await self.async_request_refresh()

    async def async_set_volume(self, volume_db: float) -> None:
        """Set the volume and refresh."""
        applied = await self.api.async_set_volume(volume_db)
        self.async_set_updated_data(self.data.apply(volume_db=applied))
        await self.async_request_refresh()

    async def async_set_mute(self, mute: bool) -> None:
        """Mute/unmute and refresh."""
        await self.api.async_set_mute(mute)
        self.async_set_updated_data(self.data.apply(mute=mute))
        await self.async_request_refresh()

    async def async_set_source(self, source: str) -> None:
        """Select an input and refresh."""
        resolved = await self.api.async_set_source(source)
        self.async_set_updated_data(self.data.apply(source=resolved))
        await self.async_request_refresh()

    async def async_set_record_source(self, source: str) -> None:
        """Select the record input and refresh."""
        resolved = await self.api.async_set_record_source(source)
        self.async_set_updated_data(self.data.apply(record_source=resolved))
        await self.async_request_refresh()


__all__ = ("RotelConfigEntry", "RotelCoordinator", "RotelData", "get_model")

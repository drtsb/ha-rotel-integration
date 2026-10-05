"""Data update coordinator for the Rotel integration.

The coordinator polls the amplifier, but it does not rely on the poll: a task
listens to what the device reports on its own, so the entities follow the front
panel (or the Rotel app, or a remote) immediately. Every change the
coordinator sees — pushed or polled — is announced on the event bus, which is
what makes automations possible without waiting for the next poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import timedelta
from typing import Any, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    VOLUME,
    RotelApi,
    RotelApiConnectionError,
    RotelApiError,
    RotelApiProtocolError,
    RotelStatus,
)
from .const import (
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_COMMAND_RECEIVED,
    ORIGIN_COMMAND,
    ORIGIN_POLL,
    ORIGIN_PUSH,
)
from .protocol import RotelCommand, RotelInput, RotelModel

LOGGER: logging.Logger = logging.getLogger(__package__)

#: Attributes that are not a state of the amplifier and therefore never
#: reported as a change: the set of unanswered queries moves whenever a
#: firmware answers something new.
_QUIET_ATTRIBUTES: frozenset[str] = frozenset({"unsupported"})


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
    #: Volume exactly as the device reported it (front panel scale).
    volume_raw: str | None = None
    #: Tone block in dB, and the balance in L01..L15/R01..R15 steps.
    bass_db: int | None = None
    treble_db: int | None = None
    balance: int | None = None
    #: True while the tone block is bypassed.
    tone_bypass: bool | None = None
    #: Speaker groups that are switched on.
    speaker_a: bool | None = None
    speaker_b: bool | None = None
    #: Front display brightness, ``0`` is the brightest.
    dimmer: int | None = None
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
            volume_raw=status.volume_raw,
            bass_db=status.bass_db,
            treble_db=status.treble_db,
            balance=status.balance,
            tone_bypass=status.tone_bypass,
            speaker_a=status.speaker_a,
            speaker_b=status.speaker_b,
            dimmer=status.dimmer,
            unsupported=status.unsupported,
        )

    def apply(self, **updates: Any) -> RotelData:
        """Return a copy with the given fields replaced.

        Only the fields that are passed are replaced, and ``None`` never is:
        a caller that optimistically updates one property after a command
        leaves the values still unknown to the device untouched.
        """
        return replace(self, **{k: v for k, v in updates.items() if v is not None})


#: Attributes of the state a device can report. A field is only ever decoded
#: into one of them, so filtering by this set is what keeps an unexpected key
#: from reaching ``RotelData.apply``.
STATE_ATTRIBUTES: frozenset[str] = frozenset(item.name for item in fields(RotelData))


def changed_fields(
    previous: RotelData, current: RotelData
) -> dict[str, tuple[Any, Any]]:
    """Return the attributes that moved, as ``{attribute: (before, after)}``.

    A value that is unknown on either side is not a change: the very first
    snapshot is no reason to announce anything, and a firmware that stops
    answering a query must not look like the amplifier was reset.
    """
    changed: dict[str, tuple[Any, Any]] = {}
    for attribute in STATE_ATTRIBUTES - _QUIET_ATTRIBUTES:
        before = getattr(previous, attribute)
        after = getattr(current, attribute)
        if before is None or after is None or before == after:
            continue
        changed[attribute] = (before, after)
    return changed


def _plain(value: Any) -> Any:
    """Return a value that survives the trip through the event bus."""
    return value.value if isinstance(value, RotelInput) else value


RotelConfigEntry: TypeAlias = ConfigEntry[RotelApi]


class RotelCoordinator(DataUpdateCoordinator[RotelData]):
    """Poll an amplifier, follow its reports, and share both with the platforms."""

    config_entry: RotelConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: RotelConfigEntry,
        api: RotelApi,
        model: RotelModel,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        *,
        push_updates: bool = True,
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
        #: Whether to react to what the device reports without being asked.
        self.push_updates = push_updates
        #: Consecutive failed polls, surfaced through diagnostics.
        self.consecutive_failures = 0
        #: Reports applied and events fired, for the diagnostics download.
        self.push_reports = 0
        self.push_events = 0
        self._listener: asyncio.Task[None] | None = None
        #: State the event tracker compares against, and where the change that
        #: is being published came from.
        self._reported: RotelData | None = None
        self._origin: str = ORIGIN_POLL
        self._device_id: str | None = None

    async def _async_setup(self) -> None:
        """Open the connection once before the first poll."""
        try:
            await self.api.async_connect()
        except RotelApiError as err:
            # Do not abort setup: Home Assistant prefers unavailable entities
            # over a failing config entry, so the first refresh decides.
            LOGGER.warning("Initial connection to %s failed: %s", self.api.host, err)
        if self.push_updates:
            self.async_start_listener()

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

    # --- unsolicited reports ---------------------------------------------

    @callback
    def async_start_listener(self) -> None:
        """Start following what the amplifier reports without being asked.

        The task belongs to the config entry, so unloading the entry stops it
        even while Home Assistant itself is shutting down.
        """
        if self._listener is not None or not self.push_updates:
            return
        self._listener = self.config_entry.async_create_background_task(
            self.hass,
            self._async_listen(),
            name=f"{self.config_entry.title} Rotel reports",
            eager_start=False,
        )

    @property
    def listening(self) -> bool:
        """True while the task that follows the device reports is running."""
        task = self._listener
        return task is not None and not task.done()

    async def _async_listen(self) -> None:
        """Apply every report the device sends, for as long as we are loaded."""
        while True:
            if (batch := await self.api.async_next_push()) is None:
                # The connection is gone. The poll re-opens it, and it keeps the
                # entities correct in the meantime.
                LOGGER.debug("Reports of %s stopped", self.api.host)
                continue
            try:
                self.async_apply_reported(batch)
            except Exception:
                # The listener may not die: a report we cannot apply is one
                # update that is lost, not a broken integration.
                LOGGER.exception("Rotel: cannot apply the report %s", batch)

    @callback
    def async_apply_reported(self, fields: Mapping[str, str]) -> None:
        """Publish a report of the device as the current state.

        Only the reported fields are replaced, so a device that reports the
        volume of a knob turn leaves everything else it told us alone.
        """
        decoded = {
            attribute: value
            for attribute, value in self.api.decode_fields(fields).items()
            if attribute in STATE_ATTRIBUTES
        }
        if not decoded:
            return
        self.push_reports += 1
        updated = self.known_state.apply(**decoded)
        self._origin = ORIGIN_PUSH
        self.async_set_updated_data(updated)

    # --- change events ---------------------------------------------------

    @callback
    def async_track_changes(self) -> None:
        """Announce every change the amplifier makes by itself.

        Called once the platforms are set up, so this listener runs after the
        entity ones: an event that reports a state the entities do not show
        yet would be worse than no event at all. The snapshot of the first
        refresh is the state to compare the next one against.
        """
        if self.data is not None:
            self._reported = self.data
        self.config_entry.async_on_unload(self.async_add_listener(self._async_track))

    @callback
    def _async_track(self) -> None:
        """Fire an event for every change this coordinator published.

        Registered as a coordinator listener so the entities have already been
        updated when an automation reacts: an event that reports a state the
        entities do not show yet is worse than no event at all.
        """
        current = self.data
        previous, self._reported = self._reported, current
        origin, self._origin = self._origin, ORIGIN_POLL
        if previous is None or current is None or origin == ORIGIN_COMMAND:
            # The snapshot we compared with is the first one, and a change Home
            # Assistant caused itself is nothing new to an automation.
            return
        if changed := changed_fields(previous, current):
            self.push_events += 1
            self.hass.bus.async_fire(
                EVENT_COMMAND_RECEIVED,
                {
                    "device_id": self._device_registry_id,
                    "entry_id": self.config_entry.entry_id,
                    "host": self.api.host,
                    "port": self.api.port,
                    "origin": origin,
                    "changes": {
                        attribute: {
                            "old": _plain(before),
                            "new": _plain(after),
                        }
                        for attribute, (before, after) in changed.items()
                    },
                },
            )

    @property
    def _device_registry_id(self) -> str | None:
        """Id of the amplifier in the device registry, if it is registered."""
        if self._device_id is None:
            device = dr.async_get(self.hass).async_get_device(
                identifiers={(DOMAIN, self.api.host)}
            )
            if device is not None:
                self._device_id = device.id
        return self._device_id

    async def async_shutdown(self) -> None:
        """Stop following the device reports together with the scheduled refreshes."""
        await super().async_shutdown()
        task, self._listener = self._listener, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- helpers for the entity platforms --------------------------------

    @property
    def known_state(self) -> RotelData:
        """Last known state, empty while no poll has succeeded yet.

        Services may be called before the first update completes, so the
        optimistically updated values must not assume ``data`` is set.
        """
        return self.data if self.data is not None else RotelData()

    @callback
    def _async_apply_command(self, data: RotelData) -> None:
        """Publish the result of a command Home Assistant sent itself.

        The state moves right away so the entities follow the user's action,
        but no event is fired: the change came from here, and the automation
        that asked for it knows about it.
        """
        self._origin = ORIGIN_COMMAND
        self.async_set_updated_data(data)

    async def async_set_power(self, power: bool) -> None:
        """Switch the amplifier on/off and refresh."""
        await self.api.async_set_power(power)
        known = self.known_state
        self._async_apply_command(
            known.apply(power=power, mute=False if power else known.mute)
        )
        await self.async_request_refresh()

    async def async_set_volume(self, volume_db: float) -> None:
        """Set the volume and refresh."""
        applied = await self.api.async_set_volume(volume_db)
        # The raw position of the volume scale is only known here, and the
        # attribute should not wait for the next poll to catch up with it.
        self._async_apply_command(
            self.known_state.apply(
                volume_db=applied, volume_raw=self.api.values.get(VOLUME)
            )
        )
        await self.async_request_refresh()

    async def async_set_mute(self, mute: bool) -> None:
        """Mute/unmute and refresh."""
        await self.api.async_set_mute(mute)
        self._async_apply_command(self.known_state.apply(mute=mute))
        await self.async_request_refresh()

    async def async_set_source(self, source: str) -> None:
        """Select an input and refresh."""
        resolved = await self.api.async_set_source(source)
        self._async_apply_command(self.known_state.apply(source=resolved))
        await self.async_request_refresh()

    async def async_set_record_source(self, source: str) -> None:
        """Select the record input and refresh."""
        resolved = await self.api.async_set_record_source(source)
        self._async_apply_command(self.known_state.apply(record_source=resolved))
        await self.async_request_refresh()

    async def async_set_tone(self, command: RotelCommand, value: float) -> None:
        """Set bass or treble in dB and refresh."""
        applied = await self.api.async_set_tone(command, value)
        attribute = "bass_db" if command is RotelCommand.BASS else "treble_db"
        self._async_apply_command(self.known_state.apply(**{attribute: applied}))
        await self.async_request_refresh()

    async def async_set_balance(self, balance: float) -> None:
        """Set the channel balance and refresh."""
        applied = await self.api.async_set_balance(balance)
        self._async_apply_command(self.known_state.apply(balance=applied))
        await self.async_request_refresh()

    async def async_set_tone_bypass(self, bypass: bool) -> None:
        """Bypass or re-enable the tone block and refresh."""
        applied = await self.api.async_set_tone_bypass(bypass)
        self._async_apply_command(self.known_state.apply(tone_bypass=applied))
        await self.async_request_refresh()

    async def async_set_speaker(self, group: str, enabled: bool) -> None:
        """Switch a speaker group and refresh."""
        await self.api.async_set_speaker(group, enabled)
        attribute = "speaker_a" if group.casefold() == "a" else "speaker_b"
        self._async_apply_command(self.known_state.apply(**{attribute: enabled}))
        await self.async_request_refresh()

    async def async_set_dimmer(self, level: float) -> None:
        """Set the display brightness and refresh."""
        applied = await self.api.async_set_dimmer(level)
        self._async_apply_command(self.known_state.apply(dimmer=applied))
        await self.async_request_refresh()


__all__ = (
    "RotelConfigEntry",
    "RotelCoordinator",
    "RotelData",
    "changed_fields",
)


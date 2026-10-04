"""Asyncio TCP client for Rotel amplifiers.

The client is intentionally small and defensive:

* a single lock serialises every exchange, because Rotel units expose one
  UART behind the socket and interleave answers when commands overlap;
* replies are read line by line, with echoes, acknowledgements and stale
  (late) answers discarded;
* a query is retried once after reconnecting, while a state-changing command
  is never retried blindly;
* unsupported queries (``MODEL_QUERY`` on older firmware) degrade to ``None``
  instead of failing the whole poll cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Self

from .const import (
    CONNECT_TIMEOUT,
    DRAIN_TIMEOUT,
    INTERLOCK_VOLUME_DELAY,
    SOCKET_TIMEOUT,
)
from .protocol import (
    TERMINATOR,
    ProtocolError,
    RotelCommand,
    RotelInput,
    RotelModel,
    build_command,
    parse_command,
    parse_on_off,
    parse_source,
    snap_volume,
    source_payload,
    volume_from_payload,
    volume_payload_range,
    volume_to_payload,
)

LOGGER: logging.Logger = logging.getLogger(__package__)


class RotelApiError(Exception):
    """Base class for all Rotel communication errors."""


class RotelApiConnectionError(RotelApiError):
    """The device could not be reached."""


class RotelApiProtocolError(RotelApiError):
    """The device answered with something we cannot interpret."""


@dataclass(slots=True)
class RotelStatus:
    """Snapshot of the amplifier state."""

    power: bool | None = None
    volume_db: float | None = None
    mute: bool | None = None
    source: RotelInput | None = None
    record_source: RotelInput | None = None
    model: str | None = None
    firmware: str | None = None
    #: Query names that the firmware did not answer.
    unsupported: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_state(self) -> bool:
        """True when at least power state is known."""
        return self.power is not None


class RotelApi:
    """Talks the Rotel ASCII protocol over TCP."""

    def __init__(
        self,
        host: str,
        port: int,
        model: RotelModel,
        *,
        connect_timeout: float = CONNECT_TIMEOUT,
        socket_timeout: float = SOCKET_TIMEOUT,
    ) -> None:
        self._host = host
        self._port = port
        self._model = model
        self._connect_timeout = connect_timeout
        self._socket_timeout = socket_timeout
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        #: Last volume we successfully applied, used for the power interlock.
        self._last_volume_db: float | None = None

    @property
    def host(self) -> str:
        """Host we are connected to."""
        return self._host

    @property
    def port(self) -> int:
        """Port we are connected to."""
        return self._port

    @property
    def model(self) -> RotelModel:
        """Model profile used to encode/decode values."""
        return self._model

    @property
    def connected(self) -> bool:
        """True when a socket is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def base_url(self) -> str:
        """Web UI of the device, used for the device entry link."""
        return f"http://{self._host}"

    # --- connection management ------------------------------------------

    async def async_connect(self) -> None:
        """Open the socket (idempotent) and verify the protocol works."""
        async with self._lock:
            await self._async_open_locked()
            await self._async_send_locked(
                RotelCommand.POWER_QUERY, build_command(RotelCommand.POWER_QUERY)
            )

    async def async_disconnect(self) -> None:
        """Close the socket."""
        async with self._lock:
            await self._async_close_locked()

    async def _async_open_locked(self) -> None:
        if self.connected:
            return
        await self._async_close_locked()
        LOGGER.debug("Connecting to Rotel at %s:%s", self._host, self._port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._connect_timeout,
            )
        except TimeoutError as err:
            raise RotelApiConnectionError(
                f"Timeout connecting to {self._host}:{self._port}"
            ) from err
        except OSError as err:
            raise RotelApiConnectionError(
                f"Cannot connect to {self._host}:{self._port}: {err}"
            ) from err

    async def _async_close_locked(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await writer.wait_closed()

    # --- low level exchange ---------------------------------------------

    async def _async_exchange(self, command: RotelCommand, value: object | None = None) -> str:
        """Send ``command`` and return the payload of its reply (may be "")."""
        line = build_command(command, value)
        async with self._lock:
            await self._async_open_locked()
            try:
                return await self._async_send_locked(command, line)
            except (RotelApiConnectionError, RotelApiProtocolError):
                # The socket may have been closed by the peer (Rotel units
                # drop idle connections); reconnect and try once more.
                LOGGER.debug("Rotel: retrying %r after connection error", line)
                await self._async_close_locked()
                await self._async_open_locked()
                return await self._async_send_locked(command, line)

    async def _async_send_locked(self, command: RotelCommand, line: str) -> str:
        writer = self._writer
        reader = self._reader
        if writer is None or reader is None:
            raise RotelApiConnectionError("Socket is closed")
        payload = line.encode("ascii", "ignore") + TERMINATOR.encode("ascii")
        LOGGER.debug("Rotel >>> %s", line)
        try:
            writer.write(payload)
            await writer.drain()
        except (OSError, TimeoutError) as err:
            await self._async_close_locked()
            raise RotelApiConnectionError(f"Send failed for {line!r}: {err}") from err

        if command.endswith("_QUERY"):
            return await self._async_read_payload_locked(reader, command)

        # Write commands normally return nothing; drain late replies so they
        # cannot be mistaken for the answer of the next query.
        await self._async_drain_locked(reader)
        return ""

    async def _async_read_payload_locked(
        self, reader: asyncio.StreamReader, command: RotelCommand
    ) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._socket_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RotelApiProtocolError(f"Timeout waiting for reply to {command}")
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
            except TimeoutError as err:
                raise RotelApiProtocolError(
                    f"Timeout waiting for reply to {command}"
                ) from err
            if not line:
                await self._async_close_locked()
                raise RotelApiConnectionError("Device closed the connection")

            parsed = parse_command(line.decode("ascii", "ignore"))
            if parsed is None:
                continue
            reply_command, payload = parsed
            if reply_command == command.removesuffix("_QUERY"):
                LOGGER.debug("Rotel <<< %s", line.strip())
                return payload
            # Anything else is an echo of a previous command or an out of band
            # message; keep waiting for the answer we asked for.

    async def _async_drain_locked(self, reader: asyncio.StreamReader) -> None:
        """Consume anything the device sends unsolicited for a short while."""
        with contextlib.suppress(TimeoutError, OSError):
            while True:
                line = await asyncio.wait_for(
                    reader.readline(), timeout=DRAIN_TIMEOUT
                )
                if not line:
                    return
                LOGGER.debug("Rotel <<< (drained) %s", line.strip())

    # --- commands --------------------------------------------------------

    async def async_query(self, command: RotelCommand) -> str | None:
        """Query a single value from the device, ``None`` when unsupported."""
        try:
            return await self._async_exchange(command)
        except RotelApiProtocolError:
            LOGGER.debug("Rotel does not support %s", command)
            return None

    async def async_get_status(self) -> RotelStatus:
        """Poll the amplifier for its full state.

        Queries run sequentially (one UART behind one socket) and every query
        is independent: a firmware that ignores ``MODEL_QUERY`` still yields
        a usable snapshot.
        """
        status = RotelStatus()
        unsupported: set[str] = set()

        power = await self._async_exchange(RotelCommand.POWER_QUERY)
        status.power = parse_on_off(power)

        volume = await self._async_exchange(RotelCommand.VOLUME_QUERY)
        status.volume_db = self._parse_volume(volume)

        mute = await self._async_exchange(RotelCommand.MUTE_QUERY)
        status.mute = parse_on_off(mute)

        source = await self._async_exchange(RotelCommand.SOURCE_QUERY)
        try:
            status.source = parse_source(source, self._model)
        except ProtocolError as err:
            LOGGER.debug("Unexpected source reply: %s", err)

        if self._model.record_inputs:
            record_source = await self.async_query(RotelCommand.RECORD_SOURCE_QUERY)
            if record_source:
                with contextlib.suppress(ProtocolError):
                    status.record_source = parse_source(
                        record_source, self._model, record=True
                    )
            else:
                unsupported.add(RotelCommand.RECORD_SOURCE_QUERY)

        model = await self.async_query(RotelCommand.MODEL_QUERY)
        if model:
            status.model = model
        else:
            unsupported.add(RotelCommand.MODEL_QUERY)

        firmware = await self.async_query(RotelCommand.FIRMWARE_QUERY)
        if firmware:
            status.firmware = firmware
        else:
            unsupported.add(RotelCommand.FIRMWARE_QUERY)

        if status.power:
            self._last_volume_db = status.volume_db
        status.unsupported = frozenset(unsupported)
        return status

    async def async_set_power(self, power: bool) -> None:
        """Switch the amplifier to standby or on.

        Rotel units apply an interlock on power-up: the pre-out relays stay
        open until a volume has been set. Re-sending the last known volume
        removes the ~1 s "no sound after standby" surprise.
        """
        await self._async_exchange(RotelCommand.POWER, power)
        if power and self._last_volume_db is not None:
            with contextlib.suppress(RotelApiError):
                await asyncio.sleep(INTERLOCK_VOLUME_DELAY)
                await self.async_set_volume(self._last_volume_db)

    async def async_set_volume(self, volume_db: float) -> float:
        """Set the volume in dB and return the value the device accepted."""
        snapped = snap_volume(volume_db, self._model)
        payload = volume_to_payload(snapped, self._model)
        await self._async_exchange(RotelCommand.VOLUME, payload)
        self._last_volume_db = snapped
        return snapped

    async def async_set_mute(self, mute: bool) -> None:
        """Mute or unmute the amplifier."""
        await self._async_exchange(RotelCommand.MUTE, mute)

    async def async_set_source(self, source: str) -> RotelInput:
        """Select an input by label or protocol value."""
        resolved = _resolve_source(source, self._model)
        await self._async_exchange(
            RotelCommand.SOURCE, source_payload(resolved.name, self._model)
        )
        return resolved

    async def async_set_record_source(self, source: str) -> RotelInput:
        """Select the record input (pre-out/record path) of an RCX."""
        if not self._model.record_inputs:
            raise RotelApiProtocolError(
                f"{self._model.name} has no record source"
            )
        resolved = _resolve_source(source, self._model, record=True)
        await self._async_exchange(
            RotelCommand.RECORD_SOURCE,
            source_payload(resolved.name, self._model, record=True),
        )
        return resolved

    async def async_reset_panel(self) -> None:
        """Reset the display/panel lock (used after a firmware quirk)."""
        await self._async_exchange(RotelCommand.RESET_PANEL)

    async def async_get_model_info(self) -> tuple[str | None, str | None]:
        """Return ``(model, firmware)`` for the config flow."""
        model = await self.async_query(RotelCommand.MODEL_QUERY)
        firmware = await self.async_query(RotelCommand.FIRMWARE_QUERY)
        return model, firmware

    async def async_validate(self) -> dict[str, Any]:
        """Validate the connection and return basic device information."""
        await self.async_connect()
        try:
            model, firmware = await self.async_get_model_info()
            return {"model": model, "firmware": firmware}
        finally:
            await self.async_disconnect()

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the socket on context exit."""
        await self.async_disconnect()

    def __repr__(self) -> str:
        """Return a debug representation."""
        return (
            f"RotelApi(host={self._host!r}, port={self._port}, "
            f"model={self._model.key!r}, connected={self.connected})"
        )

    def _parse_volume(self, payload: str) -> float:
        """Decode a volume reply, warning about a mismatching profile."""
        value = _as_int(payload)
        lowest, highest = volume_payload_range(self._model)
        if not lowest <= value <= highest:
            LOGGER.warning(
                "Rotel reported volume %s which is outside the %s expected for the "
                "%s profile (expected %s..%s). Adjust the volume_scale of the "
                "profile if the volume behaves incorrectly",
                value,
                self._model.volume_scale.value,
                self._model.key,
                lowest,
                highest,
            )
        return volume_from_payload(value, self._model)


def _as_int(value: str) -> int:
    """Parse an integer payload, raising :class:`ProtocolError`."""
    try:
        return int(float(value.strip().strip('"')))
    except (TypeError, ValueError) as err:
        raise RotelApiProtocolError(f"Expected a number, got {value!r}") from err


def _resolve_source(source: str, model: RotelModel, *, record: bool = False) -> RotelInput:
    """Resolve a source label or protocol value to a :class:`RotelInput`."""
    if found := model.input_by_name(source):
        return found
    if found := model.input_by_value(source):
        return found
    if record and model.record_inputs:
        for item in model.record_inputs:
            if item.name.casefold() == source.casefold() or (
                item.value.casefold() == source.casefold()
            ):
                return item
    known = ", ".join(item.name for item in model.inputs)
    raise RotelApiProtocolError(f"Unknown source {source!r}. Known sources: {known}")


__all__ = (
    "RotelApi",
    "RotelApiConnectionError",
    "RotelApiError",
    "RotelApiProtocolError",
    "RotelStatus",
)

"""Asyncio TCP client for Rotel amplifiers.

The client is intentionally small and defensive:

* a single lock serialises every exchange, because Rotel units answer in the
  order they receive commands and interleave replies otherwise;
* replies are framed on ``$`` and turned into ``{key: value}`` maps, so
  batches of queries, unsolicited push fields and echoes are all handled;
* a query is retried once after reconnecting, while a state-changing command
  is never retried blindly;
* unsupported queries (``model?`` on older firmware) degrade to ``None``
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
    DRAIN_MAX_TIMEOUT,
    DRAIN_TIMEOUT,
    INTERLOCK_VOLUME_DELAY,
    MAX_BUFFER_SIZE,
    MAX_VALUE_LENGTH,
    SOCKET_TIMEOUT,
)
from .protocol import (
    RESPONSE_TERMINATOR,
    ProtocolError,
    RotelCommand,
    RotelInput,
    RotelModel,
    RotelQuery,
    build_command,
    build_query,
    match_input,
    parse_messages,
    parse_on_off,
    parse_source,
    parse_volume_number,
    snap_volume,
    source_command,
    volume_from_payload,
    volume_is_out_of_range,
    volume_payload_range,
    volume_to_payload,
)

LOGGER: logging.Logger = logging.getLogger(__package__)

#: Names of the fields the device reports. They are the values of
#: :class:`~custom_components.rotel_control.protocol.RotelQuery`, kept as plain
#: strings because they are used as dictionary keys.
POWER = str(RotelQuery.POWER)
VOLUME = str(RotelQuery.VOLUME)
MUTE = str(RotelQuery.MUTE)
SOURCE = str(RotelQuery.SOURCE)
RECORD_SOURCE = str(RotelQuery.RECORD_SOURCE)
MODEL = str(RotelQuery.MODEL)
FIRMWARE = str(RotelQuery.FIRMWARE)

#: Fields every amplifier reports; anything else is optional.
CORE_FIELDS: frozenset[str] = frozenset({POWER, VOLUME, MUTE, SOURCE})


def _limit_values(values: dict[str, str]) -> dict[str, str]:
    """Cap the length of what a device reported.

    Replies are cached for the lifetime of the connection and end up in entity
    attributes and in downloaded diagnostics, so a device that answers with a
    huge string must not be able to grow them without bound.
    """
    return {
        key: value if len(value) <= MAX_VALUE_LENGTH else value[:MAX_VALUE_LENGTH]
        for key, value in values.items()
    }


class RotelApiError(Exception):
    """Base class for all Rotel communication errors."""


class RotelApiConnectionError(RotelApiError):
    """The device could not be reached."""


class RotelApiClosedError(RotelApiConnectionError):
    """The device accepted the connection but closed it again.

    Rotel units behave like this while in standby with the factory
    default POWER OPTION = Normal, and when another controller already
    holds their single control connection, so the config flow can tell
    it apart from a port that does not listen at all.
    """


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
    #: Raw volume as reported by the device (front panel scale).
    volume_raw: str | None = None
    #: Query names that the firmware did not answer.
    unsupported: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_state(self) -> bool:
        """True when at least the power state is known."""
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
        self._buffer = ""
        #: Everything the device told us so far, including push fields.
        self._values: dict[str, str] = {}
        #: Fields that have been asked for at least once on this connection.
        self._asked: set[str] = set()
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

    @property
    def values(self) -> dict[str, str]:
        """Last reported value of every field the device sent."""
        return dict(self._values)

    # --- connection management ------------------------------------------

    async def async_connect(self) -> None:
        """Open the socket (idempotent) and verify the protocol works."""
        async with self._lock:
            await self._async_open_locked()
            try:
                await self._async_exchange_locked(
                    [
                        build_query(RotelQuery.POWER),
                        build_query(RotelQuery.MODEL),
                        build_query(RotelQuery.FIRMWARE),
                        *(build_query(key) for key in self._model.queries),
                    ],
                    required={POWER},
                )
                # The device answers in bursts; give the fields we do not wait
                # for a moment to arrive so they can be cached.
                await self._async_read_available_locked()
            except RotelApiError:
                await self._async_close_locked()
                raise

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
        self._buffer = ""

    async def _async_close_locked(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        self._buffer = ""
        # "Asked once" is a per-connection promise: a connection that failed
        # before the device answered must be asked again on the next one.
        self._asked.clear()
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await writer.wait_closed()

    # --- low level exchange ---------------------------------------------

    async def _async_write_locked(self, line: str) -> None:
        """Write one command line, opening the socket if needed."""
        writer = self._writer
        if writer is None:
            raise RotelApiConnectionError("Socket is closed")
        LOGGER.debug("Rotel >>> %s", line)
        try:
            writer.write(line.encode("ascii", "ignore"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError) as err:
            await self._async_close_locked()
            raise RotelApiClosedError(
                f"Device reset the connection: {err}"
            ) from err
        except (OSError, TimeoutError) as err:
            await self._async_close_locked()
            raise RotelApiConnectionError(f"Send failed for {line!r}: {err}") from err

    async def _async_exchange_locked(
        self,
        lines: list[str],
        *,
        required: set[str] | None = None,
    ) -> dict[str, str]:
        """Send ``lines`` and return the fields the device reported.

        Reading stops as soon as every key of ``required`` has been seen plus
        the rest of the burst that follows it, so a poll returns a complete
        snapshot.
        """
        await self._async_open_locked()
        for line in lines:
            await self._async_write_locked(line)
            if line.endswith("?"):
                self._asked.add(line.removesuffix("?"))
        return await self._async_collect_locked(required or set())

    async def _async_exchange(
        self, lines: list[str], *, required: set[str] | None = None
    ) -> dict[str, str]:
        """Exchange under the lock, reconnecting and retrying once on error."""
        async with self._lock:
            try:
                return await self._async_exchange_locked(lines, required=required)
            except (RotelApiConnectionError, RotelApiProtocolError):
                # The socket may have been closed by the peer (Rotel units
                # drop idle connections); reconnect and try once more.
                LOGGER.debug("Rotel: retrying %r after connection error", lines)
                await self._async_close_locked()
                return await self._async_exchange_locked(lines, required=required)

    async def _async_collect_locked(self, required: set[str]) -> dict[str, str]:
        """Read fields until ``required`` is complete or the socket times out."""
        reader = self._reader
        if reader is None:
            raise RotelApiConnectionError("Socket is closed")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._socket_timeout
        received: dict[str, str] = {}
        while True:
            values, self._buffer = parse_messages(self._buffer)
            received.update(values)
            if required <= received.keys():
                # The rest of the burst follows immediately: collect it too so
                # the snapshot is complete instead of one poll behind.
                received.update(await self._async_read_available_locked())
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                missing = sorted(required - received.keys())
                if missing:
                    raise RotelApiProtocolError(
                        f"Timeout waiting for {', '.join(missing)}"
                    )
                break
            try:
                data = await asyncio.wait_for(reader.read(512), timeout=remaining)
            except TimeoutError:
                continue
            except (ConnectionResetError, BrokenPipeError) as err:
                await self._async_close_locked()
                raise RotelApiClosedError(
                    f"Device reset the connection: {err}"
                ) from err
            if not data:
                await self._async_close_locked()
                raise RotelApiClosedError("Device closed the connection")
            self._buffer += data.decode("ascii", "ignore")
            if len(self._buffer) > MAX_BUFFER_SIZE:
                await self._async_close_locked()
                raise RotelApiConnectionError(
                    f"Device sent more than {MAX_BUFFER_SIZE} bytes without a "
                    f"'{RESPONSE_TERMINATOR}' terminator"
                )

        # Cache everything, including fields that are only informational.
        self._values.update(_limit_values(received))
        if received:
            LOGGER.debug("Rotel <<< %s", received)
        return received

    async def _async_read_available_locked(
        self, timeout: float = DRAIN_TIMEOUT
    ) -> dict[str, str]:
        """Return whatever the device sends unsolicited while it keeps talking.

        The wait is an *idle* timeout: every chunk that arrives restarts it, so
        a burst that spans more than ``timeout`` is still collected completely
        instead of leaving fields behind for the next poll. ``DRAIN_MAX_TIMEOUT``
        caps the total wait for a device that never stops sending.
        """
        reader = self._reader
        received: dict[str, str] = {}
        if reader is None:
            return received
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_MAX_TIMEOUT
        idle_until = loop.time() + timeout
        while True:
            now = loop.time()
            if now >= deadline or now >= idle_until:
                return received
            try:
                data = await asyncio.wait_for(
                    reader.read(512), timeout=min(idle_until, deadline) - now
                )
            except (TimeoutError, OSError):
                return received
            if not data:
                await self._async_close_locked()
                return received
            self._buffer += data.decode("ascii", "ignore")
            if len(self._buffer) > MAX_BUFFER_SIZE:
                await self._async_close_locked()
                raise RotelApiConnectionError(
                    f"Device sent more than {MAX_BUFFER_SIZE} bytes without a "
                    f"'{RESPONSE_TERMINATOR}' terminator"
                )
            values, self._buffer = parse_messages(self._buffer)
            if values:
                LOGGER.debug("Rotel <<< %s", values)
                received.update(values)
                self._values.update(_limit_values(values))
                # The device is still talking: keep waiting for the rest.
                idle_until = loop.time() + timeout

    async def _async_command(self, line: str) -> None:
        """Send a command and keep the late fields it may trigger."""
        async with self._lock:
            try:
                await self._async_open_locked()
                await self._async_write_locked(line)
                await self._async_read_available_locked()
            except RotelApiConnectionError:
                LOGGER.debug("Rotel: retrying %r after connection error", line)
                await self._async_close_locked()
                await self._async_open_locked()
                await self._async_write_locked(line)
                await self._async_read_available_locked()

    # --- commands --------------------------------------------------------

    async def async_query(self, key: RotelQuery) -> str | None:
        """Ask for a single field, ``None`` when the firmware ignores it."""
        name = str(key)
        try:
                received = await self._async_exchange(
                [build_query(key)], required={name}
            )
        except RotelApiProtocolError:
            LOGGER.debug("Rotel does not support %s", key)
            return None
        return received.get(name)

    async def async_get_status(self) -> RotelStatus:
        """Poll the amplifier for its full state.

        All queries go out in one write: the device answers every one of
        them, and the replies usually arrive in a single TCP segment. Only
        the power state is mandatory — a field the firmware does not answer
        stays unknown instead of failing the whole poll.
        """
        status = RotelStatus()
        unsupported: set[str] = set()
        optional = {str(key) for key in self._model.queries}
        # The device information is asked for once: repeating it every poll
        # would only waste bandwidth on a firmware that never answers.
        wanted = (
            set(CORE_FIELDS)
            | optional
            | ({MODEL, FIRMWARE} - self._asked)
        )
        received = await self._async_exchange(
            [f"{name}?" for name in sorted(wanted)], required={POWER}
        )
        # Fields that did not arrive must not keep a stale value of an older
        # poll, and an unanswered optional query is remembered as unsupported.
        for name in optional:
            if name not in received:
                unsupported.add(name)
                self._values.pop(name, None)

        if (power := received.get(POWER)) is not None:
            try:
                status.power = parse_on_off(power)
            except ProtocolError as err:
                LOGGER.debug("Unexpected power reply: %s", err)

        if (volume := received.get(VOLUME)) is not None:
            status.volume_raw = volume[:MAX_VALUE_LENGTH]
            status.volume_db = self._parse_volume(volume)

        if (mute := received.get(MUTE)) is not None:
            try:
                status.mute = parse_on_off(mute)
            except ProtocolError as err:
                LOGGER.debug("Unexpected mute reply: %s", err)

        if (source := received.get(SOURCE)) is not None:
            try:
                status.source = parse_source(source, self._model)
            except ProtocolError as err:
                LOGGER.debug("Unexpected source reply: %s", err)

        if (record := received.get(RECORD_SOURCE)) is not None:
            with contextlib.suppress(ProtocolError):
                status.record_source = parse_source(record, self._model, record=True)

        status.model = self._values.get(MODEL)
        status.firmware = self._values.get(FIRMWARE)
        for name in (MODEL, FIRMWARE):
            if name not in self._values:
                unsupported.add(name)

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
        await self._async_command(
            build_command(RotelCommand.POWER_ON if power else RotelCommand.POWER_OFF)
        )
        self._values[POWER] = "on" if power else "standby"
        if power and self._last_volume_db is not None:
            with contextlib.suppress(RotelApiError):
                await asyncio.sleep(INTERLOCK_VOLUME_DELAY)
                await self.async_set_volume(self._last_volume_db)

    async def async_set_volume(self, volume_db: float) -> float:
        """Set the volume in dB and return the value the device accepted."""
        snapped = snap_volume(volume_db, self._model)
        payload = volume_to_payload(snapped, self._model)
        await self._async_command(build_command(RotelCommand.VOLUME, payload))
        self._values[VOLUME] = str(payload)
        self._last_volume_db = snapped
        return snapped

    async def async_set_mute(self, mute: bool) -> None:
        """Mute or unmute the amplifier."""
        await self._async_command(
            build_command(RotelCommand.MUTE_ON if mute else RotelCommand.MUTE_OFF)
        )
        self._values[MUTE] = "on" if mute else "off"

    async def async_set_source(self, source: str) -> RotelInput:
        """Select an input by label or protocol value."""
        resolved = self._require_source(source, self._model)
        await self._async_command(source_command(resolved.value))
        self._values[SOURCE] = resolved.value
        return resolved

    async def async_set_record_source(self, source: str) -> RotelInput:
        """Select the record input (pre-out/record path) of an RCX."""
        if not self._model.record_inputs:
            raise RotelApiProtocolError(f"{self._model.name} has no record source")
        resolved = self._require_source(source, self._model, record=True)
        await self._async_command(source_command(f"rec_{resolved.value}"))
        self._values[RECORD_SOURCE] = resolved.value
        return resolved

    async def async_validate(self) -> dict[str, Any]:
        """Validate the connection and return basic device information."""
        await self.async_connect()
        try:
            return {
                "model": await self._async_field(RotelQuery.MODEL),
                "firmware": await self._async_field(RotelQuery.FIRMWARE),
            }
        finally:
            await self.async_disconnect()

    async def _async_field(self, key: RotelQuery) -> str | None:
        """Return a field, asking the device only if it was never asked before."""
        name = str(key)
        if (value := self._values.get(name)) is not None:
            return value
        if name in self._asked:
            # An old firmware that does not answer is asked exactly once.
            return None
        return await self.async_query(key)

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
        number = parse_volume_number(payload)
        if number is not None and volume_is_out_of_range(number, self._model):
            lowest, highest = volume_payload_range(self._model)
            LOGGER.warning(
                "Rotel reported volume %r which is outside the %s..%s the %s "
                "profile expects. Adjust the volume scale of the profile if the "
                "volume behaves incorrectly",
                payload,
                lowest,
                highest,
                self._model.key,
            )
        try:
            return volume_from_payload(payload, self._model)
        except (TypeError, ValueError) as err:
            raise RotelApiProtocolError(f"Expected a volume, got {payload!r}") from err

    def _require_source(
        self, source: str, model: RotelModel, *, record: bool = False
    ) -> RotelInput:
        """Resolve a source label, raising a protocol error listing the options."""
        inputs = model.record_inputs if record else model.inputs
        if found := match_input(inputs, source):
            return found
        known = ", ".join(item.name for item in inputs)
        raise RotelApiProtocolError(
            f"Unknown source {source!r}. Known sources: {known}"
        )


__all__ = (
    "RotelApi",
    "RotelApiClosedError",
    "RotelApiConnectionError",
    "RotelApiError",
    "RotelApiProtocolError",
    "RotelStatus",
)
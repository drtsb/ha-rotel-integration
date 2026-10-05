"""Asyncio TCP client for Rotel amplifiers.

The client is intentionally small and defensive:

* one task owns the read side of the socket for the whole connection, so
  nothing else can consume a reply by accident;
* replies are framed on ``$`` and turned into ``{key: value}`` maps, which
  makes a batch of queries, an unsolicited push field and the echo of a command
  the same kind of message;
* a single lock serialises every exchange, because Rotel units answer in the
  order they receive commands and interleave replies otherwise;
* a query is retried once after reconnecting, while a state-changing command
  is never retried blindly;
* unsupported queries (``model?`` on older firmware) degrade to ``None``
  instead of failing the whole poll cycle.

Unsolicited reports are what makes the integration feel instant: a unit with
"auto update" enabled sends a field whenever something changes at the front
panel, and :meth:`RotelApi.async_next_push` hands those batches to the caller
without anybody having to ask.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Self

from .const import (
    CONNECT_TIMEOUT,
    DRAIN_MAX_TIMEOUT,
    DRAIN_TIMEOUT,
    INTERLOCK_VOLUME_DELAY,
    MAX_BUFFER_SIZE,
    MAX_VALUE_LENGTH,
    PUSH_ECHO_TTL,
    PUSH_QUEUE_SIZE,
    READ_CHUNK,
    SOCKET_TIMEOUT,
)
from .protocol import (
    RESPONSE_TERMINATOR,
    TONE_BYPASS_QUERIES,
    ProtocolError,
    RotelCommand,
    RotelInput,
    RotelModel,
    RotelQuery,
    balance_command,
    build_command,
    build_query,
    clamp_balance,
    clamp_dimmer,
    clamp_tone,
    dimmer_command,
    match_input,
    parse_balance,
    parse_dimmer,
    parse_messages,
    parse_on_off,
    parse_source,
    parse_speakers,
    parse_tone,
    parse_volume_number,
    snap_volume,
    source_command,
    tone_command,
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
BASS = str(RotelQuery.BASS)
TREBLE = str(RotelQuery.TREBLE)
BALANCE = str(RotelQuery.BALANCE)
SPEAKER = str(RotelQuery.SPEAKER)
DIMMER = str(RotelQuery.DIMMER)

#: The tone bypass switch is ``bypass`` on current firmware and ``tone`` on
#: older ones; the key the device answered is remembered per connection.
TONE_KEYS: tuple[str, ...] = tuple(str(key) for key in TONE_BYPASS_QUERIES)

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
    #: Tone block, in decibel, and the balance in L01..L15/R01..R15 steps.
    bass_db: int | None = None
    treble_db: int | None = None
    balance: int | None = None
    #: True while the tone block is bypassed.
    tone_bypass: bool | None = None
    #: Speaker groups that are switched on.
    speaker_a: bool | None = None
    speaker_b: bool | None = None
    #: Front display brightness, ``DIMMER_MIN`` is the brightest.
    dimmer: int | None = None
    #: Query names that the firmware did not answer.
    unsupported: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_state(self) -> bool:
        """True when at least the power state is known."""
        return self.power is not None

    @classmethod
    def from_fields(
        cls, decoded: Mapping[str, Any], unsupported: frozenset[str]
    ) -> RotelStatus:
        """Build a snapshot from the attributes :meth:`RotelApi.decode_fields`.

        Only the fields that were present end up in the snapshot, so a reply
        for a single field (the push of an auto update device) never pretends
        the rest of the state has been forgotten.
        """
        return cls(
            power=decoded.get("power"),
            volume_db=decoded.get("volume_db"),
            mute=decoded.get("mute"),
            source=decoded.get("source"),
            record_source=decoded.get("record_source"),
            model=decoded.get("device_model"),
            firmware=decoded.get("firmware"),
            volume_raw=decoded.get("volume_raw"),
            bass_db=decoded.get("bass_db"),
            treble_db=decoded.get("treble_db"),
            balance=decoded.get("balance"),
            tone_bypass=decoded.get("tone_bypass"),
            speaker_a=decoded.get("speaker_a"),
            speaker_b=decoded.get("speaker_b"),
            dimmer=decoded.get("dimmer"),
            unsupported=unsupported,
        )


class _Exchange:
    """One request/reply round trip, waiting for the fields it needs.

    The read task owns the socket, so an exchange is a future it resolves: it
    is finished as soon as every required field arrived *and* the device went
    quiet again (the rest of a burst follows the answer we asked for), or with
    a timeout naming the fields that never came.
    """

    def __init__(self, required: set[str], timeout: float) -> None:
        """Start waiting for ``required`` fields, giving up after ``timeout``."""
        loop = asyncio.get_running_loop()
        self.required = required
        self.received: dict[str, str] = {}
        #: A command asks nothing in particular, so it is complete as soon as
        #: the device stops answering it.
        self.satisfied = not required
        self.future: asyncio.Future[dict[str, str]] = loop.create_future()
        self._loop = loop
        self._deadline = loop.time() + timeout
        self._settled_at = loop.time()
        self._timer: asyncio.TimerHandle | None = None
        # Armed right away: a device that answers nothing at all must not keep
        # the caller waiting forever.
        self._arm()

    def feed(self, values: Mapping[str, str]) -> None:
        """Take the fields of one chunk and notice when they are complete."""
        self.received.update(values)
        if not self.satisfied and self.required <= self.received.keys():
            self.satisfied = True
        if self.satisfied:
            self._settled_at = self._loop.time()
        self._arm()

    def fail(self, error: RotelApiError) -> None:
        """Give up with ``error``."""
        self._cancel_timer()
        if not self.future.done():
            self.future.set_exception(error)

    def close(self) -> None:
        """Stop waiting, dropping a result nobody is going to look at."""
        self._cancel_timer()
        if not self.future.done():
            self.future.cancel()
        elif not self.future.cancelled():
            # Mark an error as retrieved, or asyncio complains about it.
            self.future.exception()

    async def wait(self) -> dict[str, str]:
        """Return the fields the device reported for this exchange."""
        return await self.future

    def _arm(self) -> None:
        """(Re)start the timer that decides when this exchange is over."""
        self._cancel_timer()
        loop = self._loop
        if self.satisfied:
            # Collect the rest of the burst, but not forever: DRAIN_MAX_TIMEOUT
            # stops a device that keeps talking from stalling a poll.
            until = min(
                self._settled_at + DRAIN_MAX_TIMEOUT, loop.time() + DRAIN_TIMEOUT
            )
        else:
            until = self._deadline
        self._timer = loop.call_at(until, self._on_timeout)

    def _on_timeout(self) -> None:
        """Complete the exchange, or report what the device never sent."""
        self._timer = None
        if self.future.done():
            return
        if self.satisfied:
            self._complete()
            return
        if missing := sorted(self.required - self.received.keys()):
            self.fail(
                RotelApiProtocolError(f"Timeout waiting for {', '.join(missing)}")
            )
            return
        self._complete()

    def _complete(self) -> None:
        """Hand the collected fields to the waiting caller."""
        self._cancel_timer()
        if not self.future.done():
            self.future.set_result(dict(self.received))

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


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
        #: Task that owns the read side of the socket, one per connection.
        self._read_task: asyncio.Task[None] | None = None
        #: Exchanges waiting for a reply. Rotel answers in order, so there is
        #: at most one of them; the lock guarantees that.
        self._waiters: collections.deque[_Exchange] = collections.deque()
        #: Batches the device reported on its own, for ``async_next_push``.
        self._pushes: asyncio.Queue[dict[str, str] | None] = asyncio.Queue(
            maxsize=PUSH_QUEUE_SIZE
        )
        #: Batches dropped because the listener did not keep up.
        self.dropped_pushes = 0
        #: Fields the device is expected to report back, as
        #: ``{key: (deadline, expected value)}``. The answer to a command we
        #: sent is not a change made at the amplifier.
        self._echo: dict[str, tuple[float, str | None]] = {}
        #: Everything the device told us so far, including push fields.
        self._values: dict[str, str] = {}
        #: Fields that have been asked for at least once on this connection.
        self._asked: set[str] = set()
        #: Last volume we successfully applied, used for the power interlock.
        self._last_volume_db: float | None = None
        #: Which of ``TONE_KEYS`` this firmware answers, ``None`` until told.
        self._tone_key: str | None = None

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
    def listening(self) -> bool:
        """True when a task is reading the socket for unsolicited reports."""
        task = self._read_task
        return task is not None and not task.done()

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
            except RotelApiError:
                await self._async_close_locked()
                raise

    async def async_disconnect(self) -> None:
        """Close the socket."""
        async with self._lock:
            await self._async_close_locked()

    async def _async_open_locked(self) -> None:
        if self.connected and self.listening:
            return
        await self._async_close_locked()
        LOGGER.debug("Connecting to Rotel at %s:%s", self._host, self._port)
        try:
            reader, writer = await asyncio.wait_for(
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
        self._reader, self._writer = reader, writer
        self._buffer = ""
        self._read_task = asyncio.create_task(
            self._async_read_loop(reader), name=f"Rotel read {self._host}:{self._port}"
        )

    async def _async_close_locked(self) -> None:
        """Drop the connection, stopping the task that reads it."""
        task, self._read_task = self._read_task, None
        writer, self._writer, self._reader = self._writer, None, None
        self._buffer = ""
        # "Asked once" is a per-connection promise: a connection that failed
        # before the device answered must be asked again on the next one.
        self._asked.clear()
        self._tone_key = None
        self._echo.clear()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            # The task closes the socket itself on the way out, except when it
            # is cancelled here, which is why the writer is closed below.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await task
        self._fail_exchanges(RotelApiClosedError("Connection closed"))
        if writer is None:
            return
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await writer.wait_closed()

    async def _async_read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read the socket until it is gone, framing everything on ``$``.

        This task is the only reader of the connection, which is what lets the
        replies of a poll and the unsolicited reports of the device be told
        apart reliably: an exchange that is still collecting gets the fields,
        and whatever nobody was waiting for is a push.
        """
        writer = self._writer
        error: RotelApiError = RotelApiClosedError("Device closed the connection")
        try:
            while True:
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                self._buffer += data.decode("ascii", "ignore")
                if len(self._buffer) > MAX_BUFFER_SIZE:
                    error = RotelApiConnectionError(
                        f"Device sent more than {MAX_BUFFER_SIZE} bytes without a "
                        f"'{RESPONSE_TERMINATOR}' terminator"
                    )
                    break
                values, self._buffer = parse_messages(self._buffer)
                if values:
                    LOGGER.debug("Rotel <<< %s", values)
                    # Cache everything, including fields that only the
                    # diagnostics care about.
                    self._values.update(_limit_values(values))
                waiter = self._waiters[0] if self._waiters else None
                if waiter is not None:
                    # Every chunk counts, also one without a field: the "?" a
                    # device sends for a query it does not know must not cut
                    # the burst short that this exchange is collecting.
                    waiter.feed(values)
                elif values:
                    self._async_push(values)
        except (ConnectionResetError, BrokenPipeError) as err:
            error = RotelApiClosedError(f"Device reset the connection: {err}")
        except OSError as err:
            error = RotelApiConnectionError(f"Connection failed: {err}")
        except asyncio.CancelledError:
            # Closed on purpose by _async_close_locked, not a device error.
            raise
        finally:
            await self._async_forget_connection(writer, error)

    async def _async_forget_connection(
        self, writer: asyncio.StreamWriter | None, error: RotelApiError
    ) -> None:
        """Release a connection whose reader task stopped, whoever stopped it."""
        if self._writer is writer:
            self._writer = None
            self._reader = None
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError, TimeoutError):
                    await writer.wait_closed()
        self._buffer = ""
        self._asked.clear()
        self._tone_key = None
        self._echo.clear()
        if self._read_task is asyncio.current_task():
            self._read_task = None
        self._fail_exchanges(error)
        # Wake the listener so it knows the stream of reports has stopped; the
        # next exchange opens a new connection and a new read task.
        self._async_queue_push(None)

    def _async_push(self, values: Mapping[str, str]) -> None:
        """Queue what the device reported without being asked."""
        now = asyncio.get_running_loop().time()
        fresh = {
            key: value
            for key, value in values.items()
            if not self._is_echo(key, value, now)
        }
        if not fresh:
            return
        LOGGER.debug("Rotel <<< reported %s", fresh)
        self._async_queue_push(dict(fresh))

    def _is_echo(self, key: str, value: str, now: float) -> bool:
        """True when the device only confirms a command we sent ourselves.

        Rotel answers every command with the field it changed, and that answer
        must not be reported as a change made at the device. An expectation
        without a value accepts any answer, which is what a speaker command
        needs: it reports the combined state of both groups.
        """
        if (expected := self._echo.get(key)) is None:
            return False
        until, wanted = expected
        if until <= now or (wanted is not None and wanted != value):
            return False
        del self._echo[key]
        LOGGER.debug("Rotel <<< echo of our own %s command: %s", key, value)
        return True

    def _expect_echo(self, echo: Mapping[str, str | None] | None) -> None:
        """Remember which fields the device is going to answer a command with."""
        if not echo:
            return
        until = asyncio.get_running_loop().time() + PUSH_ECHO_TTL
        self._echo.update({key: (until, value) for key, value in echo.items()})

    def _async_queue_push(self, batch: dict[str, str] | None) -> None:
        """Hand a batch to the listener, dropping the oldest one on overflow."""
        if self._pushes.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._pushes.get_nowait()
            self.dropped_pushes += 1
        self._pushes.put_nowait(batch)

    def _fail_exchanges(self, error: RotelApiError) -> None:
        """Wake every exchange that is still waiting for the device."""
        while self._waiters:
            self._waiters.popleft().fail(error)

    async def async_next_push(self) -> dict[str, str] | None:
        """Wait for the next report the device sends on its own.

        ``None`` means the connection went away; the caller keeps waiting,
        because the next exchange opens a new one and starts a new read task.
        """
        return await self._pushes.get()

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
        echo: Mapping[str, str | None] | None = None,
    ) -> dict[str, str]:
        """Send ``lines`` and return the fields the device reported.

        Reading stops as soon as every key of ``required`` has been seen plus
        the rest of the burst that follows it, so a poll returns a complete
        snapshot.
        """
        await self._async_open_locked()
        self._expect_echo(echo)
        exchange = _Exchange(required or set(), self._socket_timeout)
        self._waiters.append(exchange)
        try:
            for line in lines:
                await self._async_write_locked(line)
                if line.endswith("?"):
                    self._asked.add(line.removesuffix("?"))
            return await exchange.wait()
        finally:
            if exchange in self._waiters:
                self._waiters.remove(exchange)
            exchange.close()

    async def _async_exchange(
        self,
        lines: list[str],
        *,
        required: set[str] | None = None,
        echo: Mapping[str, str | None] | None = None,
    ) -> dict[str, str]:
        """Exchange under the lock, reconnecting and retrying once on error."""
        async with self._lock:
            try:
                return await self._async_exchange_locked(
                    lines, required=required, echo=echo
                )
            except (RotelApiConnectionError, RotelApiProtocolError):
                # The socket may have been closed by the peer (Rotel units
                # drop idle connections); reconnect and try once more.
                LOGGER.debug("Rotel: retrying %r after connection error", lines)
                await self._async_close_locked()
                return await self._async_exchange_locked(
                    lines, required=required, echo=echo
                )

    async def _async_command(
        self, line: str, *, echo: Mapping[str, str | None] | None = None
    ) -> None:
        """Send a command and keep the late fields it may trigger.

        ``echo`` says which fields the device is going to answer with, so a
        reply that arrives after this exchange is over is still recognised as
        the confirmation of our own command.
        """
        async with self._lock:
            try:
                await self._async_exchange_locked([line], echo=echo)
            except RotelApiConnectionError:
                LOGGER.debug("Rotel: retrying %r after connection error", line)
                await self._async_close_locked()
                await self._async_exchange_locked([line], echo=echo)

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

    def decode_fields(
        self, fields: Mapping[str, str], *, strict: bool = False
    ) -> dict[str, Any]:
        """Decode reported fields into the attributes of the state.

        The keys of the result are the attributes the entities read, so a
        caller can apply the result on top of what it already knows. Only the
        fields that are present are decoded, which is what lets the very same
        function turn a full poll and a single unsolicited report into state.
        """
        model = self._model
        state: dict[str, Any] = {}

        def put(
            attribute: str,
            parser: Callable[[str], Any],
            payload: str,
            name: str | None = None,
        ) -> None:
            """Decode one field into ``attribute``, unless it fails."""
            value = self._parse(name or attribute, parser, payload, strict)
            if value is not None:
                state[attribute] = value

        for name in (POWER, MUTE):
            if (payload := fields.get(name)) is not None:
                put(name, parse_on_off, payload)

        if (volume := fields.get(VOLUME)) is not None:
            # The raw value is kept as the device reported it: it is the only
            # thing a user can compare with the display of the amplifier.
            state["volume_raw"] = volume[:MAX_VALUE_LENGTH]
            if (number := parse_volume_number(volume)) is not None and (
                volume_is_out_of_range(number, model)
            ):
                lowest, highest = volume_payload_range(model)
                LOGGER.warning(
                    "Rotel reported volume %r which is outside the %s..%s the %s "
                    "profile expects. Adjust the volume scale of the profile if the "
                    "volume behaves incorrectly",
                    volume,
                    lowest,
                    highest,
                    model.key,
                )
            put(
                "volume_db",
                lambda value: volume_from_payload(value, model),
                volume,
                VOLUME,
            )

        for name, record in ((SOURCE, False), (RECORD_SOURCE, True)):
            if (payload := fields.get(name)) is None:
                continue
            put(
                name,
                partial(parse_source, model=model, record=record),
                payload,
            )

        for name, attribute in ((BASS, "bass_db"), (TREBLE, "treble_db")):
            if (payload := fields.get(name)) is not None:
                put(attribute, parse_tone, payload, name)

        if (payload := fields.get(BALANCE)) is not None:
            put("balance", parse_balance, payload)

        for key in TONE_KEYS:
            if (payload := fields.get(key)) is None:
                continue
            put("tone_bypass", parse_on_off, payload, key)
            break

        if (payload := fields.get(SPEAKER)) is not None:
            # A speaker reply describes both groups at once, so it fills two
            # attributes instead of one.
            groups = self._parse(SPEAKER, parse_speakers, payload, strict)
            if groups is not None:
                state["speaker_a"], state["speaker_b"] = groups

        if (payload := fields.get(DIMMER)) is not None:
            put("dimmer", parse_dimmer, payload)

        for name, attribute in ((MODEL, "device_model"), (FIRMWARE, "firmware")):
            if (payload := fields.get(name)) is not None:
                state[attribute] = payload[:MAX_VALUE_LENGTH]

        return state

    async def async_get_status(self) -> RotelStatus:
        """Poll the amplifier for its full state.

        All queries go out in one write: the device answers every one of
        them, and the replies usually arrive in a single TCP segment. Only
        the power state is mandatory — a field the firmware does not answer
        stays unknown instead of failing the whole poll.
        """
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
        # The tone bypass is answered under one of two names. Whichever the
        # firmware reports is remembered as the one its commands have to use,
        # so the switch works on old and current units alike.
        answered_tone = self._answered_tone_key(received)
        if answered_tone is not None:
            self._tone_key = answered_tone
        # Fields that did not arrive must not keep a stale value of an older
        # poll, and an unanswered optional query is remembered as unsupported.
        for name in optional:
            if name in TONE_KEYS and answered_tone is not None:
                continue
            if name not in received:
                unsupported.add(name)
                self._values.pop(name, None)

        decoded = self.decode_fields(received, strict=True)
        # The device information is asked once per connection, so the answer
        # of an earlier poll still describes the amplifier.
        for name, attribute in ((MODEL, "device_model"), (FIRMWARE, "firmware")):
            if attribute not in decoded and (cached := self._values.get(name)):
                decoded[attribute] = cached[:MAX_VALUE_LENGTH]
            if name not in self._values:
                unsupported.add(name)

        status = RotelStatus.from_fields(decoded, frozenset(unsupported))
        if status.power:
            self._last_volume_db = status.volume_db
        return status

    def _answered_tone_key(self, fields: Mapping[str, str]) -> str | None:
        """Return the spelling of the tone bypass this firmware reports."""
        return next((key for key in TONE_KEYS if fields.get(key)), None)

    async def async_set_power(self, power: bool) -> None:
        """Switch the amplifier to standby or on.

        Rotel units apply an interlock on power-up: the pre-out relays stay
        open until a volume has been set. Re-sending the last known volume
        removes the ~1 s "no sound after standby" surprise.
        """
        state = "on" if power else "standby"
        await self._async_command(
            build_command(RotelCommand.POWER_ON if power else RotelCommand.POWER_OFF),
            echo={POWER: state},
        )
        self._values[POWER] = state
        if power and self._last_volume_db is not None:
            with contextlib.suppress(RotelApiError):
                await asyncio.sleep(INTERLOCK_VOLUME_DELAY)
                await self.async_set_volume(self._last_volume_db)

    async def async_set_volume(self, volume_db: float) -> float:
        """Set the volume in dB and return the value the device accepted."""
        snapped = snap_volume(volume_db, self._model)
        payload = volume_to_payload(snapped, self._model)
        await self._async_command(
            build_command(RotelCommand.VOLUME, payload), echo={VOLUME: str(payload)}
        )
        self._values[VOLUME] = str(payload)
        self._last_volume_db = snapped
        return snapped

    async def async_set_mute(self, mute: bool) -> None:
        """Mute or unmute the amplifier."""
        state = "on" if mute else "off"
        await self._async_command(
            build_command(RotelCommand.MUTE_ON if mute else RotelCommand.MUTE_OFF),
            echo={MUTE: state},
        )
        self._values[MUTE] = state

    async def async_set_source(self, source: str) -> RotelInput:
        """Select an input by label or protocol value."""
        resolved = self._require_source(source, self._model)
        await self._async_command(
            source_command(resolved.value), echo={SOURCE: resolved.value}
        )
        self._values[SOURCE] = resolved.value
        return resolved

    async def async_set_record_source(self, source: str) -> RotelInput:
        """Select the record input (pre-out/record path) of an RCX."""
        if not self._model.record_inputs:
            raise RotelApiProtocolError(f"{self._model.name} has no record source")
        resolved = self._require_source(source, self._model, record=True)
        await self._async_command(
            source_command(f"rec_{resolved.value}"),
            echo={RECORD_SOURCE: resolved.value},
        )
        self._values[RECORD_SOURCE] = resolved.value
        return resolved

    # --- tone controls ---------------------------------------------------

    async def async_set_tone(self, command: RotelCommand, value: float) -> int:
        """Set bass or treble and return the number of dB the device accepted."""
        if command is RotelCommand.BASS:
            field = BASS
        elif command is RotelCommand.TREBLE:
            field = TREBLE
        else:
            raise ValueError(f"{command} is not a tone control")
        clamped = clamp_tone(value)
        reported = f"{clamped:+03d}" if clamped else "000"
        await self._async_command(
            tone_command(command, clamped), echo={field: reported}
        )
        self._values[field] = reported
        return clamped

    async def async_set_balance(self, balance: float) -> int:
        """Set the channel balance and return the value the device accepted.

        Negative is left, positive is right, ``0`` re-centres it.
        """
        clamped = clamp_balance(balance)
        reported = (
            "000" if not clamped else f"{'L' if clamped < 0 else 'R'}{abs(clamped):02d}"
        )
        await self._async_command(balance_command(clamped), echo={BALANCE: reported})
        self._values[BALANCE] = reported
        return clamped

    async def async_set_tone_bypass(self, bypass: bool) -> bool:
        """Bypass or re-enable the tone block."""
        key = self._tone_key or TONE_KEYS[0]
        state = "on" if bypass else "off"
        await self._async_command(f"{key}_{state}!", echo={key: state})
        self._values[key] = state
        return bypass

    async def async_set_speaker(self, group: str, enabled: bool) -> bool:
        """Switch one speaker group on or off.

        The device reports the *result* of the change (``speaker=a``, ``b``,
        ``a_b`` or ``off``), and this optimistic value assumes the command was
        accepted; the next poll corrects it either way.
        """
        token = group.strip().casefold()
        if token not in self._model.speaker_groups:
            raise RotelApiProtocolError(
                f"{self._model.name} has no speaker group {group!r}"
            )
        command = {
            ("a", True): RotelCommand.SPEAKER_A_ON,
            ("a", False): RotelCommand.SPEAKER_A_OFF,
            ("b", True): RotelCommand.SPEAKER_B_ON,
            ("b", False): RotelCommand.SPEAKER_B_OFF,
        }[(token, enabled)]
        # Any answer counts: the device reports the resulting state of both
        # groups, which is not the command that was sent.
        await self._async_command(build_command(command), echo={SPEAKER: None})
        return enabled

    async def async_set_dimmer(self, level: float) -> int:
        """Set the front display brightness (``0`` is the brightest)."""
        clamped = clamp_dimmer(level)
        await self._async_command(
            dimmer_command(clamped), echo={DIMMER: str(clamped)}
        )
        self._values[DIMMER] = str(clamped)
        return clamped

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

    def _parse[T](
        self,
        name: str,
        parser: Callable[[str], T],
        payload: str,
        strict: bool = False,
    ) -> T | None:
        """Decode one field, honouring how strictly the caller needs it.

        A reply we cannot interpret must not cost the amplifier its state, so
        the field is skipped and the matching entity reports itself as
        unavailable. A poll asks for more: a device that answers a field every
        amplifier reports with nonsense is not talking to us, and that has to
        surface as an error instead of a plausible looking half state.
        """
        try:
            return parser(payload)
        except (ProtocolError, TypeError, ValueError) as err:
            LOGGER.debug("Unexpected %s reply %r: %s", name, payload, err)
            if strict and name in CORE_FIELDS:
                raise RotelApiProtocolError(
                    f"Unexpected {name} reply: {payload!r}"
                ) from err
            return None

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
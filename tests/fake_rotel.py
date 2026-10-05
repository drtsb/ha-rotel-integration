"""A minimal fake Rotel amplifier for the test suite.

It speaks the protocol of a real unit on TCP 9590: every query is ``key?``,
every command ends with ``!``, no CR/LF is used, and every reply field is
terminated with ``$``. Commands carry no delimiter of their own beyond the
``?``/``!`` that closes them, so the fake has to scan the stream for them.
"""

from __future__ import annotations

import asyncio
import contextlib

#: Terminator of a command, as documented by Rotel.
COMMAND_TERMINATOR = "!"
#: Terminator of every field the device sends.
RESPONSE_TERMINATOR = "$"
#: Characters that close a command.
COMMAND_ENDS = "?!"

#: How long a unit with auto update waits before repeating a change it made
#: as an unsolicited report. Long enough to arrive after the confirmation of
#: the command itself, exactly as a real one does.
AUTO_UPDATE_DELAY = 0.15

#: Query key -> the state attribute that answers it. ``bypass`` and ``tone``
#: are two spellings of the same switch, and a device answers only the one its
#: firmware knows (see ``legacy_tone``).
QUERIES = {
    "power": "power",
    "volume": "volume",
    "mute": "mute",
    "source": "source",
    "record_source": "record_source",
    "model": "model",
    "version": "firmware",
    "bypass": "tone_bypass",
    "tone": "tone_bypass",
    "bass": "bass",
    "treble": "treble",
    "balance": "balance",
    "speaker": "speaker",
    "dimmer": "dimmer",
}


#: First word of a command -> the state attribute that command changes. The
#: source commands carry no prefix of their own, so everything else is a
#: source selection.
COMMAND_ATTRIBUTES = {
    "power": "power",
    "mute": "mute",
    "vol": "volume",
    "rec": "record_source",
    "speaker": "speaker",
    "balance": "balance",
    "dimmer": "dimmer",
}


def _pop_command(buffer: str) -> tuple[str, str]:
    """Split the first complete command off ``buffer``, terminator included."""
    for index, char in enumerate(buffer):
        if char in COMMAND_ENDS:
            return buffer[: index + 1], buffer[index + 1 :]
    return "", buffer


def _field(key: str, value: object) -> str:
    """Format a reply field the way the device does."""
    return f"{key}={value}{RESPONSE_TERMINATOR}"


def _signed(value: int) -> str:
    """Format a tone block value: ``000``, ``+05``, ``-10``."""
    if not value:
        return "000"
    return f"{value:+03d}"


def _balance(value: int) -> str:
    """Format a balance: ``000``, ``L15``, ``R03``."""
    if not value:
        return "000"
    side = "L" if value < 0 else "R"
    return f"{side}{abs(value):02d}"


class FakeRotel:
    """Minimal Rotel amplifier speaking the ASCII protocol."""

    def __init__(
        self,
        *,
        power: bool = True,
        volume: int = 30,
        mute: bool = False,
        source: str = "tuner",
        record_source: str = "cd",
        model: str = "RA1572",
        firmware: str = "1.24",
        bass: int = 0,
        treble: int = 0,
        balance: int = 0,
        tone_bypass: bool = False,
        speaker: str = "a",
        dimmer: int = 2,
        legacy_tone: bool = False,
        ignore: frozenset[str] = frozenset(),
        drop_next: bool = False,
        fragment: bool = False,
        quote: bool = False,
        field_delay: float = 0.0,
        auto_update: bool = False,
    ) -> None:
        """Initialise the fake device."""
        self.power = power
        self.volume = volume
        self.mute = mute
        self.source = source
        self.record_source = record_source
        self.model = model
        self.firmware = firmware
        self.bass = bass
        self.treble = treble
        self.balance = balance
        self.tone_bypass = tone_bypass
        #: ``off``, ``a``, ``b`` or ``a_b``.
        self.speaker = speaker
        self.dimmer = dimmer
        #: Firmware that reports the tone bypass as "tone" instead of "bypass".
        self.legacy_tone = legacy_tone
        #: Query keys the device does not answer (as an old firmware would).
        self.ignore = ignore
        self.drop_next = drop_next
        #: Send every reply byte by byte, like a device with a slow UART.
        self.fragment = fragment
        #: Wrap every value in quotes, as some firmware revisions do.
        self.quote = quote
        #: Pause between the answers of a poll, like a busy device.
        self.field_delay = field_delay
        #: Report a change as soon as it was made at the front panel, which is
        #: what a unit with "auto update" enabled does.
        self.auto_update = auto_update
        self.received: list[str] = []
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        #: Delayed auto update reports, cancelled when the device stops.
        self._reports: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> FakeRotel:
        """Start listening on a random loopback port."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        """Stop the server and drop open client connections.

        Python 3.12 makes ``Server.wait_closed()`` wait for the handlers, so the
        sockets opened by the api under test must be closed first.
        """
        for task in self._reports:
            task.cancel()
        self._reports.clear()
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def report(self, **fields: str) -> None:
        """Send fields the way an auto update device does: without being asked.

        The state of the fake is not touched, so a test decides what the
        amplifier "really" is and what it reports.
        """
        payload = "".join(_field(key, value) for key, value in fields.items())
        for writer in self._open_writers():
            await self._send(writer, payload)

    def _open_writers(self) -> list[asyncio.StreamWriter]:
        """Return the connections that are still open, forgetting the others."""
        self._writers = [writer for writer in self._writers if not writer.is_closing()]
        return list(self._writers)

    def _report_later(self, writer: asyncio.StreamWriter, command: str) -> None:
        """Report a command we just applied as an unsolicited change.

        A unit with auto update enabled does not only answer the command, it
        also announces the new state a moment later — the same message a knob
        turned by hand produces.
        """
        task = asyncio.create_task(self._send_report(writer, command))
        self._reports.add(task)
        task.add_done_callback(self._reports.discard)

    async def _send_report(self, writer: asyncio.StreamWriter, command: str) -> None:
        """Wait for the device to catch up, then report what it changed."""
        await asyncio.sleep(AUTO_UPDATE_DELAY)
        with contextlib.suppress(OSError):
            # The connection may be gone by now: a report nobody receives is
            # what a real amplifier does when Home Assistant went away.
            await self._send(writer, self._state_report(command))

    def _state_report(self, command: str) -> str:
        """Return the field that describes the amplifier after ``command``."""
        name = command.removesuffix(COMMAND_TERMINATOR)
        if name in {"bypass_on", "bypass_off"}:
            return _field("bypass", self._reported("tone_bypass"))
        if name in {"tone_on", "tone_off"}:
            return _field("tone", self._reported("tone_bypass"))
        if (attribute := COMMAND_ATTRIBUTES.get(name.partition("_")[0])) is not None:
            return _field(attribute, self._reported(attribute))
        return _field("source", self._reported("source"))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one connection until the client goes away."""
        self.connections += 1
        self._writers.append(writer)
        buffer = ""
        try:
            while True:
                data = await reader.read(512)
                if not data:
                    return
                buffer += data.decode("ascii", "ignore")
                while True:
                    command, buffer = _pop_command(buffer)
                    if not command:
                        break
                    self.received.append(command)
                    if self.drop_next:
                        # Hang up on the very next command, like a device that
                        # drops idle connections.
                        self.drop_next = False
                        writer.close()
                        return
                    if reply := self._reply(command):
                        if self.field_delay:
                            await asyncio.sleep(self.field_delay)
                        await self._send(writer, reply)
                        if self.auto_update and "=" in reply:
                            self._report_later(writer, command)
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover
            return

    async def _send(self, writer: asyncio.StreamWriter, reply: str) -> None:
        """Send a reply, optionally byte by byte."""
        payload = reply.encode("ascii")
        if self.fragment:
            for index in range(len(payload)):
                writer.write(payload[index : index + 1])
                await writer.drain()
        else:
            writer.write(payload)
        await writer.drain()

    def _reply(self, command: str) -> str:
        """Return the reply a single command deserves."""
        if command.endswith("?"):
            return self._answer_query(command.removesuffix("?"))
        return self._apply_command(command.removesuffix(COMMAND_TERMINATOR))

    def _answer_query(self, name: str) -> str:
        """Return the ``key=value$`` answer of a query."""
        if name in {"bypass", "tone"}:
            # Only the spelling this firmware knows is answered.
            wanted = "tone" if self.legacy_tone else "bypass"
            if name != wanted:
                return f"?{RESPONSE_TERMINATOR}"
        attribute = QUERIES.get(name)
        if attribute is None or name in self.ignore:
            return f"?{RESPONSE_TERMINATOR}"
        return _field(name, self._reported(attribute))

    def _reported(self, attribute: str) -> str:
        """Return the value the device reports for one of its attributes."""
        value = {
            "power": "on" if self.power else "standby",
            "volume": str(self.volume),
            "mute": "on" if self.mute else "off",
            "source": self.source,
            "record_source": self.record_source,
            "model": self.model,
            "firmware": self.firmware,
            "bass": _signed(self.bass),
            "treble": _signed(self.treble),
            "balance": _balance(self.balance),
            "tone_bypass": "on" if self.tone_bypass else "off",
            "speaker": self.speaker,
            "dimmer": str(self.dimmer),
        }[attribute]
        return f'"{value}"' if self.quote else str(value)

    def _apply_command(self, command: str) -> str:
        """Apply a command and report the resulting state."""
        if command.startswith("rec_"):
            self.record_source = command.removeprefix("rec_")
            return _field("record_source", self.record_source)
        if command.startswith("vol_"):
            self.volume = int(command.removeprefix("vol_"))
            return _field("volume", self.volume)
        if command in {"power_on", "power_off"}:
            self.power = command == "power_on"
            return _field("power", self._reported("power"))
        if command in {"mute_on", "mute_off"}:
            self.mute = command == "mute_on"
            return _field("mute", self._reported("mute"))
        if command.startswith(("bass_", "treble_")):
            attribute, _, payload = command.partition("_")
            setattr(self, attribute, int(payload))
            return _field(attribute, self._reported(attribute))
        if command.startswith("balance_"):
            self.balance = _parse_balance(command.removeprefix("balance_"))
            return _field("balance", _balance(self.balance))
        if command.startswith("speaker_"):
            return _field("speaker", self._apply_speaker(command))
        if command.startswith("dimmer_"):
            self.dimmer = int(command.removeprefix("dimmer_"))
            return _field("dimmer", self.dimmer)
        if command in {"bypass_on", "bypass_off"} and not self.legacy_tone:
            self.tone_bypass = command == "bypass_on"
            return _field("bypass", self._reported("tone_bypass"))
        if command in {"tone_on", "tone_off"} and self.legacy_tone:
            self.tone_bypass = command == "tone_on"
            return _field("tone", self._reported("tone_bypass"))
        if command in QUERIES:  # not a command the device knows
            return f"?{RESPONSE_TERMINATOR}"
        self.source = command
        return _field("source", self.source)

    def _apply_speaker(self, command: str) -> str:
        """Apply a speaker command and return the resulting speaker state.

        ``speaker_a_on!``/``speaker_a_off!`` set a group, while the shorter
        ``speaker_a!`` toggles it, exactly as the documentation describes.
        """
        _, group, action = command.split("_", 2)
        groups = set(self.speaker.split("_")) - {"off"}
        if action in {"on", "off"}:
            groups = groups | {group} if action == "on" else groups - {group}
        else:
            groups = groups - {group} if group in groups else groups | {group}
        if not groups:
            self.speaker = "off"
        elif groups == {"a", "b"}:
            self.speaker = "a_b"
        else:
            self.speaker = next(iter(groups))
        return self.speaker


def _parse_balance(token: str) -> int:
    """Decode a ``balance_`` payload into a signed value."""
    token = token.strip().lower()
    if token in {"000", "0"}:
        return 0
    side, magnitude = token[0], int(token[1:])
    return -magnitude if side == "l" else magnitude
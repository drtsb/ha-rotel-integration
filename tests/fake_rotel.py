"""A minimal fake Rotel amplifier for the test suite.

It speaks the protocol of a real unit on TCP 9590: every query is ``key?``,
every command ends with ``!``, no CR/LF is used, and every reply field is
terminated with ``$``. Commands carry no delimiter of their own beyond the
``?``/``!`` that closes them, so the fake has to scan the stream for them.
"""

from __future__ import annotations

import asyncio

#: Terminator of a command, as documented by Rotel.
COMMAND_TERMINATOR = "!"
#: Terminator of every field the device sends.
RESPONSE_TERMINATOR = "$"
#: Characters that close a command.
COMMAND_ENDS = "?!"

#: Query key -> the state attribute that answers it.
QUERIES = {
    "power": "power",
    "volume": "volume",
    "mute": "mute",
    "source": "source",
    "record_source": "record_source",
    "model": "model",
    "version": "firmware",
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
        ignore: frozenset[str] = frozenset(),
        drop_next: bool = False,
        fragment: bool = False,
        quote: bool = False,
        field_delay: float = 0.0,
    ) -> None:
        """Initialise the fake device."""
        self.power = power
        self.volume = volume
        self.mute = mute
        self.source = source
        self.record_source = record_source
        self.model = model
        self.firmware = firmware
        #: Query keys the device does not answer (as an old firmware would).
        self.ignore = ignore
        self.drop_next = drop_next
        #: Send every reply byte by byte, like a device with a slow UART.
        self.fragment = fragment
        #: Wrap every value in quotes, as some firmware revisions do.
        self.quote = quote
        #: Pause between the answers of a poll, like a busy device.
        self.field_delay = field_delay
        self.received: list[str] = []
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
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
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

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
        if command in QUERIES:  # not a command the device knows
            return f"?{RESPONSE_TERMINATOR}"
        self.source = command
        return _field("source", self.source)
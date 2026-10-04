"""A minimal fake Rotel amplifier for the test suite."""

from __future__ import annotations

import asyncio


class FakeRotel:
    """Minimal Rotel amplifier speaking the ASCII protocol."""

    def __init__(
        self,
        *,
        power: bool = True,
        volume: int = 95,
        mute: bool = False,
        source: str = "TUNER",
        model: str = "RA1572",
        firmware: str = "1.24",
        echo: bool = False,
        ignore: frozenset[str] = frozenset(),
        drop_next: bool = False,
    ) -> None:
        """Initialise the fake device."""
        self.power = power
        self.volume = volume
        self.mute = mute
        self.source = source
        self.model = model
        self.firmware = firmware
        self.echo = echo
        self.ignore = ignore
        self.drop_next = drop_next
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

        Python 3.12 makes ``Server.wait_closed()`` wait for the handlers, so
        the sockets opened by the api under test must be closed first.
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
        """Serve one connection."""
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.decode("ascii", "ignore").strip()
                self.received.append(text)
                if self.drop_next:
                    # Hang up on the very next command, like a device that
                    # drops idle connections.
                    self.drop_next = False
                    writer.close()
                    return
                for reply in self._replies(text):
                    if self.echo:
                        writer.write(text.encode("ascii") + b"\r\n")
                    writer.write(reply.encode("ascii") + b"\r\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):  # pragma: no cover
            return

    def _replies(self, command: str) -> list[str]:
        """Return the replies a command deserves."""
        keyword, _, value = command.partition(" ")
        keyword = keyword.upper()
        if keyword in {item.upper() for item in self.ignore}:
            return []
        if keyword == "POWER_QUERY":
            return ["POWER 1" if self.power else "POWER 0"]
        if keyword == "POWER":
            self.power = value == "on"
            return ["OK"]
        if keyword == "VOLUME_QUERY":
            return [f"VOLUME {self.volume}"]
        if keyword == "VOLUME":
            self.volume = int(value)
            return ["OK"]
        if keyword == "MUTE_QUERY":
            return ["MUTE 1" if self.mute else "MUTE 0"]
        if keyword == "MUTE":
            self.mute = value == "on"
            return ["OK"]
        if keyword == "SOURCE_QUERY":
            return [f"SOURCE {self.source}"]
        if keyword == "SOURCE":
            self.source = value
            return ["OK"]
        if keyword == "REC_SELECT_QUERY":
            return ["REC_SELECT CD"]
        if keyword == "MODEL_QUERY":
            return [f"MODEL {self.model}"]
        if keyword == "FIRMWARE_QUERY":
            return [f"FIRMWARE {self.firmware}"]
        return []

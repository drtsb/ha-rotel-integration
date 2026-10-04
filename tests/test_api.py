"""Tests for the asyncio TCP client against a fake Rotel amplifier."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import PACKAGE_NAME  # noqa: F401  (registers the package)
from fake_rotel import FakeRotel


def make_api(
    api_module: Any,
    protocol_module: Any,
    device: FakeRotel,
    **kwargs: Any,
) -> Any:
    """Build an api instance pointed at the fake device."""
    kwargs.setdefault("connect_timeout", 1.0)
    kwargs.setdefault("socket_timeout", 0.5)
    return api_module.RotelApi(
        "127.0.0.1",
        device.port,
        protocol_module.get_model("ra1572"),
        **kwargs,
    )


@pytest.fixture(name="device")
async def device_fixture(socket_enabled: None):
    """Start a fake amplifier for every test.

    ``socket_enabled`` lifts the socket sandbox that Home Assistant's test
    plugin installs for the whole session.
    """
    device = FakeRotel()
    await device.start()
    yield device
    await device.stop()


async def test_connect_sends_a_query_and_verifies_the_protocol(
    api_module, protocol_module, device
) -> None:
    """Connecting is only successful when the device answers a query."""
    api = make_api(api_module, protocol_module, device)

    await api.async_connect()

    assert api.connected
    assert "power?" in device.received
    assert not any(command.endswith("!") for command in device.received)


async def test_connect_fails_on_a_silent_port(api_module, protocol_module, device) -> None:
    """A port that accepts the socket but never answers is not a Rotel."""
    device.ignore = frozenset({"power"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    with pytest.raises(api_module.RotelApiProtocolError):
        await api.async_connect()
    assert not api.connected


async def test_get_status_reports_every_value(api_module, protocol_module, device) -> None:
    """A poll fills the complete snapshot."""
    api = make_api(api_module, protocol_module, device)
    status = await api.async_get_status()

    assert status.power is True
    assert status.volume_raw == "30"
    assert status.volume_db == pytest.approx(-45.0)
    assert status.mute is False
    assert status.source is not None
    assert status.source.name == "Tuner"
    assert status.model == "RA1572"
    assert status.firmware == "1.24"
    # The RA-1572 has no record source, so it is never asked for one.
    assert status.unsupported == frozenset()
    assert "record_source?" not in device.received


async def test_get_status_asks_for_every_field_once(
    api_module, protocol_module, device
) -> None:
    """A poll asks for the state fields, the device info only once."""
    api = make_api(api_module, protocol_module, device)
    await api.async_get_status()
    device.received.clear()

    await api.async_get_status()

    # Everything the profile declares, on every poll, so an entity that shows
    # a tone or a display setting always follows the device.
    assert sorted(device.received) == sorted(
        [f"{key}?" for key in protocol_module.get_model("ra1572").queries]
        + ["mute?", "power?", "source?", "volume?"]
    )


async def test_set_power_and_volume_round_trip(api_module, protocol_module, device) -> None:
    """Commands reach the device and the reported state follows."""
    api = make_api(api_module, protocol_module, device)
    await api.async_set_volume(-60.0)

    status = await api.async_get_status()
    assert status.volume_db == -60.0
    assert "vol_0!" in device.received


async def test_reply_split_across_segments(api_module, protocol_module, device) -> None:
    """A device that dribbles its reply out is understood as well."""
    device.fragment = True
    api = make_api(api_module, protocol_module, device)
    status = await api.async_get_status()

    assert status.power is True
    assert status.source is not None


async def test_power_interlock_resends_the_volume(api_module, protocol_module, device) -> None:
    """Powering on re-sends the volume so the pre-out relays open."""
    api = make_api(api_module, protocol_module, device)
    await api.async_set_volume(-60.0)
    device.received.clear()

    device.power = False
    await api.async_set_power(True)

    assert "power_on!" in device.received
    assert "vol_0!" in device.received


async def test_unsupported_query_degrades_to_none(api_module, protocol_module, device) -> None:
    """An old firmware without model? still yields a snapshot."""
    device.ignore = frozenset({"model"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    status = await api.async_get_status()
    assert status.power is True
    assert status.model is None
    assert "model" in status.unsupported


async def test_unsupported_query_is_asked_only_once(
    api_module, protocol_module, device
) -> None:
    """A field the firmware ignores is not re-asked on every poll."""
    device.ignore = frozenset({"model"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    await api.async_get_status()
    device.received.clear()
    await api.async_get_status()
    await api.async_get_status()

    assert "model?" not in device.received


async def test_record_source_of_a_processor(api_module, protocol_module, device) -> None:
    """An RCX is asked for its record source and can set it."""
    api = api_module.RotelApi(
        "127.0.0.1",
        device.port,
        protocol_module.get_model("rcx1500"),
        connect_timeout=1.0,
        socket_timeout=0.5,
    )
    status = await api.async_get_status()
    assert status.record_source is not None
    assert status.record_source.name == "CD"
    assert "record_source?" in device.received

    resolved = await api.async_set_record_source("Tuner")
    assert resolved.name == "Tuner"
    assert "rec_tuner!" in device.received


async def test_out_of_range_volume_is_logged(
    api_module, protocol_module, device, caplog: pytest.LogCaptureFixture
) -> None:
    """A volume the profile cannot hold is reported instead of clamped silently."""
    device.volume = 200
    api = make_api(api_module, protocol_module, device)

    with caplog.at_level("WARNING"):
        status = await api.async_get_status()

    assert "outside" in caplog.text
    assert status.volume_db == protocol_module.get_model("ra1572").volume_max_db


async def test_wrong_protocol_raises(api_module, protocol_module, device) -> None:
    """A silent device is reported as a protocol error, not as state."""
    device.ignore = frozenset({"power", "volume", "mute", "source"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    with pytest.raises(api_module.RotelApiProtocolError):
        await api.async_get_status()


async def test_connection_error(api_module, protocol_module, device) -> None:
    """An unreachable device raises a connection error."""
    await device.stop()
    api = make_api(api_module, protocol_module, device, connect_timeout=0.5)

    with pytest.raises(api_module.RotelApiConnectionError):
        await api.async_get_status()


async def test_device_that_closes_the_connection(api_module, protocol_module, socket_enabled: None) -> None:
    """A device that hangs up right away is reported as closed."""
    import asyncio

    async def _reset(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept the connection and close it immediately."""
        writer.close()

    server = await asyncio.start_server(_reset, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    api = api_module.RotelApi(
        "127.0.0.1",
        port,
        protocol_module.get_model("ra1572"),
        connect_timeout=1.0,
        socket_timeout=0.5,
    )
    try:
        with pytest.raises(api_module.RotelApiClosedError):
            await api.async_connect()
        assert not api.connected
    finally:
        await api.async_disconnect()
        server.close()
        await server.wait_closed()


async def test_reconnects_after_the_device_hangs_up(api_module, protocol_module, device) -> None:
    """A dropped connection is re-established transparently."""
    api = make_api(api_module, protocol_module, device)
    assert await api.async_query(protocol_module.RotelQuery.POWER) == "on"

    device.drop_next = True
    assert await api.async_query(protocol_module.RotelQuery.POWER) == "on"
    assert device.connections >= 2


async def test_validate_returns_device_information(api_module, protocol_module, device) -> None:
    """The config flow helper reports model and firmware."""
    api = make_api(api_module, protocol_module, device)
    info = await api.async_validate()
    assert info == {"model": "RA1572", "firmware": "1.24"}
    assert not api.connected


async def test_select_source_by_label(api_module, protocol_module, device) -> None:
    """A source label is translated into the protocol value."""
    api = make_api(api_module, protocol_module, device)
    resolved = await api.async_set_source("Phono")
    assert resolved.value == "phono"
    assert "phono!" in device.received


async def test_select_unknown_source(api_module, protocol_module, device) -> None:
    """An unknown source raises a protocol error listing the valid ones."""
    api = make_api(api_module, protocol_module, device)
    with pytest.raises(api_module.RotelApiProtocolError, match="Known sources"):
        await api.async_set_source("FLAC")

async def test_model_and_firmware_are_asked_again_after_a_failed_connect(
    api_module, protocol_module, device
) -> None:
    """A failed connect must not consume the "asked once" promise.

    The queries are written before the device answers, so a connection that
    fails could otherwise keep model/version out of every later poll.
    """
    device.ignore = frozenset({"power"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)
    with pytest.raises(api_module.RotelApiProtocolError):
        await api.async_connect()

    device.ignore = frozenset()
    status = await api.async_get_status()

    assert "model?" in device.received
    assert "version?" in device.received
    assert status.model == "RA1572"
    assert status.firmware == "1.24"
    assert not status.unsupported


async def test_quoted_values_are_understood(api_module, protocol_module, device) -> None:
    """Firmware that wraps values in quotes does not break the poll."""
    device.quote = True
    api = make_api(api_module, protocol_module, device)

    status = await api.async_get_status()

    assert status.power is True
    assert status.mute is False
    assert status.volume_db == protocol_module.volume_from_payload(
        str(device.volume), protocol_module.get_model("ra1572")
    )
    assert status.source is not None
    assert status.source.name == "Tuner"


async def test_unframed_data_disconnects_the_device(
    api_module, protocol_module, socket_enabled: None
) -> None:
    """A peer that never terminates its replies cannot fill memory."""
    import asyncio

    payload = b"volume=30" * 2048  # no '$' anywhere

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(64)
            writer.write(payload)
            await writer.drain()
            await reader.read()  # return as soon as the client hangs up
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    api = api_module.RotelApi(
        "127.0.0.1",
        port,
        protocol_module.get_model("ra1572"),
        connect_timeout=1.0,
        socket_timeout=0.5,
    )
    try:
        with pytest.raises(api_module.RotelApiConnectionError):
            await api.async_get_status()
        assert not api.connected
    finally:
        await api.async_disconnect()
        server.close()
        await server.wait_closed()


async def test_cached_values_are_truncated(api_module, protocol_module, device) -> None:
    """A device cannot push unbounded strings into state and diagnostics."""
    from custom_components.rotel_control.const import MAX_VALUE_LENGTH

    device.firmware = "9" * (MAX_VALUE_LENGTH * 4)
    api = make_api(api_module, protocol_module, device)

    status = await api.async_get_status()

    assert len(status.firmware or "") == MAX_VALUE_LENGTH
    assert len(api.values["version"]) == MAX_VALUE_LENGTH


async def test_slow_burst_is_collected_in_one_poll(
    api_module, protocol_module, socket_enabled: None
) -> None:
    """Answers that arrive over more than the drain window are not lost."""
    import asyncio

    slow = FakeRotel(field_delay=0.03)
    await slow.start()
    api = make_api(api_module, protocol_module, slow, socket_timeout=1.0)
    try:
        status = await api.async_get_status()
    finally:
        await api.async_disconnect()
        await slow.stop()
        # let the sleeping handler of the fake device finish
        await asyncio.sleep(0.1)

    assert status.power is True
    assert status.volume_db is not None
    assert status.mute is False
    assert status.source is not None
    assert status.source.name == "Tuner"
    assert not status.unsupported

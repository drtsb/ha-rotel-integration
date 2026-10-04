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


async def test_get_status_reports_every_value(api_module, protocol_module, device) -> None:
    """A poll fills the complete snapshot."""
    api = make_api(api_module, protocol_module, device)
    status = await api.async_get_status()

    assert status.power is True
    assert status.volume_db == -12.5
    assert status.mute is False
    assert status.source is not None
    assert status.source.name == "Tuner"
    assert status.model == "RA1572"
    assert status.firmware == "1.24"
    # The RA-1572 has no record source, so it is never asked for one.
    assert status.unsupported == frozenset()
    assert not any(
        command.startswith("REC_SELECT") for command in device.received
    )


async def test_set_power_and_volume_round_trip(api_module, protocol_module, device) -> None:
    """Commands reach the device and the reported state follows."""
    api = make_api(api_module, protocol_module, device)
    await api.async_set_volume(-24.0)

    status = await api.async_get_status()
    assert status.volume_db == -24.0
    assert "VOLUME 72" in device.received


async def test_commands_survive_an_echo(api_module, protocol_module, device) -> None:
    """A device that echoes its commands still parses correctly."""
    device.echo = True
    api = make_api(api_module, protocol_module, device)
    status = await api.async_get_status()
    assert status.power is True
    assert status.volume_db == -12.5


async def test_power_interlock_resends_the_volume(api_module, protocol_module, device) -> None:
    """Powering on re-sends the volume so the pre-out relays open."""
    api = make_api(api_module, protocol_module, device)
    await api.async_set_volume(-24.0)
    device.received.clear()

    device.power = False
    await api.async_set_power(True)

    assert "POWER on" in device.received
    assert "VOLUME 72" in device.received


async def test_unsupported_query_degrades_to_none(api_module, protocol_module, device) -> None:
    """An old firmware without MODEL_QUERY still yields a snapshot."""
    device.ignore = frozenset({"MODEL_QUERY"})
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    status = await api.async_get_status()
    assert status.power is True
    assert status.model is None
    assert "MODEL_QUERY" in status.unsupported


async def test_wrong_protocol_raises(api_module, protocol_module, device) -> None:
    """A silent device is reported as a protocol error, not as state."""
    device.ignore = frozenset(
        {"POWER_QUERY", "VOLUME_QUERY", "MUTE_QUERY", "SOURCE_QUERY"}
    )
    api = make_api(api_module, protocol_module, device, socket_timeout=0.2)

    with pytest.raises(api_module.RotelApiProtocolError):
        await api.async_get_status()


async def test_connection_error(api_module, protocol_module, device) -> None:
    """An unreachable device raises a connection error."""
    await device.stop()
    api = make_api(api_module, protocol_module, device, connect_timeout=0.5)

    with pytest.raises(api_module.RotelApiConnectionError):
        await api.async_get_status()


async def test_reconnects_after_the_device_hangs_up(api_module, protocol_module, device) -> None:
    """A dropped connection is re-established transparently."""
    api = make_api(api_module, protocol_module, device)
    assert await api.async_query(api_module.RotelCommand.POWER_QUERY) == "1"

    device.drop_next = True
    assert await api.async_query(api_module.RotelCommand.POWER_QUERY) == "1"
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
    assert resolved.value == "PHONO"
    assert "SOURCE PHONO" in device.received


async def test_select_unknown_source(api_module, protocol_module, device) -> None:
    """An unknown source raises a protocol error listing the valid ones."""
    api = make_api(api_module, protocol_module, device)
    with pytest.raises(api_module.RotelApiProtocolError, match="Known sources"):
        await api.async_set_source("FLAC")

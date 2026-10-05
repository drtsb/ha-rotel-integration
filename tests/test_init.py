"""End-to-end tests: config flow, setup and entities against real Home Assistant."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_rotel import AUTO_UPDATE_DELAY, FakeRotel
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.components.ssdp import SsdpServiceInfo
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rotel_control.const import (
    CONF_INPUTS,
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    CONF_PUSH_UPDATES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    EVENT_COMMAND_RECEIVED,
    ORIGIN_POLL,
    ORIGIN_PUSH,
)
from custom_components.rotel_control.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.rotel_control.protocol import (
    ROTEL_MODELS,
    get_model,
    volume_to_payload,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _entry_data(device: FakeRotel, model: str = "ra1572") -> dict[str, Any]:
    """Return config entry data pointing at the fake amplifier."""
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: device.port,
        CONF_NAME: "Living room",
        CONF_MODEL_PROFILE: model,
    }


def _schema_defaults(result: dict[str, Any]) -> dict[str, Any]:
    """Return the values the form is pre-filled with."""
    return {
        str(key): key.default()
        for key in result["data_schema"].schema
        if key.default is not vol_undefined()
    }


def vol_undefined() -> Any:
    """Return the voluptuous marker for "no default"."""
    import voluptuous as vol

    return vol.UNDEFINED


def _suggested_values(result: dict[str, Any]) -> dict[str, Any]:
    """Return the values Home Assistant suggests for the submitted form."""
    suggested: dict[str, Any] = {}
    for key in result["data_schema"].schema:
        description = getattr(key, "description", None)
        if isinstance(description, dict) and "suggested_value" in description:
            suggested[str(key)] = description["suggested_value"]
    return suggested


async def _async_setup(
    hass: HomeAssistant, device: FakeRotel, model: str = "ra1572"
) -> MockConfigEntry:
    """Add the integration through the config flow and set it up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: device.port,
            CONF_MODEL_PROFILE: model,
            CONF_NAME: "Living room",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry: MockConfigEntry = result["result"]  # type: ignore[assignment]
    return entry


async def test_config_flow_creates_and_sets_up_the_entry(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A verified connection creates a loaded entry with all entities."""
    entry = await _async_setup(hass, device)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == "127.0.0.1"
    assert entry.title == "Living room"

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_update_success
    assert coordinator.data.power is True
    assert coordinator.data.volume_db == pytest.approx(-45.0)
    assert coordinator.data.device_model == "RA1572"

    states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("media_player.")
    ]
    assert states, "no media_player entity was created"


async def test_default_port_is_9590(hass: HomeAssistant) -> None:
    """The form offers the port every Rotel network unit listens on."""
    assert DEFAULT_PORT == 9590

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert _schema_defaults(result)[CONF_PORT] == 9590


async def test_all_platforms_are_forwarded(hass: HomeAssistant, device: FakeRotel) -> None:
    """Every platform of the entry is set up."""
    entry = await _async_setup(hass, device)
    registry = er.async_get(hass)

    assert registry.async_get("media_player.living_room_amplifier")
    assert registry.async_get("number.living_room_volume")
    assert registry.async_get("number.living_room_bass")
    assert registry.async_get("number.living_room_treble")
    assert registry.async_get("number.living_room_balance")
    assert registry.async_get("select.living_room_input")
    assert registry.async_get("select.living_room_display_brightness")
    assert registry.async_get("switch.living_room_speakers_a")
    assert registry.async_get("switch.living_room_speakers_b")
    assert registry.async_get("switch.living_room_tone_bypass")
    assert registry.async_get("sensor.living_room_control_connection")
    assert entry.state is ConfigEntryState.LOADED


async def test_device_registry_entry(hass: HomeAssistant, device: FakeRotel) -> None:
    """The amplifier is grouped under one device with a manufacturer."""
    await _async_setup(hass, device)
    registry = dr.async_get(hass)

    entry = registry.async_get_device(identifiers={(DOMAIN, "127.0.0.1")})
    assert entry is not None
    assert entry.manufacturer == "Rotel"
    assert entry.model == get_model("ra1572").name
    assert entry.sw_version == "1.24"


async def test_media_player_state_and_volume(hass: HomeAssistant, device: FakeRotel) -> None:
    """The media player exposes state, volume in percent and dB."""
    await _async_setup(hass, device)
    state = hass.states.get("media_player.living_room_amplifier")

    assert state is not None
    assert state.state == STATE_ON
    assert state.attributes["source"] == "Tuner"
    # -45 dB on the -60..20 dB scale of the profile
    assert state.attributes["volume_level"] == pytest.approx(0.188, abs=0.01)
    assert state.attributes["is_volume_muted"] is False
    assert state.attributes["volume_raw"] == "30"
    assert "Phono" in state.attributes["source_list"]
    features = state.attributes["supported_features"]
    assert features & 128  # SELECT_SOURCE
    assert features & 256  # TURN_OFF


@pytest.mark.parametrize(
    ("label", "command"),
    [
        ("CD", "cd!"),
        ("Tuner", "tuner!"),
        ("Phono", "phono!"),
        ("Coax 1", "coax1!"),
        ("Coax 2", "coax2!"),
        ("Optical 1", "opt1!"),
        ("Optical 2", "opt2!"),
        ("Aux", "aux!"),
        ("Balanced", "bal_xlr!"),
        ("USB", "usb!"),
        ("PC USB", "pcusb!"),
        ("Bluetooth", "bluetooth!"),
    ],
)
async def test_generic_profile_offers_the_documented_inputs(
    hass: HomeAssistant, socket_enabled: None, label: str, command: str
) -> None:
    """An unknown unit is offered every input, each with its own command."""
    device = FakeRotel(source="aux", model="RC-1590")
    await device.start()
    try:
        await _async_setup(hass, device, model="generic")
        state = hass.states.get("media_player.living_room_amplifier")

        assert state.attributes["source_list"] == [
            "CD",
            "Tuner",
            "Phono",
            "Coax 1",
            "Coax 2",
            "Optical 1",
            "Optical 2",
            "Aux",
            "Balanced",
            "USB",
            "PC USB",
            "Bluetooth",
        ]
        assert state.attributes["source"] == "Aux"
        # One balanced input, reported by the hardware as bal_xlr.
        assert (
            len(
                [
                    name
                    for name in state.attributes["source_list"]
                    if name.startswith("Balanced")
                ]
            )
            == 1
        )

        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.living_room_input", "option": label},
            blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        await device.stop()

    assert command in device.received
    assert hass.states.get("select.living_room_input").state == label


async def test_turn_off_and_on_go_to_the_device(hass: HomeAssistant, device: FakeRotel) -> None:
    """Power commands reach the amplifier and the state follows."""
    await _async_setup(hass, device)

    await hass.services.async_call(
        "media_player",
        "turn_off",
        {"entity_id": "media_player.living_room_amplifier"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "power_off!" in device.received
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_OFF

    await hass.services.async_call(
        "media_player",
        "turn_on",
        {"entity_id": "media_player.living_room_amplifier"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "power_on!" in device.received
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_ON


async def test_set_volume_level_converts_to_db(hass: HomeAssistant, device: FakeRotel) -> None:
    """A 0..1 volume is translated into a device step."""
    await _async_setup(hass, device)

    await hass.services.async_call(
        "media_player",
        "volume_set",
        {"entity_id": "media_player.living_room_amplifier", "volume_level": 0.5},
        blocking=True,
    )
    await hass.async_block_till_done()

    model = get_model("ra1572")
    expected = model.volume_min_db + 0.5 * model.volume_range_db
    assert f"vol_{volume_to_payload(expected, model)}!" in device.received


async def test_mute_toggle_service(hass: HomeAssistant, device: FakeRotel) -> None:
    """Mute toggling reaches the amplifier."""
    await _async_setup(hass, device)

    await hass.services.async_call(
        "media_player",
        "volume_mute",
        {"entity_id": "media_player.living_room_amplifier", "is_volume_muted": True},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "mute_on!" in device.received
    state = hass.states.get("media_player.living_room_amplifier")
    assert state.attributes["is_volume_muted"] is True


async def test_select_source_service(hass: HomeAssistant, device: FakeRotel) -> None:
    """Selecting an input via the select entity updates the source."""
    await _async_setup(hass, device)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.living_room_input", "option": "Phono"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "phono!" in device.received
    state = hass.states.get("select.living_room_input")
    assert state.state == "Phono"
    assert "Line 1" in state.attributes["options"]


async def test_number_entity_sets_db(hass: HomeAssistant, device: FakeRotel) -> None:
    """The dB number entity has the range of the profile."""
    await _async_setup(hass, device)
    state = hass.states.get("number.living_room_volume")

    assert state is not None
    assert state.attributes["min"] == -60.0
    assert state.attributes["max"] == 20.0
    assert state.attributes["step"] == 0.5
    assert state.attributes["unit_of_measurement"] == "dB"

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.living_room_volume", "value": -30.0},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert "vol_60!" in device.received


# --- tone controls ------------------------------------------------------


async def test_tone_numbers_report_the_device_values(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Bass, treble and balance start at what the amplifier reported."""
    device = FakeRotel(bass=-4, treble=6, balance=-3)
    await device.start()
    try:
        await _async_setup(hass, device)
    finally:
        await device.stop()

    bass = hass.states.get("number.living_room_bass")
    assert bass.attributes["min"] == -10.0
    assert bass.attributes["max"] == 10.0
    assert bass.attributes["step"] == 1.0
    assert bass.attributes["unit_of_measurement"] == "dB"
    assert bass.state == "-4.0"
    assert hass.states.get("number.living_room_treble").state == "6.0"
    # Left is negative, so the slider is centred on 0.
    assert hass.states.get("number.living_room_balance").state == "-3.0"


@pytest.mark.parametrize(
    ("entity_id", "value", "expected"),
    [
        ("number.living_room_bass", -5.0, "bass_-05!"),
        ("number.living_room_bass", 0.0, "bass_000!"),
        ("number.living_room_bass", 8.0, "bass_+08!"),
        ("number.living_room_treble", -2.0, "treble_-02!"),
        ("number.living_room_treble", 10.0, "treble_+10!"),
        ("number.living_room_balance", -7.0, "balance_l07!"),
        ("number.living_room_balance", 0.0, "balance_000!"),
        ("number.living_room_balance", 12.0, "balance_r12!"),
    ],
)
async def test_tone_numbers_send_the_rotel_commands(
    hass: HomeAssistant, device: FakeRotel, entity_id: str, value: float, expected: str
) -> None:
    """A tone value is translated into the documented command."""
    await _async_setup(hass, device)

    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )
    await hass.async_block_till_done()

    assert expected in device.received
    # The state follows what the device confirms.
    assert hass.states.get(entity_id).state == f"{device_state(device, entity_id)}"


def device_state(device: FakeRotel, entity_id: str) -> float:
    """Return the value the fake device now reports for a tone entity."""
    if entity_id.endswith("bass"):
        return float(device.bass)
    if entity_id.endswith("treble"):
        return float(device.treble)
    return float(device.balance)


async def test_speaker_switches_use_the_explicit_commands(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """Each speaker group is switched on and off on its own."""
    await _async_setup(hass, device)
    assert hass.states.get("switch.living_room_speakers_a").state == STATE_ON
    assert hass.states.get("switch.living_room_speakers_b").state == STATE_OFF

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.living_room_speakers_b"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "speaker_b_on!" in device.received
    assert device.speaker == "a_b"
    assert hass.states.get("switch.living_room_speakers_a").state == STATE_ON
    assert hass.states.get("switch.living_room_speakers_b").state == STATE_ON

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.living_room_speakers_a"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "speaker_a_off!" in device.received
    assert device.speaker == "b"
    assert hass.states.get("switch.living_room_speakers_a").state == STATE_OFF
    assert hass.states.get("switch.living_room_speakers_b").state == STATE_ON


async def test_tone_bypass_switch(hass: HomeAssistant, device: FakeRotel) -> None:
    """The tone block is bypassed and enabled again."""
    await _async_setup(hass, device)
    assert hass.states.get("switch.living_room_tone_bypass").state == STATE_OFF

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.living_room_tone_bypass"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "bypass_on!" in device.received
    assert device.tone_bypass is True
    assert hass.states.get("switch.living_room_tone_bypass").state == STATE_ON

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.living_room_tone_bypass"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "bypass_off!" in device.received
    assert hass.states.get("switch.living_room_tone_bypass").state == STATE_OFF


async def test_tone_bypass_switch_on_legacy_firmware(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Firmware that answers "tone" instead of "bypass" still works."""
    device = FakeRotel(legacy_tone=True)
    await device.start()
    try:
        await _async_setup(hass, device)
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": "switch.living_room_tone_bypass"},
            blocking=True,
        )
        await hass.async_block_till_done()
    finally:
        await device.stop()

    assert "bypass_on!" not in device.received
    assert "tone_on!" in device.received
    assert device.tone_bypass is True
    assert hass.states.get("switch.living_room_tone_bypass").state == STATE_ON


async def test_display_dimmer_select(hass: HomeAssistant, device: FakeRotel) -> None:
    """Every brightness level is offered and maps to one command."""
    await _async_setup(hass, device)
    state = hass.states.get("select.living_room_display_brightness")

    assert state.attributes["options"] == ["0", "1", "2", "3", "4", "5", "6"]
    assert state.state == "2"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.living_room_display_brightness", "option": "5"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "dimmer_5!" in device.received
    assert hass.states.get("select.living_room_display_brightness").state == "5"


async def test_tone_entities_are_unavailable_without_the_control(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A device that does not answer a query shows the entity as unavailable."""
    device = FakeRotel(
        ignore=frozenset({"bypass", "bass", "treble", "balance", "speaker", "dimmer"})
    )
    await device.start()
    try:
        entry = await _async_setup(hass, device)
    finally:
        await device.stop()

    for entity_id in (
        "number.living_room_bass",
        "number.living_room_treble",
        "number.living_room_balance",
        "switch.living_room_speakers_a",
        "switch.living_room_tone_bypass",
        "select.living_room_display_brightness",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    # The amplifier itself is untouched by an unsupported control.
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_ON

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "bass" in diagnostics["state"]["unsupported_queries"]


async def test_media_player_exposes_the_tone_state(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """The extended state is readable on the media player as well."""
    device = FakeRotel(bass=2, treble=-1, balance=4, dimmer=0, speaker="a_b")
    await device.start()
    try:
        await _async_setup(hass, device)
    finally:
        await device.stop()

    attributes = hass.states.get("media_player.living_room_amplifier").attributes
    assert attributes["bass_db"] == 2
    assert attributes["treble_db"] == -1
    assert attributes["balance"] == 4
    assert attributes["tone_bypass"] is False
    assert attributes["speakers_a"] is True
    assert attributes["speakers_b"] is True
    assert attributes["display_dimmer"] == 0


async def test_cannot_connect_shows_an_error(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A closed port is reported as cannot_connect instead of creating an entry."""
    await device.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: device.port, CONF_MODEL_PROFILE: "ra1572"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_failed_attempt_keeps_the_entered_values(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A failed attempt must not reset the form (issue: fields were cleared)."""
    await device.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: device.port,
            CONF_MODEL_PROFILE: "rcx1500",
            CONF_NAME: "Salon",
        },
    )

    assert result["type"] == FlowResultType.FORM
    defaults = _schema_defaults(result)
    assert defaults[CONF_HOST] == "127.0.0.1"
    assert defaults[CONF_PORT] == device.port
    assert defaults[CONF_MODEL_PROFILE] == "rcx1500"
    assert defaults[CONF_NAME] == "Salon"
    # Home Assistant also gets the values as suggestions, which is what the
    # frontend uses to keep the input of a rejected form.
    assert _suggested_values(result) == {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: device.port,
        CONF_MODEL_PROFILE: "rcx1500",
        CONF_NAME: "Salon",
    }


async def test_port_without_rotel_protocol_is_reported_separately(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A port that answers nothing at all gets its own error message."""
    clients: list[asyncio.StreamWriter] = []

    async def _silent(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept the connection and never answer."""
        clients.append(writer)
        await reader.read()

    server = await asyncio.start_server(_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "127.0.0.1",
                CONF_PORT: port,
                CONF_MODEL_PROFILE: "ra1572",
            },
        )
    finally:
        for writer in clients:
            writer.close()
        server.close()
        await server.wait_closed()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "no_response"}


async def test_device_that_closes_the_connection_is_reported(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """An amplifier that hangs up right away gets its own error."""
    clients: list[asyncio.StreamWriter] = []

    async def _reset(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept the connection and close it immediately."""
        clients.append(writer)
        writer.close()

    server = await asyncio.start_server(_reset, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "127.0.0.1",
                CONF_PORT: port,
                CONF_MODEL_PROFILE: "ra1572",
            },
        )
    finally:
        for writer in clients:
            writer.close()
        server.close()
        await server.wait_closed()

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "device_closed"}


async def test_duplicate_host_is_aborted(hass: HomeAssistant, device: FakeRotel) -> None:
    """A second entry for the same host is refused."""
    await _async_setup(hass, device)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: device.port, CONF_MODEL_PROFILE: "ra1572"},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_model_is_detected_from_the_device(hass: HomeAssistant, device: FakeRotel) -> None:
    """The profile reported by the device wins over the selected one."""
    device.model = "RCX1500"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: device.port, CONF_MODEL_PROFILE: "generic"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL_PROFILE] == "rcx1500"

    await hass.async_block_till_done()
    entry = result["result"]
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.model.key == "rcx1500"
    # RCX models expose a record source; the entity is registered but disabled.
    registry = er.async_get(hass)
    record_entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}-record_input"
    )
    assert record_entity_id is not None
    assert registry.async_get(record_entity_id).disabled_by is (
        er.RegistryEntryDisabler.INTEGRATION
    )


async def test_ssdp_discovery_probes_the_control_port(
    hass: HomeAssistant, device: FakeRotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovered amplifier is verified and its port pre-filled."""
    monkeypatch.setattr(
        "custom_components.rotel_control.config_flow.CANDIDATE_PORTS", (device.port,)
    )
    info = SsdpServiceInfo(
        ssdp_usn="uuid:rotel-ra1572::urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_st="urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_location="http://127.0.0.1:8080/description.xml",
        upnp={
            "manufacturer": "Rotel",
            "friendlyName": "Rotel RA-1572",
            "modelName": "RA-1572",
        },
        x_homeassistant_matching_domains={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "ssdp"}, data=info
    )

    assert result["type"] == FlowResultType.FORM
    defaults = _schema_defaults(result)
    assert defaults[CONF_HOST] == "127.0.0.1"
    assert defaults[CONF_PORT] == device.port
    assert defaults[CONF_MODEL_PROFILE] == "ra1572"
    assert defaults[CONF_NAME] == "Rotel RA-1572"


async def test_ssdp_discovery_ignores_a_device_without_rotel(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """An unrelated announcement must not start a flow."""
    info = SsdpServiceInfo(
        ssdp_usn="uuid:printer::urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_st="urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_location="http://127.0.0.1:8080/description.xml",
        upnp={"manufacturer": "HP"},
        x_homeassistant_matching_domains={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "ssdp"}, data=info
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_rotel"


async def test_ssdp_discovery_aborts_when_nothing_answers(
    hass: HomeAssistant, device: FakeRotel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device that does not speak Rotel on any port is not offered."""
    await device.stop()
    monkeypatch.setattr(
        "custom_components.rotel_control.config_flow.CANDIDATE_PORTS", (device.port,)
    )
    info = SsdpServiceInfo(
        ssdp_usn="uuid:rotel-ra1572::urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_st="urn:schemas-upnp-org:device:MediaRenderer:1",
        ssdp_location="http://127.0.0.1:8080/description.xml",
        upnp={"manufacturer": "Rotel"},
        x_homeassistant_matching_domains={},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "ssdp"}, data=info
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "not_rotel"


async def test_reauth_updates_a_broken_entry(hass: HomeAssistant, device: FakeRotel) -> None:
    """Reauthentication re-validates and restores the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=_entry_data(device), unique_id="127.0.0.1"
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert _schema_defaults(result)[CONF_PORT] == device.port

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: device.port, CONF_MODEL_PROFILE: "ra1572"},
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_keeps_the_values_after_a_failure(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A failed reconfigure keeps the entry untouched and the form filled in."""
    entry = await _async_setup(hass, device)
    await device.stop()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: 9600, CONF_MODEL_PROFILE: "ra1572"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert _suggested_values(result)[CONF_PORT] == 9600
    assert entry.data[CONF_PORT] == device.port


async def test_options_flow_changes_the_poll_interval(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """Changing the interval reloads the entry with the new value."""
    entry = await _async_setup(hass, device)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: 30,
            CONF_MODEL_PROFILE: "ra1572",
            CONF_INPUTS: [item.value for item in get_model("ra1572").inputs],
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval.total_seconds() == 30


async def test_options_flow_limits_the_inputs(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A unit with fewer inputs than its profile only shows what it has."""
    entry = await _async_setup(hass, device)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            CONF_MODEL_PROFILE: "ra1572",
            CONF_INPUTS: ["cd", "tuner", "bal_xlr", "usb"],
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert [item.value for item in coordinator.model.inputs] == [
        "cd",
        "tuner",
        "bal_xlr",
        "usb",
    ]
    state = hass.states.get("select.living_room_input")
    assert state.attributes["options"] == ["CD", "Tuner", "Balanced", "USB"]


async def test_unload_removes_the_socket(hass: HomeAssistant, device: FakeRotel) -> None:
    """Unloading the entry closes the TCP connection."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.api.connected

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator.api.connected
    assert DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]
    # Home Assistant keeps a placeholder state for removed entities.
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_UNAVAILABLE


async def test_entities_go_unavailable_when_the_device_disappears(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A device that stops answering makes the entities unavailable."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Stop answering and force a refresh instead of waiting for the interval.
    await device.stop()
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    state = hass.states.get("media_player.living_room_amplifier")
    assert state.state == STATE_UNAVAILABLE


async def test_manifest_declares_discovery_correctly() -> None:
    """The manifest uses the matchers Home Assistant supports.

    ``ssdp`` entries must be dicts (the XML file form is legacy) and are
    matched exactly on ``manufacturer``/``st``/``deviceType``.
    """
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "rotel_control"
            / "manifest.json"
        ).read_text()
    )

    assert manifest["domain"] == DOMAIN
    # The amplifier reports the changes it makes by itself, so the integration
    # is not a pure poller any more.
    assert manifest["iot_class"] == "local_push"
    assert manifest["config_flow"] is True
    assert manifest["version"]
    # Home Assistant bootstraps ssdp/zeroconf itself, so neither belongs in
    # the dependencies of a config entry.
    assert "ssdp" not in manifest.get("dependencies", [])

    assert manifest["ssdp"]
    for matcher in manifest["ssdp"]:
        assert isinstance(matcher, dict), "ssdp matchers must be dicts"
        assert matcher.keys() <= {"manufacturer", "st", "nt", "deviceType"}
        assert "manufacturer" in matcher

    assert manifest["zeroconf"]
    for service_type in manifest["zeroconf"]:
        assert service_type.endswith("._tcp.local.")

async def test_state_is_readable_before_the_first_poll(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """Entities and diagnostics survive a coordinator without data yet.

    The first refresh normally gates the setup, but a reload or a manual
    refresh can still leave the coordinator without state, and a state
    property that raises would take the whole platform down.
    """
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    def _drop_state() -> None:
        """Drop the state, as a failed first refresh would."""
        coordinator.data = None
        coordinator.last_update_success = False

    _drop_state()

    player = hass.data["entity_components"]["media_player"].get_entity(
        "media_player.living_room_amplifier"
    )
    assert player.state is MediaPlayerState.OFF
    assert player.volume_level is None
    assert player.is_volume_muted is None
    assert player.source is None
    # Nothing is known yet, so the raw volume is absent and the tone controls
    # read as unknown rather than as a value the device never reported.
    attributes = player.extra_state_attributes
    assert "volume_raw" not in attributes
    assert attributes["bass_db"] is None
    assert attributes["tone_bypass"] is None
    assert attributes["display_dimmer"] is None

    number_entity = hass.data["entity_components"]["number"].get_entity(
        "number.living_room_volume"
    )
    assert number_entity.native_value is None

    select_entity = hass.data["entity_components"]["select"].get_entity(
        "select.living_room_input"
    )
    assert select_entity.current_option is None

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["state"]["power"] is None
    assert diagnostics["state"]["unsupported_queries"] == []


async def test_commands_work_without_previous_state(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A command applied to an unknown state does not raise."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    coordinator.data = None
    coordinator.last_update_success = False

    await coordinator.async_set_power(False)
    assert coordinator.data is not None
    assert coordinator.data.power is False

    coordinator.data = None
    coordinator.last_update_success = False

    await coordinator.async_set_volume(-40.0)
    assert coordinator.data is not None
    assert coordinator.data.volume_db == pytest.approx(-40.0)

    coordinator.data = None
    coordinator.last_update_success = False

    await coordinator.async_set_mute(True)
    assert coordinator.data is not None
    assert coordinator.data.mute is True

    coordinator.data = None
    coordinator.last_update_success = False

    await coordinator.async_set_source("CD")
    assert coordinator.data is not None
    assert coordinator.data.source is not None
    assert coordinator.data.source.name == "CD"


async def test_candidate_ports_are_probed_concurrently(monkeypatch) -> None:
    """Probing must not serialise a connect timeout per candidate port."""
    import asyncio

    from custom_components.rotel_control import config_flow
    from custom_components.rotel_control.protocol import get_model

    active = 0
    peak = 0
    model = get_model("ra1572")

    async def _fake_probe(host, port, model_key=None, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return model, {"model": "RA1572"}

    monkeypatch.setattr(config_flow, "_async_probe", _fake_probe)

    found = await config_flow.async_find_rotel_port("127.0.0.1", (9590, 9600, 9500))

    assert found is not None
    assert found[0] == 9590
    assert peak == 3, "the ports were probed one after another"


async def test_manifest_does_not_match_every_http_device() -> None:
    """A generic zeroconf type would start a flow for every device on the LAN."""
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "rotel_control"
            / "manifest.json"
        ).read_text()
    )

    assert "_http._tcp.local." not in manifest["zeroconf"]
    for service_type in manifest["zeroconf"]:
        assert "rotel" in service_type


def test_readme_documents_every_profile() -> None:
    """The README table must not drift away from the profiles."""
    readme = (
        Path(__file__).parent.parent / "README.md"
    ).read_text()
    # Only the profile table: other tables may well start with a backticked
    # lowercase word of their own.
    table = readme.split("| Profile | Model |", 1)[1].split("\n\n", 1)[0]

    documented = set(re.findall(r"^\| `([a-z0-9]+)` \|", table, re.MULTILINE))
    assert documented == set(ROTEL_MODELS)
    for key, model in ROTEL_MODELS.items():
        if key == "rbx1500":
            assert "Balanced 1–4" in readme
            assert len(
                [item for item in model.inputs if item.name.startswith("Balanced")]
            ) == 4


# --- reports of the device ----------------------------------------------


async def _async_wait_until(condition: Callable[[], bool], what: str) -> None:
    """Wait until a background task of the integration reacted.

    The listener that follows the reports of the amplifier is a background
    task, which ``async_block_till_done`` deliberately does not wait for.
    """
    for _ in range(100):
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"the coordinator never reacted to {what}")


def _capture(events: list[Event]) -> Any:
    """Return a bus listener that collects the events of the integration."""

    @callback
    def _on_event(event: Event) -> None:
        events.append(event)

    return _on_event


async def test_report_of_the_device_updates_the_entities(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A change the amplifier reports on its own is applied right away."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # No poll may run, so the state can only come from the report itself.
    coordinator.update_interval = None
    events: list[Event] = []
    hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _capture(events))

    await device.report(volume="20", source="phono")

    await _async_wait_until(
        lambda: coordinator.data.source is not None
        and coordinator.data.source.value == "phono",
        "the report of the device",
    )
    state = hass.states.get("media_player.living_room_amplifier")
    assert state.attributes["source"] == "Phono"
    assert state.attributes["volume_raw"] == "20"
    assert state.attributes["volume_level"] == pytest.approx(0.125, abs=0.01)
    assert coordinator.push_reports == 1
    assert len(events) == 1
    data = events[0].data
    assert data["origin"] == ORIGIN_PUSH
    assert data["host"] == "127.0.0.1"
    assert data["port"] == device.port
    assert data["entry_id"] == entry.entry_id
    assert data["device_id"] is not None
    assert data["changes"]["source"] == {"old": "tuner", "new": "phono"}
    assert data["changes"]["volume_db"] == {
        "old": pytest.approx(-45.0),
        "new": pytest.approx(-50.0),
    }


async def test_report_of_a_field_nobody_asked_for(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A report only replaces the field it carries."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_interval = None

    await device.report(speaker="a_b")

    await _async_wait_until(
        lambda: bool(coordinator.data.speaker_b), "the speaker report"
    )
    assert coordinator.data.speaker_a is True
    assert coordinator.data.speaker_b is True
    # Everything else survived the report.
    assert coordinator.data.volume_db == pytest.approx(-45.0)
    assert coordinator.data.source is not None
    assert hass.states.get("switch.living_room_speakers_b").state == STATE_ON
    assert hass.states.get("switch.living_room_speakers_a").state == STATE_ON


async def test_report_of_a_repeated_value_is_not_announced(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """Saying what we already know is not a change of the amplifier."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_interval = None
    events: list[Event] = []
    hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _capture(events))

    await device.report(volume="30")
    await _async_wait_until(
        lambda: coordinator.push_reports == 1, "the repeated report"
    )
    await hass.async_block_till_done()

    assert not events


async def test_change_seen_by_a_poll_is_announced(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A unit that does not push is followed by the poll, and it announces."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_interval = None
    events: list[Event] = []
    hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _capture(events))

    device.volume = 20  # the volume knob was turned
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.volume_db == pytest.approx(-50.0)
    assert len(events) == 1
    assert events[0].data["origin"] == ORIGIN_POLL
    assert events[0].data["changes"]["volume_db"]["new"] == pytest.approx(-50.0)


async def test_a_command_of_home_assistant_is_not_announced(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """An echo of our own command is no reason to disturb an automation.

    The device reports the change it was told to make, once as the answer to
    the command and once as an automatic update. Neither is a change somebody
    made at the amplifier.
    """
    device = FakeRotel(auto_update=True)
    await device.start()
    try:
        entry = await _async_setup(hass, device)
        coordinator = hass.data[DOMAIN][entry.entry_id]
        events: list[Event] = []
        hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _capture(events))

        await coordinator.async_set_volume(-40.0)
        await asyncio.sleep(AUTO_UPDATE_DELAY + 0.3)
        await hass.async_block_till_done()

        assert coordinator.data.volume_db == pytest.approx(-40.0)
        # The device did repeat the change, and none of it was announced.
        assert "vol_40!" in device.received
        assert coordinator.push_reports >= 1
        assert not events
    finally:
        await device.stop()


async def test_an_event_reports_a_state_the_entities_already_show(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """An automation may read the entities straight from the event."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.update_interval = None
    seen: list[tuple[Event, str | None]] = []

    @callback
    def _on_event(event: Event) -> None:
        state = hass.states.get("media_player.living_room_amplifier")
        seen.append((event, state.attributes["source"]))

    hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _on_event)

    # The RA-1572 reports its first optical input as "coax1".
    await device.report(source="opt1")
    await _async_wait_until(lambda: bool(seen), "the report of the device")

    assert seen[0][0].data["changes"]["source"] == {"old": "tuner", "new": "coax1"}
    assert seen[0][1] == "Optical Coax 1"


async def test_reports_can_be_turned_off(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """The option stops the listener, and polling takes over its job."""
    entry = await _async_setup(hass, device)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
            CONF_MODEL_PROFILE: "ra1572",
            CONF_INPUTS: [item.value for item in get_model("ra1572").inputs],
            CONF_PUSH_UPDATES: False,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert not coordinator.listening
    events: list[Event] = []
    hass.bus.async_listen(EVENT_COMMAND_RECEIVED, _capture(events))

    device.volume = 20
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # The state is still correct, and it is still announced: the device never
    # pushed, the poll saw the change.
    assert coordinator.data.volume_db == pytest.approx(-50.0)
    assert [event.data["origin"] for event in events] == [ORIGIN_POLL]


async def test_unload_stops_the_report_listener(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """Nothing keeps reading the socket of an unloaded entry."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.listening

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert not coordinator.listening
    assert not coordinator.api.listening


async def test_diagnostics_describe_the_reports(
    hass: HomeAssistant, device: FakeRotel
) -> None:
    """A bug report shows whether the device ever reported anything."""
    entry = await _async_setup(hass, device)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    await device.report(volume="44")
    await _async_wait_until(
        lambda: coordinator.push_reports == 1, "the report of the device"
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["reports"] == {
        "enabled": True,
        "listener_running": True,
        "applied": 1,
        "events": 1,
        "dropped": 0,
    }
    assert diagnostics["connection"]["listening"] is True
    assert diagnostics["state"]["volume_db"] == pytest.approx(-38.0)


# --- brand assets -------------------------------------------------------

BRAND_DIR = Path(__file__).parent.parent / "custom_components" / "rotel_control" / "brand"

#: Home Assistant serves the images of a custom integration from
#: ``custom_components/<domain>/brand/`` and HACS expects the same folder, so
#: the sizes are the ones the brand rules ask for: a square icon, its double
#: size, and a landscape logo in both resolutions.
BRAND_SIZES = {
    "icon.png": (256, 256),
    "icon@2x.png": (512, 512),
    "logo.png": (512, 256),
    "logo@2x.png": (1024, 512),
}


@pytest.mark.parametrize(("name", "size"), BRAND_SIZES.items())
def test_brand_images_are_shipped(name: str, size: tuple[int, int]) -> None:
    """The integration folder carries the logo the UI shows."""
    from PIL import Image

    path = BRAND_DIR / name
    assert path.is_file(), f"{path} is missing"
    with Image.open(path) as image:
        assert image.size == size
        assert image.mode == "RGBA"


def test_repository_root_carries_the_hacs_brand() -> None:
    """HACS reads the icon of a repository from its root."""
    from PIL import Image

    root = Path(__file__).parent.parent
    with Image.open(root / "icon.png") as image:
        assert image.size == (512, 512)
    assert (root / "logo.png").is_file()

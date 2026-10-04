"""End-to-end tests: config flow, setup and entities against real Home Assistant."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fake_rotel import FakeRotel
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
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rotel_control.const import (
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
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


async def _async_setup(hass: HomeAssistant, device: FakeRotel) -> MockConfigEntry:
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
            CONF_MODEL_PROFILE: "ra1572",
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
    assert registry.async_get("select.living_room_input")
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
        result["flow_id"], {CONF_POLL_INTERVAL: 30, CONF_MODEL_PROFILE: "ra1572"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.update_interval.total_seconds() == 30


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
    assert manifest["iot_class"] == "local_polling"
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
    assert player.extra_state_attributes == {}

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

    documented = set(re.findall(r"^\| `([a-z0-9]+)` \|", readme, re.MULTILINE))
    assert documented == set(ROTEL_MODELS)
    for key, model in ROTEL_MODELS.items():
        if key == "rbx1500":
            assert "Balanced 1–4" in readme
            assert len(
                [item for item in model.inputs if item.name.startswith("Balanced")]
            ) == 4

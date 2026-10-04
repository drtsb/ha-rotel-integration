"""End-to-end tests: config flow, setup and entities against real Home Assistant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fake_rotel import FakeRotel
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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rotel_control.const import (
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.rotel_control.protocol import (
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


async def _async_setup(hass: HomeAssistant, device: FakeRotel) -> MockConfigEntry:
    """Add the integration through the config flow and set it up."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == "form"
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
    assert result["type"] == "create_entry"
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
    assert coordinator.data.volume_db == -12.5

    states = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("media_player.")
    ]
    assert states, "no media_player entity was created"


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
    assert state.attributes["volume_level"] == pytest.approx(0.594, abs=0.01)
    assert state.attributes["is_volume_muted"] is False
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

    assert "POWER off" in device.received
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_OFF

    await hass.services.async_call(
        "media_player",
        "turn_on",
        {"entity_id": "media_player.living_room_amplifier"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert "POWER on" in device.received
    assert hass.states.get("media_player.living_room_amplifier").state == STATE_ON


async def test_set_volume_level_converts_to_db(hass: HomeAssistant, device: FakeRotel) -> None:
    """A 0..1 volume is translated into the dB range of the profile."""
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
    assert f"VOLUME {volume_to_payload(expected, model)}" in device.received


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

    assert "MUTE on" in device.received
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

    assert "SOURCE PHONO" in device.received
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
    assert "VOLUME 60" in device.received


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
    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}


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
    assert result["type"] == "abort"
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
    assert result["type"] == "create_entry"
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


async def test_reauth_updates_a_broken_entry(hass: HomeAssistant, device: FakeRotel) -> None:
    """Reauthentication re-validates and restores the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=_entry_data(device), unique_id="127.0.0.1"
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reauth_flow(hass)
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: device.port, CONF_MODEL_PROFILE: "ra1572"},
    )
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


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
    assert result["type"] == "create_entry"
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
    matched on ``manufacturer``/``st``/``deviceType``.
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

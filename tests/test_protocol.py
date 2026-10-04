"""Tests for the dependency-free protocol helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "rotel_control"
    / "protocol.py"
)


def _load_protocol():
    """Load protocol.py standalone (no Home Assistant imports involved)."""
    name = "rotel_protocol_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protocol = _load_protocol()
RotelCommand = protocol.RotelCommand
RotelInput = protocol.RotelInput
RotelModel = protocol.RotelModel
RotelQuery = protocol.RotelQuery
VolumeScale = protocol.VolumeScale


@pytest.fixture(name="model")
def model_fixture() -> RotelModel:
    """Return the RA-1572 profile."""
    return protocol.get_model("ra1572")


# --- wire format --------------------------------------------------------


def test_terminators_match_the_rotel_documentation() -> None:
    """Commands end with "!", reply fields with "$", nothing uses CRLF."""
    assert protocol.COMMAND_TERMINATOR == "!"
    assert protocol.RESPONSE_TERMINATOR == "$"


def test_build_query() -> None:
    """A query is the key plus a question mark, without a terminator."""
    assert protocol.build_query(RotelQuery.POWER) == "power?"
    assert protocol.build_query("volume") == "volume?"


def test_build_command_without_value() -> None:
    """A plain command ends with the command terminator."""
    assert protocol.build_command(RotelCommand.POWER_ON) == "power_on!"
    assert protocol.build_command(RotelCommand.MUTE_OFF) == "mute_off!"


def test_build_command_with_value() -> None:
    """A value is appended with an underscore, e.g. vol_42!."""
    assert protocol.build_command(RotelCommand.VOLUME, 42) == "vol_42!"
    assert protocol.build_command(RotelCommand.VOLUME, -30.5) == "vol_-30.5!"


def test_source_command() -> None:
    """Selecting an input is the input key plus the terminator."""
    assert protocol.source_command("coax1") == "coax1!"


# --- reply parsing ------------------------------------------------------


def test_parse_message_reads_key_value_pairs() -> None:
    """A chunk becomes a key/value mapping."""
    assert protocol.parse_message("power=on") == {"power": "on"}
    assert protocol.parse_message("volume=42") == {"volume": "42"}
    assert protocol.parse_message("source=analog_cd") == {"source": "analog_cd"}


def test_parse_message_reads_several_fields_of_one_chunk() -> None:
    """Firmware may pack several fields into one terminated chunk."""
    assert protocol.parse_message("version=1.24\nmodel=RA-1572") == {
        "version": "1.24",
        "model": "RA-1572",
    }


def test_parse_message_ignores_negative_responses_and_noise() -> None:
    """An unknown command answers with "?", which carries no state."""
    assert protocol.parse_message("?") == {}
    assert protocol.parse_message("") == {}
    assert protocol.parse_message("  \nnoise\n") == {}


@pytest.mark.parametrize(
    ("buffer", "expected", "rest"),
    [
        ("power=on$", {"power": "on"}, ""),
        ("power=on$volume=4", {"power": "on"}, "volume=4"),
        ("volume=4", {}, "volume=4"),
        ("", {}, ""),
    ],
)
def test_parse_messages_frames_on_the_terminator(
    buffer: str, expected: dict[str, str], rest: str
) -> None:
    """Only complete fields are parsed, the remainder is kept."""
    assert protocol.parse_messages(buffer) == (expected, rest)


def test_parse_messages_handles_a_reply_split_in_segments() -> None:
    """A field arriving in two TCP segments is still read."""
    values, rest = protocol.parse_messages("power=o")
    assert values == {}
    values, rest = protocol.parse_messages(rest + "n$volume=10$")
    assert values == {"power": "on", "volume": "10"}
    assert rest == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("on", True),
        ("ON", True),
        ("0", False),
        ("off", False),
        ("standby", False),
        ("STANDBY", False),
    ],
)
def test_parse_on_off(raw: str, expected: bool) -> None:
    """All spellings Rotel uses for booleans are understood."""
    assert protocol.parse_on_off(raw) is expected


def test_parse_on_off_rejects_garbage() -> None:
    """An unexpected value is an error, not a silent False."""
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_on_off("maybe")


# --- volume -------------------------------------------------------------


def test_steps_scale_matches_the_front_panel_scale(model: RotelModel) -> None:
    """The front panel index is mapped onto the dB range of the profile."""
    assert model.volume_scale is VolumeScale.STEPS
    steps = model.volume_steps
    assert protocol.volume_payload_range(model) == (0, steps)
    assert protocol.volume_to_payload(model.volume_min_db, model) == 0
    assert protocol.volume_to_payload(model.volume_max_db, model) == steps


def test_profile_defaults_keep_the_released_volume_range() -> None:
    """Upgrading must not silently narrow the range of existing entries.

    The released version used -60..20 dB for every profile that does not
    declare its own bounds; a narrower default would remap the volume of
    already configured amplifiers without any migration.
    """
    for key, model in protocol.ROTEL_MODELS.items():
        if key in {"rcx1570", "rcx1500", "rbx1500"}:
            continue
        assert (model.volume_min_db, model.volume_max_db) == (-60.0, 20.0), key


def test_steps_scale_round_trip(model: RotelModel) -> None:
    """Every device step survives a round trip."""
    for payload in range(0, model.volume_steps + 1, 8):
        volume = protocol.volume_from_payload(payload, model)
        assert protocol.volume_to_payload(volume, model) == payload


def test_volume_out_of_range_is_clamped(model: RotelModel) -> None:
    """Volumes outside the range are clamped to the nearest limit."""
    assert protocol.volume_to_payload(-100, model) == 0
    assert protocol.volume_to_payload(100, model) == model.volume_steps


def test_db_scale_is_passed_through_verbatim() -> None:
    """Processors report decibel, so the payload is the volume."""
    model = protocol.get_model("rcx1500")
    assert model.volume_scale is VolumeScale.DB
    assert protocol.volume_to_payload(-30.5, model) == -30.5
    assert protocol.volume_from_payload(-30.5, model) == -30.5


def test_volume_percent_scale() -> None:
    """The percent scale encodes 0..100 and is lossy by design."""
    model = RotelModel(
        key="percent",
        name="Percent",
        rbc="PCT",
        inputs=(RotelInput("cd", "CD"),),
        volume_min_db=-60.0,
        volume_max_db=20.0,
        volume_scale=VolumeScale.PERCENT,
    )
    assert protocol.volume_payload_range(model) == (0, 100)
    assert protocol.volume_to_payload(0.0, model) == 75
    assert protocol.volume_to_payload(-60.0, model) == 0
    assert protocol.volume_to_payload(20.0, model) == 100
    assert protocol.volume_from_payload(75, model) == 0.0


def test_volume_sentinels_of_an_implemented_firmware(model: RotelModel) -> None:
    """``volume=max``/``min`` are the limits of the profile."""
    assert protocol.volume_from_payload("max", model) == model.volume_max_db
    assert protocol.volume_from_payload("min", model) == model.volume_min_db


def test_negative_payload_on_a_steps_profile_is_read_as_db(model: RotelModel) -> None:
    """A processor reporting decibel must not be clamped to the minimum."""
    assert protocol.volume_from_payload(-30.0, model) == -30.0
    assert protocol.volume_is_out_of_range(-30.0, model)


def test_volume_out_of_range_detection(model: RotelModel) -> None:
    """The API warns when a payload cannot belong to the profile."""
    assert not protocol.volume_is_out_of_range(42, model)
    assert protocol.volume_is_out_of_range(model.volume_steps + 1, model)
    assert not protocol.volume_is_out_of_range("max", model)


@pytest.mark.parametrize(
    ("payload", "expected"), [("42", 42.0), ("-30.5", -30.5), ("max", None)]
)
def test_parse_volume_number(payload: str, expected: float | None) -> None:
    """Only a real position is a number, sentinels are not."""
    assert protocol.parse_volume_number(payload) == expected


def test_snap_volume_avoids_float_dust(model: RotelModel) -> None:
    """Snapped volumes are rounded to two decimals."""
    assert protocol.snap_volume(model.volume_min_db - 0.0000001, model) == (
        model.volume_min_db
    )
    assert protocol.snap_volume(model.volume_max_db - 0.3, model) == (
        model.volume_max_db - 0.5
    )


# --- inputs -------------------------------------------------------------


def test_parse_source_by_value_and_label(model: RotelModel) -> None:
    """Inputs resolve by protocol value and by label."""
    assert protocol.parse_source("tuner", model).name == "Tuner"
    assert protocol.parse_source("Tuner", model).value == "tuner"
    assert protocol.parse_source("phono", model).name == "Phono"


def test_parse_source_understands_firmware_aliases(model: RotelModel) -> None:
    """Some firmware reports "analog_cd" instead of "cd"."""
    assert protocol.parse_source("analog_cd", model).name == "CD"


@pytest.mark.parametrize("key", ["ra1572", "ra1200", "rcx1570", "rcx1500", "rbx1500"])
def test_previously_exposed_input_names_still_resolve(key: str) -> None:
    """Automations written for the released version keep working.

    The line inputs were renamed from "Aux" to "Line" and the RA-1200 lost its
    "Coax"/"Optical"/"Balanced" names, but the old labels must keep resolving.
    """
    model = protocol.get_model(key)
    assert protocol.parse_source("Aux 1", model).value == "aux1"
    assert protocol.parse_source("Aux 2", model).value == "aux2"
    if key == "ra1200":
        assert protocol.parse_source("Coax 1", model).value == "coax1"
        assert protocol.parse_source("Coax 2", model).value == "coax2"
        assert protocol.parse_source("Optical 1", model).value == "coax1"
        assert protocol.parse_source("Optical 2", model).value == "coax2"
        assert protocol.parse_source("Balanced", model).value == "bal_xlr1"


def test_rbx1500_keeps_four_balanced_inputs() -> None:
    """The RBX-1500 profile exposes Balanced 1 to 4."""
    names = [item.name for item in protocol.get_model("rbx1500").inputs]
    assert [name for name in names if name.startswith("Balanced")] == [
        "Balanced 1",
        "Balanced 2",
        "Balanced 3",
        "Balanced 4",
    ]


def test_parse_source_unknown_raises(model: RotelModel) -> None:
    """An unknown input is reported with the list of known ones."""
    with pytest.raises(protocol.ProtocolError, match="Known"):
        protocol.parse_source("FLAC", model)


def test_record_inputs_only_for_processors() -> None:
    """Only RCX models expose a record source."""
    assert not protocol.get_model("ra1572").record_inputs
    assert protocol.get_model("rcx1500").record_inputs
    assert (
        protocol.parse_source(
            "cd", protocol.get_model("rcx1500"), record=True
        ).name
        == "CD"
    )


# --- model profiles -----------------------------------------------------


def test_all_profiles_are_valid() -> None:
    """Every profile has sane bounds, inputs and a unique key."""
    for key, model in protocol.ROTEL_MODELS.items():
        assert model.key == key
        assert model.volume_max_db > model.volume_min_db
        assert model.volume_step_db > 0
        assert model.inputs, f"{key} has no inputs"
        values = [item.value for item in model.inputs]
        assert len(values) == len(set(values)), f"{key} has duplicate input values"
        for item in model.inputs:
            assert protocol.match_input(model.inputs, item.name) is item
            assert protocol.match_input(model.inputs, item.value) is item


def test_get_model_falls_back_to_generic() -> None:
    """An unknown key returns the generic profile."""
    assert protocol.get_model("does-not-exist").key == "generic"
    assert protocol.get_model(None).key == "generic"


def test_detect_model_normalises_the_reported_name() -> None:
    """Model codes are matched ignoring case, dashes and spaces."""
    assert protocol.detect_model("RA-1572 MKII").key == "ra1572mkii"
    assert protocol.detect_model("rbx1500").key == "rbx1500"
    assert protocol.detect_model("") is None
    assert protocol.detect_model("UNKNOWN") is None


def test_detected_profile_wins_over_generic() -> None:
    """Detection finds the profile that carries the real input list."""
    assert protocol.detect_model("RCX1570").record_inputs
    detected = protocol.detect_model("RA1572")
    assert detected is protocol.get_model("ra1572")
    assert detected.inputs == protocol.get_model("ra1572").inputs
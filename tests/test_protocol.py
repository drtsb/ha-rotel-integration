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
VolumeScale = protocol.VolumeScale


@pytest.fixture(name="model")
def model_fixture() -> RotelModel:
    """Return the RA-1572 profile."""
    return protocol.get_model("ra1572")


# --- command building ---------------------------------------------------


def test_build_command_without_value() -> None:
    """A query command is just its name."""
    assert protocol.build_command(RotelCommand.VOLUME_QUERY) == "VOLUME_QUERY"


def test_build_command_with_bool() -> None:
    """Booleans are encoded as on/off."""
    assert protocol.build_command(RotelCommand.POWER, True) == "POWER on"
    assert protocol.build_command(RotelCommand.MUTE, False) == "MUTE off"


def test_build_command_with_value() -> None:
    """Values are appended verbatim."""
    assert protocol.build_command(RotelCommand.SOURCE, "TUNER") == "SOURCE TUNER"
    assert protocol.build_command(RotelCommand.VOLUME, 95) == "VOLUME 95"


# --- volume -------------------------------------------------------------


@pytest.mark.parametrize(
    ("db", "expected"),
    [(-60.0, 0), (-12.5, 95), (-0.5, 119), (0.0, 120), (20.0, 160)],
)
def test_volume_to_payload_steps(model: RotelModel, db: float, expected: int) -> None:
    """Volumes map to device steps."""
    assert protocol.volume_to_payload(db, model) == expected


@pytest.mark.parametrize("db", [-60.0, -12.5, -0.5, 0.0, 20.0])
def test_volume_round_trip_is_lossless(model: RotelModel, db: float) -> None:
    """Every device step survives a round trip."""
    payload = protocol.volume_to_payload(db, model)
    assert protocol.volume_from_payload(payload, model) == db


def test_volume_out_of_range_is_clamped(model: RotelModel) -> None:
    """Volumes outside the range are clamped to the nearest limit."""
    assert protocol.volume_to_payload(-100, model) == 0
    assert protocol.volume_to_payload(100, model) == 160


def test_volume_percent_scale() -> None:
    """The percent scale encodes 0..100 and is lossy by design."""
    model = RotelModel(
        key="percent",
        name="Percent",
        rbc="PCT",
        inputs=(RotelInput("CD", "CD"),),
        volume_min_db=-60.0,
        volume_max_db=20.0,
        volume_scale=VolumeScale.PERCENT,
    )
    assert protocol.volume_payload_range(model) == (0, 100)
    assert protocol.volume_to_payload(0.0, model) == 75
    assert protocol.volume_to_payload(-60.0, model) == 0
    assert protocol.volume_to_payload(20.0, model) == 100
    assert protocol.volume_from_payload(75, model) == 0.0


def test_snap_volume_avoids_float_dust(model: RotelModel) -> None:
    """Snapped volumes are rounded to two decimals."""
    assert protocol.snap_volume(-60.0000000001, model) == -60.0
    assert protocol.snap_volume(19.7, model) == 19.5


# --- reply parsing ------------------------------------------------------


def test_parse_command_splits_command_and_payload() -> None:
    """A reply is split into command and payload."""
    assert protocol.parse_command("VOLUME 95\r\n") == ("VOLUME", "95")


def test_parse_command_ignores_acks_and_empty_lines() -> None:
    """Acknowledgements carry no state and are skipped."""
    assert protocol.parse_command("OK") is None
    assert protocol.parse_command("  ") is None
    assert protocol.parse_command("?") is None


def test_parse_command_strips_quotes() -> None:
    """Some firmware revisions wrap replies in quotes."""
    assert protocol.parse_command('"POWER 1"') == ("POWER", "1")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("on", True),
        ("ON", True),
        ("standby_off", True),
        ("0", False),
        ("off", False),
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


def test_strip_echo_removes_own_command() -> None:
    """The echo of a command is removed."""
    assert protocol.strip_echo("VOLUME 95", "VOLUME") == "95"
    assert protocol.strip_echo("VOLUME_QUERY", "VOLUME_QUERY") == ""


# --- inputs -------------------------------------------------------------


def test_parse_source_by_value_and_label(model: RotelModel) -> None:
    """Inputs resolve by protocol value and by label."""
    assert protocol.parse_source("TUNER", model).name == "Tuner"
    assert protocol.parse_source("Tuner", model).value == "TUNER"
    assert protocol.parse_source("Phono", model).name == "Phono"


def test_parse_source_unknown_raises(model: RotelModel) -> None:
    """An unknown input is reported."""
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_source("FLAC", model)


def test_source_payload_accepts_label_and_value(model: RotelModel) -> None:
    """Both spellings can be sent to the device."""
    assert protocol.source_payload("Tuner", model) == "TUNER"
    assert protocol.source_payload("CD", model) == "CD"


def test_record_inputs_only_for_processors() -> None:
    """Only RCX models expose a record source."""
    assert not protocol.get_model("ra1572").record_inputs
    assert protocol.get_model("rcx1500").record_inputs
    assert (
        protocol.parse_source(
            "CD", protocol.get_model("rcx1500"), record=True
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
        assert model.input_by_name(model.inputs[0].name) == model.inputs[0]


def test_get_model_falls_back_to_generic() -> None:
    """An unknown key returns the generic profile."""
    assert protocol.get_model("does-not-exist").key == "generic"
    assert protocol.get_model(None).key == "generic"


def test_detect_model_normalises_the_reported_name() -> None:
    """RBC codes are matched ignoring case, dashes and spaces."""
    assert protocol.detect_model("RA-1572 MKII").key == "ra1572mkii"
    assert protocol.detect_model("rbx1500").key == "rbx1500"
    assert protocol.detect_model("") is None
    assert protocol.detect_model("UNKNOWN") is None


def test_detected_profile_wins_over_generic() -> None:
    """Detection finds the profile that carries the real input list."""
    assert protocol.detect_model("RCX1570").record_inputs

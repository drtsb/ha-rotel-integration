"""Tests for the dependency-free protocol helpers."""

from __future__ import annotations

import importlib

import pytest

# Registering the package is a side effect of this import; the protocol is then
# loaded as part of it, so its relative import of ``const`` resolves. Neither
# module imports Home Assistant, which is what this suite guards.
from conftest import PACKAGE_NAME

protocol = importlib.import_module(f"{PACKAGE_NAME}.protocol")
protocol_const = importlib.import_module(f"{PACKAGE_NAME}.const")
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


def test_steps_scale_is_the_front_panel_scale(model: RotelModel) -> None:
    """The payload of this scale is the raw 0..96 of the front panel."""
    assert model.volume_scale is VolumeScale.STEPS
    assert model.volume_steps == 96
    assert protocol.VOLUME_UNITS == 96
    assert protocol.volume_payload_range(model) == (0, 96)
    assert protocol.volume_to_payload(model.volume_min_db, model) == 0
    assert protocol.volume_to_payload(model.volume_max_db, model) == 96


def test_steps_scale_sends_no_position_the_device_cannot_hold(
    model: RotelModel,
) -> None:
    """A volume anywhere in the dB span encodes to a payload of 0..96."""
    for volume in (
        model.volume_min_db,
        -45.0,
        -36.0,
        (model.volume_min_db + model.volume_max_db) / 2,
        model.volume_max_db,
    ):
        assert 0 <= protocol.volume_to_payload(volume, model) <= 96, volume


def test_profile_defaults_span_the_front_panel_scale() -> None:
    """A profile without own bounds covers exactly the 96 positions.

    The default bounds are the decibel anchor of the raw scale, so every
    profile that reports a raw volume has to end up with the width of that
    scale — otherwise the integration would send positions the device
    ignores.
    """
    for key, model in protocol.ROTEL_MODELS.items():
        if model.volume_scale is not VolumeScale.STEPS:
            continue
        assert (model.volume_min_db, model.volume_max_db) == (-60.0, -12.0), key
        assert model.volume_steps == 96, key


def test_db_scale_profiles_declare_their_own_bounds() -> None:
    """A profile that reports decibels is not touched by the raw default."""
    for key in ("rcx1570", "rcx1500", "rbx1500", "rca10"):
        model = protocol.get_model(key)
        assert model.volume_scale is VolumeScale.DB, key
        assert model.volume_max_db == 20.0, key


def test_a_span_that_is_not_the_front_panel_scale_is_refused() -> None:
    """The mismatch that used to send vol_160! cannot come back silently."""
    with pytest.raises(ValueError, match="96 positions"):
        RotelModel(
            key="wrong",
            name="Wrong",
            rbc="WRONG",
            inputs=(RotelInput("cd", "CD"),),
            volume_min_db=-60.0,
            volume_max_db=20.0,
        )


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

    The integrated amplifiers expose a single balanced input and call their
    coax pairs "Optical Coax", but the old labels and the values a firmware
    reports must keep resolving.
    """
    model = protocol.get_model(key)
    assert protocol.parse_source("Aux 1", model).value == "aux1"
    assert protocol.parse_source("Aux 2", model).value == "aux2"
    assert protocol.parse_source("Balanced", model).value.startswith("bal_xlr")
    if key == "ra1200":
        assert protocol.parse_source("Coax 1", model).value == "coax1"
        assert protocol.parse_source("Coax 2", model).value == "coax2"
        assert protocol.parse_source("Optical 1", model).value == "coax1"
        assert protocol.parse_source("Optical 2", model).value == "coax2"
        # One balanced input, reported as bal_xlr by the hardware.
        assert protocol.parse_source("bal_xlr", model).value == "bal_xlr"
        assert protocol.parse_source("bal_xlr1", model).value == "bal_xlr"


def test_integrated_amplifiers_have_exactly_one_balanced_input() -> None:
    """The BAL input of a Rotel amplifier is a single XLR, never a pair."""
    for key in ("ra1572", "ra1572mkii", "ra1200", "rca10", "generic"):
        values = [
            item.value
            for item in protocol.get_model(key).inputs
            if item.value.startswith("bal")
        ]
        assert values == ["bal_xlr"], key
        # And the value a firmware reports for the hardware resolves to it.
        assert protocol.parse_source("bal_xlr", protocol.get_model(key)).value == (
            "bal_xlr"
        )


def test_generic_profile_is_the_documented_input_set() -> None:
    """The fallback profile offers the inputs of the current Rotel units."""
    names = [item.name for item in protocol.get_model("generic").inputs]
    assert names == [
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


def test_catalogue_has_no_duplicate_names_or_values() -> None:
    """Every input of the catalogue is selectable and resolvable once."""
    const = protocol_const
    values = [item.value for item in const.ROTEL_INPUTS]
    names = [item.name for item in const.ROTEL_INPUTS]
    assert len(values) == len(set(values))
    assert len(names) == len(set(names))
    for item in const.ROTEL_INPUTS:
        assert protocol.match_input(const.ROTEL_INPUTS, item.value) is item
        assert protocol.match_input(const.ROTEL_INPUTS, item.name) is item
    # The catalogue is what the input selector offers.
    assert set(const.INPUT_LABELS) == set(values)


@pytest.mark.parametrize(
    ("model_key", "selected", "expected"),
    [
        # The profile default is kept when nothing is selected.
        ("generic", [], None),
        # Labels are accepted next to the protocol values.
        ("generic", ["CD", "coax1", "Balanced"], ["cd", "coax1", "bal_xlr"]),
        # Values the profile does not list are added from the catalogue.
        ("ra1572", ["cd", "usb", "bal_xlr"], ["cd", "bal_xlr", "usb"]),
        # Profile order wins over the order of the selection.
        ("generic", ["usb", "tuner"], ["tuner", "usb"]),
        # A selection that resolves to nothing leaves the profile alone.
        ("generic", ["nonsense"], None),
    ],
)
def test_get_model_inputs_limits_the_profile(
    model_key: str, selected: list[str], expected: list[str] | None
) -> None:
    """The options flow can trim the inputs down to what a unit has."""
    model = protocol.get_model(model_key)
    inputs = protocol.get_model_inputs(model, selected)
    if expected is None:
        assert inputs == model.inputs
    else:
        assert [item.value for item in inputs] == expected


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

# --- tone controls ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "000"), (5, "+05"), (-5, "-05"), (10, "+10"), (-10, "-10")],
)
def test_tone_values_use_the_rotel_token(value: int, expected: str) -> None:
    """Bass and treble are signed, zero padded and three digits wide."""
    assert protocol.format_tone(value) == expected
    assert protocol.tone_command(RotelCommand.BASS, value) == f"bass_{expected}!"
    assert protocol.tone_command(RotelCommand.TREBLE, value) == f"treble_{expected}!"


@pytest.mark.parametrize("payload", ["000", "+00", "0", "+07", "-07", 7, -7])
def test_tone_replies_decode(payload: str | int) -> None:
    """Every spelling a firmware uses for a tone level is understood."""
    assert protocol.parse_tone(str(payload)) == int(payload)


def test_tone_values_are_clamped_to_the_device_range() -> None:
    """A value outside the tone block is pulled back into it."""
    assert protocol.clamp_tone(50) == protocol.TONE_MAX_DB
    assert protocol.clamp_tone(-50) == protocol.TONE_MIN_DB
    assert protocol.format_tone(50) == "+10"


@pytest.mark.parametrize("payload", ["", "loud", "++5"])
def test_unexpected_tone_reply_is_rejected(payload: str) -> None:
    """A reply that is not a number is an error, not a silent zero."""
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_tone(payload)


def test_the_two_spellings_of_the_bypass_have_opposite_senses() -> None:
    """``bypass=on`` and ``tone=on`` are the opposite state of the switch.

    Rotel's command lists pair ``tone_on!`` with ``bypass_off!``, so the older
    firmware answers with the sense of the tone controls and not of the
    bypass. Getting this backwards leaves the tone block switched out while the
    interface claims it is in use.
    """
    assert protocol.tone_bypass_is_bypassed("bypass", "on") is True
    assert protocol.tone_bypass_is_bypassed("bypass", "off") is False
    assert protocol.tone_bypass_is_bypassed("tone", "on") is False
    assert protocol.tone_bypass_is_bypassed("tone", "off") is True


def test_the_bypass_command_follows_the_firmware() -> None:
    """Each spelling gets the command that means what the switch shows."""
    assert protocol.tone_bypass_command("bypass", True) == "bypass_on!"
    assert protocol.tone_bypass_command("bypass", False) == "bypass_off!"
    assert protocol.tone_bypass_command("tone", True) == "tone_off!"
    assert protocol.tone_bypass_command("tone", False) == "tone_on!"


def test_the_bypass_echo_uses_the_token_the_device_answers() -> None:
    """The answer to look for is the one this spelling sends."""
    for key in ("bypass", "tone"):
        for bypassed in (True, False):
            state = protocol.tone_bypass_state(key, bypassed)
            assert protocol.tone_bypass_is_bypassed(key, state) is bypassed


def test_an_unexpected_bypass_reply_is_rejected() -> None:
    """A bypass answer that is not a state is an error, not a guess."""
    with pytest.raises(protocol.ProtocolError):
        protocol.tone_bypass_is_bypassed("bypass", "maybe")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "000"), (-2, "l02"), (2, "r02"), (-15, "l15"), (15, "r15")],
)
def test_balance_uses_the_rotel_token(value: int, expected: str) -> None:
    """Left is negative, right is positive, the centre is ``000``."""
    assert protocol.format_balance(value) == expected
    assert protocol.balance_command(value) == f"balance_{expected}!"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("000", 0),
        ('"000"', 0),
        ("L03", -3),
        ("l15", -15),
        ("R07", 7),
        ("C", 0),
    ],
)
def test_balance_replies_decode(payload: str, expected: int) -> None:
    """Both firmware spellings of the sides are understood."""
    assert protocol.parse_balance(payload) == expected


@pytest.mark.parametrize("payload", ["L00", "R16", "sideways", ""])
def test_unexpected_balance_reply_is_rejected(payload: str) -> None:
    """An out of range or unreadable balance is an error."""
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_balance(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("off", (False, False)),
        ("a", (True, False)),
        ("b", (False, True)),
        ("a_b", (True, True)),
        ("A+B", (True, True)),
    ],
)
def test_speaker_replies_decode(payload: str, expected: tuple[bool, bool]) -> None:
    """The four documented speaker states are understood."""
    assert protocol.parse_speakers(payload) == expected


def test_unexpected_speaker_reply_is_rejected() -> None:
    """An unknown speaker state lists the known ones."""
    with pytest.raises(protocol.ProtocolError, match="a_b"):
        protocol.parse_speakers("c")


@pytest.mark.parametrize(("payload", "expected"), [("0", 0), ("6", 6), ("42", 6)])
def test_dimmer_replies_are_clamped(payload: str, expected: int) -> None:
    """Brightness is a level between DIMMER_MIN and DIMMER_MAX."""
    assert protocol.parse_dimmer(payload) == expected
    assert protocol.clamp_dimmer(9) == protocol.DIMMER_MAX


def test_dimmer_commands_cover_every_level() -> None:
    """Each level of the select maps to one documented command."""
    assert protocol.dimmer_command(0) == "dimmer_0!"
    assert protocol.dimmer_command(3) == "dimmer_3!"
    assert protocol.dimmer_command(6) == "dimmer_6!"
    assert len(protocol_const.DIMMER_OPTIONS) == 7


def test_model_queries_follow_the_declared_features() -> None:
    """A profile without a tone block is never asked for one."""
    model = protocol.get_model("ra1572")
    assert protocol.RotelQuery.BASS in model.queries
    assert protocol.RotelQuery.SPEAKER in model.queries
    assert protocol.RotelQuery.RECORD_SOURCE not in model.queries

    plain = protocol.RotelModel(
        key="plain",
        name="Plain",
        rbc="",
        inputs=(RotelInput("cd", "CD"),),
        tone_control=False,
        speaker_groups=(),
        dimmer=False,
    )
    assert plain.queries == ()


def test_selectable_inputs_cover_every_profile() -> None:
    """The input selector never rejects a selection another profile uses."""
    selectable = protocol.selectable_inputs()
    allowed = {item.value for item in selectable}
    # The catalogue comes first, so the common inputs are at the top.
    assert selectable[: len(protocol_const.ROTEL_INPUTS)] == protocol_const.ROTEL_INPUTS
    for model in protocol.ROTEL_MODELS.values():
        assert {item.value for item in model.inputs} <= allowed
    assert len(allowed) == len(selectable)

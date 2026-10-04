"""Pure protocol helpers for Rotel control interfaces.

This module deliberately has **no Home Assistant imports** so that the wire
format can be unit-tested in isolation and reused by the RS-232 variant of
the protocol. It imports :mod:`.const`, which is equally dependency free.

Rotel amplifiers with a network interface expose the very same ASCII command
set that is documented for the RS-232 port, on TCP port ``9590``::

    >>> power?
    <<< power=on$
    >>> volume?
    <<< volume=42$
    >>> power_on!
    >>> vol_42!
    >>> tuner!
    >>> bypass_on!
    >>> bass_-04!
    >>> balance_l02!
    >>> speaker_a_on!
    >>> dimmer_3!

Every command is terminated with ``!`` and never carries CR/LF, every reply
field is terminated with ``$``. A reply may carry several fields at once,
separated by newlines, and the device may answer unsolicited fields when
push updates are enabled::

    <<< version=1.24$
    <<< model=RA-1572$\\nvolume=42$

Models differ in a handful of ways only, and all of them are described by
:class:`RotelModel` so a new device usually needs nothing but a new profile:

* the command key of every input (and the values the firmware may report for
  it),
* how a volume is encoded (raw steps of 0..96 or plain decibel),
* the volume range and granularity used in Home Assistant,
* which of the tone controls exist at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .const import (
    BALANCE_MAX,
    BALANCE_MIN,
    BALANCE_STEP,
    DIMMER_MAX,
    DIMMER_MIN,
    ROTEL_INPUTS,
    SOURCE_BALANCED,
    SOURCE_BLUETOOTH,
    SOURCE_CD,
    SOURCE_COAX1,
    SOURCE_COAX2,
    SOURCE_OPTICAL1,
    SOURCE_OPTICAL2,
    SOURCE_PCUSB,
    SOURCE_PHONO,
    SOURCE_TUNER,
    SPEAKER_GROUPS,
    SPEAKER_STATES,
    TONE_MAX_DB,
    TONE_MIN_DB,
    TONE_STEP_DB,
    RotelInput,
)

#: Terminator of every command sent to the device.
COMMAND_TERMINATOR = "!"

#: Terminator of every field the device sends.
RESPONSE_TERMINATOR = "$"

#: Replies a Rotel sends when it does not understand (or refuses) a command.
NEGATIVE_RESPONSES = frozenset({"?", "no_reply", "error", "nack"})


class RotelQuery(StrEnum):
    """Keys that can be asked for with ``<key>?``."""

    POWER = "power"
    VOLUME = "volume"
    MUTE = "mute"
    SOURCE = "source"
    RECORD_SOURCE = "record_source"
    MODEL = "model"
    FIRMWARE = "version"
    #: Tone bypass on current firmware.
    BYPASS = "bypass"
    #: Tone bypass on firmware that predates the ``bypass`` naming.
    TONE = "tone"
    BASS = "bass"
    TREBLE = "treble"
    BALANCE = "balance"
    SPEAKER = "speaker"
    DIMMER = "dimmer"


#: Both spellings of the tone bypass switch are asked for: a unit answers the
#: one its firmware knows and ignores the other. Whichever answers first
#: decides the commands used from then on.
TONE_BYPASS_QUERIES: tuple[RotelQuery, ...] = (RotelQuery.BYPASS, RotelQuery.TONE)


class RotelCommand(StrEnum):
    """Commands that change something, sent as ``<command>!``."""

    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    MUTE_ON = "mute_on"
    MUTE_OFF = "mute_off"
    #: ``vol_<NN>!``, the value is appended with an underscore.
    VOLUME = "vol"
    #: ``bass_<000/+01/-10>!`` and the same for ``treble_``.
    BASS = "bass"
    TREBLE = "treble"
    #: ``balance_<000/l15/r15>!``.
    BALANCE = "balance"
    #: ``speaker_a_on!`` and friends.
    SPEAKER_A_ON = "speaker_a_on"
    SPEAKER_A_OFF = "speaker_a_off"
    SPEAKER_B_ON = "speaker_b_on"
    SPEAKER_B_OFF = "speaker_b_off"
    #: ``dimmer_<0-6>!``.
    DIMMER = "dimmer"


class VolumeScale(StrEnum):
    """How a volume is turned into the integer sent to the device.

    ``STEPS`` is used by the two channel amplifiers (A12/A14/RA-1572 family):
    they report a raw position of the front panel volume scale instead of a
    decibel value. ``DB`` is used by the processors, which report decibels
    directly. ``PERCENT`` covers firmware that reports 0..100.
    """

    #: Payload is the offset from ``volume_min_db`` in ``volume_step_db`` units.
    STEPS = "steps"
    #: Payload is the volume in decibel.
    DB = "db"
    #: Payload is 0..100 percent of the volume range.
    PERCENT = "percent"


class ProtocolError(Exception):
    """Raised when a reply cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class RotelModel:
    """Static description of an amplifier model."""

    key: str
    name: str
    #: Model code reported by ``model?``, e.g. ``RA1572``.
    rbc: str
    inputs: tuple[RotelInput, ...]
    record_inputs: tuple[RotelInput, ...] = ()
    volume_min_db: float = -60.0
    #: Kept at the value the released version used, so upgrading does not
    #: silently narrow the range of already configured entries. Profiles whose
    #: hardware differs declare their own bounds.
    volume_max_db: float = 20.0
    volume_step_db: float = 0.5
    volume_scale: VolumeScale = VolumeScale.STEPS
    #: True for processors with a second zone.
    zone2: bool = False
    #: False for units without a tone block (every network unit has one, but a
    #: stripped down profile can say so).
    tone_control: bool = True
    #: False to hide the tone bypass switch while keeping bass and treble.
    tone_bypass: bool = True
    #: Speaker groups that exist; an empty tuple disables the speaker entities.
    speaker_groups: tuple[str, ...] = SPEAKER_GROUPS
    #: False for units with a fixed front display brightness.
    dimmer: bool = True

    def __post_init__(self) -> None:
        if self.volume_max_db <= self.volume_min_db:
            raise ValueError("volume_max_db must be greater than volume_min_db")
        if self.volume_step_db <= 0:
            raise ValueError("volume_step_db must be positive")

    @property
    def volume_range_db(self) -> float:
        """Total span of the volume range in dB."""
        return self.volume_max_db - self.volume_min_db

    @property
    def volume_steps(self) -> int:
        """Amount of discrete volume positions of this model."""
        return round(self.volume_range_db / self.volume_step_db)

    @property
    def queries(self) -> tuple[RotelQuery, ...]:
        """Optional queries this model answers.

        Derived from the features instead of being declared per profile, so a
        profile cannot ask for a control it does not have, and a new control
        only has to be added here.
        """
        keys: list[RotelQuery] = []
        if self.record_inputs:
            keys.append(RotelQuery.RECORD_SOURCE)
        if self.tone_control:
            keys.extend((*TONE_BYPASS_QUERIES, RotelQuery.BASS, RotelQuery.TREBLE))
            keys.append(RotelQuery.BALANCE)
        if self.speaker_groups:
            keys.append(RotelQuery.SPEAKER)
        if self.dimmer:
            keys.append(RotelQuery.DIMMER)
        return tuple(keys)


def match_input(inputs: Sequence[RotelInput], value: str) -> RotelInput | None:
    """Return the input of ``inputs`` matching a label or reported value."""
    needle = value.strip().strip('"').casefold()
    if not needle:
        return None
    for item in inputs:
        if needle in {known.casefold() for known in item.values}:
            return item
    for item in inputs:
        if needle == item.name.casefold():
            return item
    return None


# --- input helpers ------------------------------------------------------
# The catalogue of every known input lives in const.py. Only the variants a
# specific unit deviates with are built here.


def _line(index: int) -> RotelInput:
    """Numbered RCA line input, called "Line" on the current units."""
    return RotelInput(
        f"aux{index}",
        f"Line {index}",
        "mdi:audio-input",
        (f"aux{index}", f"aux {index}", f"line{index}", f"line {index}"),
    )


def _balanced(index: int) -> RotelInput:
    """Balanced (XLR) input ``index`` of a unit with more than one of them.

    ``index`` 1 of a processor also answers to the bare "Balanced" name, which
    is what the single XLR of the integrated amplifiers reports.
    """
    known: tuple[str, ...] = (
        f"balanced {index}",
        f"bal_xlr{index}",
        f"balanced{index}",
    )
    if index == 1:
        known += SOURCE_BALANCED.aliases
    return RotelInput(
        f"bal_xlr{index}",
        f"Balanced {index}",
        "mdi:surround-sound",
        known,
    )


def _optical_coax(index: int) -> RotelInput:
    """Optical coax input of the RA-1572 family (a coax RCA pair)."""
    return RotelInput(
        f"coax{index}",
        f"Optical Coax {index}",
        "mdi:surround-sound",
        (f"coax{index}", f"coax {index}", f"optical {index}", f"opt{index}"),
    )


def _hdmi(index: int) -> RotelInput:
    """HDMI input ``index``."""
    return RotelInput(f"hdmi{index}", f"HDMI {index}", "mdi:hdmi")


#: Inputs shared by the RCX/RBX processors.
_PROCESSOR_INPUTS = (
    _hdmi(1),
    _hdmi(2),
    _hdmi(3),
    _hdmi(4),
    SOURCE_COAX1,
    SOURCE_COAX2,
    SOURCE_OPTICAL1,
    SOURCE_OPTICAL2,
    _balanced(1),
    _balanced(2),
    _line(1),
    _line(2),
)

#: The RBX-1500 has two more balanced inputs than the RCX processors.
_RBX_INPUTS = (
    *_PROCESSOR_INPUTS[:8],
    _balanced(1),
    _balanced(2),
    _balanced(3),
    _balanced(4),
    _line(1),
    _line(2),
)

#: Inputs of the two channel amplifiers with a network interface. They expose
#: one balanced (XLR) input, whose coax pairs are called "Optical Coax", and
#: two numbered line inputs.
_INTEGRATED_INPUTS = (
    SOURCE_CD,
    SOURCE_TUNER,
    SOURCE_BALANCED,
    _optical_coax(1),
    _optical_coax(2),
    _line(1),
    _line(2),
    SOURCE_PHONO,
)


ROTEL_MODELS: Mapping[str, RotelModel] = {
    model.key: model
    for model in (
        RotelModel(
            key="ra1572",
            name="Rotel RA-1572",
            rbc="RA1572",
            inputs=_INTEGRATED_INPUTS,
        ),
        RotelModel(
            key="ra1572mkii",
            name="Rotel RA-1572 MkII",
            rbc="RA1572MKII",
            inputs=(*_INTEGRATED_INPUTS, SOURCE_BLUETOOTH, SOURCE_PCUSB),
        ),
        RotelModel(
            key="ra1200",
            name="Rotel RA-1200 MkII",
            rbc="RA1200",
            inputs=(*_INTEGRATED_INPUTS, SOURCE_BLUETOOTH, SOURCE_PCUSB),
        ),
        RotelModel(
            key="rcx1570",
            name="Rotel RCX-1570 MkII",
            rbc="RCX1570",
            inputs=(
                SOURCE_CD,
                SOURCE_TUNER,
                SOURCE_COAX1,
                SOURCE_COAX2,
                SOURCE_OPTICAL1,
                SOURCE_OPTICAL2,
                _balanced(1),
                _balanced(2),
                _line(1),
                _line(2),
                SOURCE_PHONO,
                SOURCE_BLUETOOTH,
                SOURCE_PCUSB,
            ),
            record_inputs=(SOURCE_CD, SOURCE_TUNER, _line(1), _line(2)),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
        ),
        RotelModel(
            key="rcx1500",
            name="Rotel RCX-1500",
            rbc="RCX1500",
            inputs=(
                *_PROCESSOR_INPUTS,
                SOURCE_CD,
                SOURCE_TUNER,
                SOURCE_PHONO,
                SOURCE_BLUETOOTH,
            ),
            record_inputs=(SOURCE_CD, SOURCE_TUNER),
            volume_min_db=-80.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
            zone2=True,
        ),
        RotelModel(
            key="rbx1500",
            name="Rotel RBX-1500",
            rbc="RBX1500",
            inputs=(*_RBX_INPUTS, SOURCE_CD, SOURCE_TUNER, SOURCE_BLUETOOTH),
            volume_min_db=-80.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
        ),
        RotelModel(
            key="rca10",
            name="Rotel RCA-10",
            rbc="RCA10",
            inputs=(
                _hdmi(1),
                _hdmi(2),
                SOURCE_OPTICAL1,
                SOURCE_COAX1,
                SOURCE_BALANCED,
                _line(1),
                _line(2),
                SOURCE_PHONO,
                SOURCE_BLUETOOTH,
                SOURCE_PCUSB,
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
        ),
        # Generic fallback for unknown Rotel devices: the documented input set
        # of the current network units, raw 0..96 volume scale.
        RotelModel(
            key="generic",
            name="Rotel (generic)",
            rbc="",
            inputs=ROTEL_INPUTS,
        ),
    )
}

#: ``{profile_key: "Rotel RA-1572"}`` for the model selector of the flows.
MODEL_LABELS: Mapping[str, str] = {
    key: model.name for key, model in ROTEL_MODELS.items()
}


def get_model(key: str | None) -> RotelModel:
    """Return the profile for ``key`` falling back to the generic profile."""
    if key and key in ROTEL_MODELS:
        return ROTEL_MODELS[key]
    return ROTEL_MODELS["generic"]


def get_model_inputs(model: RotelModel, values: Sequence[str]) -> tuple[RotelInput, ...]:
    """Return the inputs of ``model`` limited to ``values``.

    Used by the options flow: a unit whose front panel differs from its model
    profile only needs the matching inputs selected once. Every entry may be
    given as a protocol value (``coax1``) or as a label (``Coax 1``), the
    profile keeps its own order and inputs it does not list but the catalogue
    knows are appended. A selection that resolves to nothing leaves the profile
    untouched, so a device is never left without any input at all.
    """
    wanted = [value for value in values if value and value.strip()]
    if not wanted:
        return model.inputs
    resolved: set[str] = set()
    for value in wanted:
        found = match_input(model.inputs, value) or match_input(ROTEL_INPUTS, value)
        if found is not None:
            resolved.add(found.value)
    if not resolved:
        return model.inputs
    selected = tuple(item for item in model.inputs if item.value in resolved)
    extra = tuple(
        item for item in ROTEL_INPUTS if item.value in resolved and item not in selected
    )
    return selected + extra


def selectable_inputs() -> tuple[RotelInput, ...]:
    """Return every input a user may pick, whatever the profile.

    The catalogue comes first, followed by the inputs only a specific profile
    knows (a numbered line input, an HDMI input, a third balanced input).
    Offering all of them means switching the model of a configured entry never
    rejects the input list that is already stored.
    """
    known = {item.value for item in ROTEL_INPUTS}
    extras: dict[str, RotelInput] = {}
    for model in ROTEL_MODELS.values():
        for item in model.inputs:
            if item.value not in known and item.value not in extras:
                extras[item.value] = item
    return (*ROTEL_INPUTS, *extras.values())


def detect_model(rbc: str | None) -> RotelModel | None:
    """Return the profile whose model code matches ``rbc``."""
    if not rbc:
        return None
    needle = rbc.strip().upper().replace("-", "").replace(" ", "")
    for model in ROTEL_MODELS.values():
        if model.rbc and model.rbc.upper() == needle:
            return model
    return None


# --- command building ---------------------------------------------------


def build_query(key: RotelQuery | str) -> str:
    """Build a query line, e.g. ``power?``."""
    return f"{key}?"


def build_command(command: RotelCommand | str, value: object | None = None) -> str:
    """Build a command line, e.g. ``power_on!`` or ``vol_42!``."""
    if value is None:
        return f"{command}{COMMAND_TERMINATOR}"
    return f"{command}_{value}{COMMAND_TERMINATOR}"


def source_command(value: str) -> str:
    """Build the line that selects an input, e.g. ``coax1!``."""
    return f"{value}{COMMAND_TERMINATOR}"


# --- reply parsing ------------------------------------------------------


def parse_message(chunk: str) -> dict[str, str]:
    """Parse one ``$`` terminated chunk into ``{key: value}``.

    A chunk may carry several newline separated fields. Replies without an
    ``=`` (the ``?`` a device sends for an unknown command) are ignored.
    """
    values: dict[str, str] = {}
    for line in chunk.replace(COMMAND_TERMINATOR, "").splitlines():
        line = line.strip().strip('"').strip()
        if not line or line.casefold() in NEGATIVE_RESPONSES or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().casefold()] = value.strip()
    return values


def parse_messages(buffer: str) -> tuple[dict[str, str], str]:
    """Split a raw buffer into ``({key: value}, unparsed remainder)``.

    Everything up to the last ``$`` is parsed, so a reply that arrives in
    several TCP segments is never truncated.
    """
    head, separator, tail = buffer.rpartition(RESPONSE_TERMINATOR)
    if not separator:
        return {}, buffer
    values: dict[str, str] = {}
    for chunk in head.split(RESPONSE_TERMINATOR):
        values.update(parse_message(chunk))
    return values, tail


def parse_on_off(value: str) -> bool:
    """Parse the many spellings Rotel uses for booleans."""
    token = value.strip().strip('"').casefold()
    if token in {"1", "on", "true", "yes"}:
        return True
    if token in {"0", "off", "false", "no", "standby"}:
        return False
    raise ProtocolError(f"Unexpected boolean value: {value!r}")


def parse_source(
    payload: str, model: RotelModel, *, record: bool = False
) -> RotelInput:
    """Resolve a reported input value to a :class:`RotelInput`."""
    inputs = model.record_inputs if record else model.inputs
    if (found := match_input(inputs, payload)) is not None:
        return found
    known = ", ".join(item.name for item in inputs)
    raise ProtocolError(f"Unknown input {payload!r} for {model.name}. Known: {known}")


# --- volume -------------------------------------------------------------


def clamp_volume(volume_db: float, model: RotelModel) -> float:
    """Clamp ``volume_db`` into the range supported by ``model``."""
    return min(max(volume_db, model.volume_min_db), model.volume_max_db)


def snap_volume(volume_db: float, model: RotelModel) -> float:
    """Clamp and round ``volume_db`` to a position the device can hold."""
    clamped = clamp_volume(volume_db, model)
    steps = round((clamped - model.volume_min_db) / model.volume_step_db)
    snapped = model.volume_min_db + steps * model.volume_step_db
    # Guard against floating point drift such as -60.00000000000001.
    return round(snapped, 2)


def volume_to_payload(volume_db: float, model: RotelModel) -> int | float:
    """Encode a volume in dB as the value expected by the device."""
    snapped = snap_volume(volume_db, model)
    if model.volume_scale is VolumeScale.STEPS:
        return round((snapped - model.volume_min_db) / model.volume_step_db)
    if model.volume_scale is VolumeScale.DB:
        return round(snapped, 2)
    percent = (snapped - model.volume_min_db) / model.volume_range_db * 100
    return round(percent)


def volume_payload_range(model: RotelModel) -> tuple[float, float]:
    """Return the inclusive range of volume payloads ``model`` understands."""
    if model.volume_scale is VolumeScale.STEPS:
        return 0, model.volume_steps
    if model.volume_scale is VolumeScale.DB:
        return model.volume_min_db, model.volume_max_db
    return 0, 100


def volume_from_payload(payload: float | str, model: RotelModel) -> float:
    """Decode the volume reported by the device back to dB."""
    if isinstance(payload, str):
        # Some firmware quotes the value: "42" and 42 must both parse.
        payload = payload.strip().strip('"').strip()
        if payload.casefold() == "max":
            return model.volume_max_db
        if payload.casefold() == "min":
            return model.volume_min_db
    value = float(payload)
    if model.volume_scale is VolumeScale.STEPS:
        if value < 0:
            # Firmware that reports decibels on a model configured for the
            # raw 0..96 scale of the front panel.
            return snap_volume(value, model)
        raw = model.volume_min_db + value * model.volume_step_db
    elif model.volume_scale is VolumeScale.DB:
        raw = value
    else:
        percent = min(max(value, 0.0), 100.0)
        raw = model.volume_min_db + (percent / 100.0) * model.volume_range_db
    return snap_volume(raw, model)


def parse_volume_number(payload: str) -> float | None:
    """Return the numeric volume of a reply.

    ``None`` for the ``max``/``min`` sentinels and for anything that is not a
    number, so callers can tell a real position from a firmware keyword.
    """
    try:
        return float(payload.strip().strip('"'))
    except ValueError:
        return None


def volume_is_out_of_range(payload: object, model: RotelModel) -> bool:
    """True when a reported volume cannot belong to ``model``."""
    if not isinstance(payload, (int, float)) or isinstance(payload, bool):
        # Sentinels such as "max"/"min" and anything unexpected.
        return False
    lowest, highest = volume_payload_range(model)
    return not lowest <= payload <= highest


# --- tone controls ------------------------------------------------------
# Bass, treble, balance and the display dimmer all use the same convention:
# a signed, zero padded, three digit token, where ``000`` is the neutral
# position (``000``/``+05``/``-10`` for the tone block, ``000``/``L15``/``R15``
# for the balance). The token is appended to the command with an underscore.


def format_tone(value: float) -> str:
    """Encode a tone block value, e.g. ``-4`` -> ``-04`` and ``0`` -> ``000``."""
    clamped = clamp_tone(value)
    if clamped == 0:
        return "000"
    return f"{clamped:+03d}"


def parse_tone(payload: str) -> int:
    """Decode a ``bass=``/``treble=`` reply into a number of dB."""
    token = _token(payload)
    try:
        value = int(token)
    except ValueError as err:
        raise ProtocolError(f"Unexpected tone value: {payload!r}") from err
    return clamp_tone(value)


def clamp_tone(value: float) -> int:
    """Clamp a tone block value to what the device can hold."""
    return min(max(round(value), TONE_MIN_DB), TONE_MAX_DB)


def tone_command(command: RotelCommand, value: float) -> str:
    """Build the command that sets bass or treble, e.g. ``bass_-04!``."""
    return build_command(command, format_tone(value))


def clamp_balance(value: float) -> int:
    """Clamp a balance value to the L01..L15/R01..R15 range of the device."""
    return min(max(round(value), BALANCE_MIN), BALANCE_MAX)


def format_balance(value: float) -> str:
    """Encode a balance, e.g. ``-2`` -> ``l02``, ``0`` -> ``000``, ``15`` -> ``r15``."""
    clamped = clamp_balance(value)
    if clamped == 0:
        return "000"
    if clamped < 0:
        return f"l{abs(clamped):02d}"
    return f"r{clamped:02d}"


def parse_balance(payload: str) -> int:
    """Decode a ``balance=`` reply, negative meaning "to the left"."""
    token = _token(payload).upper()
    if token in {"000", "0", "C", "CENTER", "CENTRE"}:
        return 0
    # The side is a prefix, and older firmware spells it in upper case.
    direction = token[0] if token[:1] in {"L", "R"} else ""
    if direction:
        token = token[1:]
    try:
        magnitude = int(token)
    except ValueError as err:
        raise ProtocolError(f"Unexpected balance value: {payload!r}") from err
    if not 1 <= magnitude <= abs(BALANCE_MAX):
        raise ProtocolError(f"Unexpected balance value: {payload!r}")
    return clamp_balance(-magnitude if direction == "L" else magnitude)


def balance_command(value: float) -> str:
    """Build the command that sets the balance, e.g. ``balance_l02!``."""
    return build_command(RotelCommand.BALANCE, format_balance(value))


def parse_speakers(payload: str) -> tuple[bool, bool]:
    """Decode a ``speaker=`` reply into ``(speaker_a, speaker_b)``."""
    token = _token(payload).casefold().replace("-", "_").replace(" ", "_")
    if token in {"off", "none", "mute", "0", ""}:
        return (False, False)
    if token in {"a", "speakera", "1"}:
        return (True, False)
    if token in {"b", "speakerb", "2"}:
        return (False, True)
    if token in {"a_b", "ab", "a+b", "both", "3"}:
        return (True, True)
    raise ProtocolError(
        f"Unexpected speaker value: {payload!r}. Known: {', '.join(SPEAKER_STATES)}"
    )


def clamp_dimmer(value: float) -> int:
    """Clamp a display brightness to the DIMMER_MIN..DIMMER_MAX of the device."""
    return min(max(round(value), DIMMER_MIN), DIMMER_MAX)


def parse_dimmer(payload: str) -> int:
    """Decode a ``dimmer=`` reply into a brightness level."""
    token = _token(payload)
    try:
        value = int(token)
    except ValueError as err:
        raise ProtocolError(f"Unexpected dimmer value: {payload!r}") from err
    return clamp_dimmer(value)


def dimmer_command(value: float) -> str:
    """Build the command that sets the display brightness, e.g. ``dimmer_3!``."""
    return build_command(RotelCommand.DIMMER, clamp_dimmer(value))


def _token(payload: str) -> str:
    """Strip the quoting a firmware may add around a value."""
    return payload.strip().strip('"').strip()


__all__ = (
    "BALANCE_MAX",
    "BALANCE_MIN",
    "BALANCE_STEP",
    "COMMAND_TERMINATOR",
    "DIMMER_MAX",
    "DIMMER_MIN",
    "MODEL_LABELS",
    "NEGATIVE_RESPONSES",
    "RESPONSE_TERMINATOR",
    "ROTEL_INPUTS",
    "ROTEL_MODELS",
    "TONE_BYPASS_QUERIES",
    "TONE_MAX_DB",
    "TONE_MIN_DB",
    "TONE_STEP_DB",
    "ProtocolError",
    "RotelCommand",
    "RotelInput",
    "RotelModel",
    "RotelQuery",
    "VolumeScale",
    "balance_command",
    "build_command",
    "build_query",
    "clamp_balance",
    "clamp_dimmer",
    "clamp_tone",
    "clamp_volume",
    "detect_model",
    "dimmer_command",
    "format_balance",
    "format_tone",
    "get_model",
    "get_model_inputs",
    "match_input",
    "parse_balance",
    "parse_dimmer",
    "parse_message",
    "parse_messages",
    "parse_on_off",
    "parse_source",
    "parse_speakers",
    "parse_tone",
    "parse_volume_number",
    "selectable_inputs",
    "snap_volume",
    "source_command",
    "tone_command",
    "volume_from_payload",
    "volume_is_out_of_range",
    "volume_payload_range",
    "volume_to_payload",
)
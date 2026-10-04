"""Pure protocol helpers for Rotel control interfaces.

This module deliberately has **no Home Assistant imports** so that the wire
format can be unit-tested in isolation and reused by the RS-232 variant of
the protocol.

Rotel amplifiers with a network interface expose the very same ASCII command
set that is documented for the RS-232 port, on TCP port ``9590``::

    >>> power?
    <<< power=on$
    >>> volume?
    <<< volume=42$
    >>> power_on!
    >>> vol_42!
    >>> tuner!

Every command is terminated with ``!`` and never carries CR/LF, every reply
field is terminated with ``$``. A reply may carry several fields at once,
separated by newlines, and the device may answer unsolicited fields when
push updates are enabled::

    <<< version=1.24$
    <<< model=RA-1572$\\nvolume=42$

Models differ in three ways only, and all three are described by
:class:`RotelModel` so a new device usually needs nothing but a new profile:

* the command key of every input (and the values the firmware may report for
  it),
* how a volume is encoded (raw steps of 0..96 or plain decibel),
* the volume range and granularity used in Home Assistant.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

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


class RotelCommand(StrEnum):
    """Commands that change something, sent as ``<command>!``."""

    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    MUTE_ON = "mute_on"
    MUTE_OFF = "mute_off"
    #: ``vol_<NN>!``, the value is appended with an underscore.
    VOLUME = "vol"


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
class RotelInput:
    """A single input (source) of an amplifier."""

    #: Command sent to the device, e.g. ``coax1`` (``coax1!``).
    value: str
    #: Label shown in the user interface.
    name: str
    icon: str = "mdi:audio-input"
    #: Values a firmware revision may report for this input instead of
    #: ``value``, e.g. ``analog_cd`` for ``cd``.
    aliases: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Human readable label shown in the UI."""
        return self.name

    @property
    def values(self) -> tuple[str, ...]:
        """Every spelling this input is known under."""
        return (self.value, *self.aliases)


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
    #: Extra queries asked once per connection, e.g. the record source.
    queries: tuple[RotelQuery, ...] = field(default_factory=tuple)

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


def _cd() -> RotelInput:
    """CD input, reported as ``analog_cd`` by some firmware revisions."""
    return RotelInput("cd", "CD", "mdi:compact-disc", ("analog_cd",))


def _tuner() -> RotelInput:
    """Tuner input."""
    return RotelInput("tuner", "Tuner", "mdi:radio")


def _phono() -> RotelInput:
    """Phono input."""
    return RotelInput("phono", "Phono", "mdi:music-note")


def _bluetooth() -> RotelInput:
    """Bluetooth input."""
    return RotelInput("bluetooth", "Bluetooth", "mdi:bluetooth")


def _pcusb() -> RotelInput:
    """PC-USB input, the command is ``pcusb!`` but reported as ``pc_usb``."""
    return RotelInput("pcusb", "PC USB", "mdi:usb", ("pc_usb", "usb"))


def _aux(index: int) -> RotelInput:
    """RCA line input ``index``.

    The input is called "Line" on the current models but was exposed as
    "Aux" before, so the old name stays resolvable for existing scripts.
    """
    aliases = ("aux", f"aux {index}", f"aux{index}") if index == 1 else (
        f"aux {index}",
        f"aux{index}",
    )
    return RotelInput(f"aux{index}", f"Line {index}", "mdi:audio-input", aliases)


def _coax(
    index: int, label: str = "Coax", aliases: tuple[str, ...] = ()
) -> RotelInput:
    """Coaxial digital input ``index``."""
    return RotelInput(f"coax{index}", f"{label} {index}", "mdi:surround-sound", aliases)


def _optical(
    index: int, label: str = "Optical", aliases: tuple[str, ...] = ()
) -> RotelInput:
    """Optical input ``index``."""
    return RotelInput(f"opt{index}", f"{label} {index}", "mdi:surround-sound", aliases)


def _xlr(
    index: int, label: str = "Balanced coax", aliases: tuple[str, ...] = ()
) -> RotelInput:
    """Balanced (XLR) input ``index``."""
    known: tuple[str, ...] = (
        f"balanced {index}",
        f"bal_xlr{index}",
        f"balanced{index}",
    )
    if index == 1:
        # Firmware revisions reporting a single balanced input.
        known += ("analog_balanced", "balanced")
    return RotelInput(
        f"bal_xlr{index}",
        f"{label} {index}",
        "mdi:surround-sound",
        (*known, *aliases),
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
    _coax(1),
    _coax(2),
    _optical(1),
    _optical(2),
    _xlr(1, "Balanced"),
    _xlr(2, "Balanced"),
    _aux(1),
    _aux(2),
)

#: The RBX-1500 has two more balanced inputs than the RCX processors.
_RBX_INPUTS = (
    _hdmi(1),
    _hdmi(2),
    _hdmi(3),
    _hdmi(4),
    _coax(1),
    _coax(2),
    _optical(1),
    _optical(2),
    _xlr(1, "Balanced"),
    _xlr(2, "Balanced"),
    _xlr(3, "Balanced"),
    _xlr(4, "Balanced"),
    _aux(1),
    _aux(2),
)

#: Inputs of the two channel amplifiers with a network interface. The coax
#: aliases keep the names the released version used ("Coax 1", "Optical 1",
#: "Balanced") resolvable on the RA-1200, whose input set was renamed.
_INTEGRATED_INPUTS = (
    _cd(),
    _tuner(),
    _xlr(1),
    _xlr(2),
    _coax(1, "Optical coax", (f"coax {1}", f"optical {1}")),
    _coax(2, "Optical coax", (f"coax {2}", f"optical {2}")),
    _aux(1),
    _aux(2),
    _phono(),
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
            inputs=(*_INTEGRATED_INPUTS, _bluetooth(), _pcusb()),
        ),
        RotelModel(
            key="ra1200",
            name="Rotel RA-1200 MkII",
            rbc="RA1200",
            inputs=(*_INTEGRATED_INPUTS, _bluetooth(), _pcusb()),
        ),
        RotelModel(
            key="rcx1570",
            name="Rotel RCX-1570 MkII",
            rbc="RCX1570",
            inputs=(
                _cd(),
                _tuner(),
                _coax(1),
                _coax(2),
                _optical(1),
                _optical(2),
                _xlr(1, "Balanced"),
                _xlr(2, "Balanced"),
                _aux(1),
                _aux(2),
                _phono(),
                _bluetooth(),
                _pcusb(),
            ),
            record_inputs=(_cd(), _tuner(), _aux(1), _aux(2)),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
            queries=(RotelQuery.RECORD_SOURCE,),
        ),
        RotelModel(
            key="rcx1500",
            name="Rotel RCX-1500",
            rbc="RCX1500",
            inputs=(*_PROCESSOR_INPUTS, _cd(), _tuner(), _phono(), _bluetooth()),
            record_inputs=(_cd(), _tuner()),
            volume_min_db=-80.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
            zone2=True,
            queries=(RotelQuery.RECORD_SOURCE,),
        ),
        RotelModel(
            key="rbx1500",
            name="Rotel RBX-1500",
            rbc="RBX1500",
            inputs=(*_RBX_INPUTS, _cd(), _tuner(), _bluetooth()),
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
                _optical(1),
                _coax(1),
                _xlr(1, "Balanced"),
                _aux(1),
                _aux(2),
                _phono(),
                _bluetooth(),
                _pcusb(),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_scale=VolumeScale.DB,
        ),
        # Generic fallback for unknown Rotel devices: raw 0..96 volume scale,
        # common Rotel input naming.
        RotelModel(
            key="generic",
            name="Rotel (generic)",
            rbc="",
            inputs=(
                _cd(),
                _tuner(),
                _coax(1),
                _coax(2),
                _optical(1),
                _optical(2),
                _aux(1),
                _aux(2),
                _phono(),
                _bluetooth(),
                _pcusb(),
                _xlr(1, "Balanced"),
            ),
        ),
    )
}


def get_model(key: str | None) -> RotelModel:
    """Return the profile for ``key`` falling back to the generic profile."""
    if key and key in ROTEL_MODELS:
        return ROTEL_MODELS[key]
    return ROTEL_MODELS["generic"]


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


__all__ = (
    "COMMAND_TERMINATOR",
    "NEGATIVE_RESPONSES",
    "RESPONSE_TERMINATOR",
    "ROTEL_MODELS",
    "ProtocolError",
    "RotelCommand",
    "RotelInput",
    "RotelModel",
    "RotelQuery",
    "VolumeScale",
    "build_command",
    "build_query",
    "clamp_volume",
    "detect_model",
    "get_model",
    "match_input",
    "parse_message",
    "parse_messages",
    "parse_on_off",
    "parse_source",
    "parse_volume_number",
    "snap_volume",
    "source_command",
    "volume_from_payload",
    "volume_is_out_of_range",
    "volume_payload_range",
    "volume_to_payload",
)
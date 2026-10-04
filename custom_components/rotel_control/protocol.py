"""Pure protocol helpers for Rotel control interfaces.

This module deliberately has **no Home Assistant imports** so that the wire
format can be unit-tested in isolation and reused by the RS-232 variant of
the protocol.

Rotel amplifiers reachable over TCP (port 9500) or RS-232 speak a line based
ASCII protocol::

    -> VOLUME_QUERY\\r\\n
    <- VOLUME 30\\r\\n
    -> VOLUME 30\\r\\n        (no answer, or a bare "OK")

Models differ in three ways only, and all three are described by
:class:`RotelModel` so a new device usually needs nothing but a new profile:

* how a volume is encoded on the wire (percent, 0.5 dB steps, raw steps),
* the numeric value of every input,
* the volume range and granularity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

#: Every command is a single ASCII line terminated by CR LF.
TERMINATOR = "\r\n"

#: Replies a Rotel sends when it does not understand (or refuses) a command.
NEGATIVE_RESPONSES = frozenset({"?", "no_reply", "error", "nack"})

#: Replies that mean "command accepted", carrying no state.
ACK_RESPONSES = frozenset({"ok", "ack", "complete"})


class RotelCommand(StrEnum):
    """Commands understood by the Rotel ASCII control protocol."""

    POWER = "POWER"
    POWER_QUERY = "POWER_QUERY"
    VOLUME = "VOLUME"
    VOLUME_QUERY = "VOLUME_QUERY"
    MUTE = "MUTE"
    MUTE_QUERY = "MUTE_QUERY"
    SOURCE = "SOURCE"
    SOURCE_QUERY = "SOURCE_QUERY"
    RECORD_SOURCE = "REC_SELECT"
    RECORD_SOURCE_QUERY = "REC_SELECT_QUERY"
    MODEL_QUERY = "MODEL_QUERY"
    FIRMWARE_QUERY = "FIRMWARE_QUERY"
    RESET_PANEL = "RESET_PANEL"


class VolumeScale(StrEnum):
    """How a volume in dB is turned into the integer sent to the device.

    ``STEPS`` is the default because it round-trips exactly for devices whose
    front panel steps in 0.5 dB (0..160 for a -60..+20 dB range).  Use
    ``PERCENT`` only when the firmware reports a 0..100 percentage; note that
    a percent payload cannot represent every 0.5 dB step.
    """

    #: Payload is the offset from ``volume_min_db`` in ``volume_step_db`` units.
    STEPS = "steps"
    #: Payload is 0..100 percent of the volume range.
    PERCENT = "percent"


class ProtocolError(Exception):
    """Raised when a reply cannot be interpreted."""


@dataclass(frozen=True, slots=True)
class RotelInput:
    """A single input (source) of an amplifier."""

    value: str
    name: str
    icon: str = "mdi:audio-input"

    @property
    def label(self) -> str:
        """Human readable label shown in the UI."""
        return self.name


@dataclass(frozen=True, slots=True)
class RotelModel:
    """Static description of an amplifier model."""

    key: str
    name: str
    #: Rotel Base Code reported by ``MODEL_QUERY``, e.g. ``"RA1572"``.
    rbc: str
    inputs: tuple[RotelInput, ...]
    record_inputs: tuple[RotelInput, ...] = ()
    volume_min_db: float = -60.0
    volume_max_db: float = 20.0
    volume_step_db: float = 0.5
    volume_scale: VolumeScale = VolumeScale.STEPS
    #: Number of steps the device reports (used to sanity check replies).
    zone2: bool = False

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

    def input_by_value(self, value: str) -> RotelInput | None:
        """Return the input matching ``value`` (case/space insensitive)."""
        needle = value.strip().upper()
        for item in self.inputs:
            if item.value.upper() == needle:
                return item
        return None

    def input_by_name(self, name: str) -> RotelInput | None:
        """Return the input whose label matches ``name``."""
        needle = name.strip().casefold()
        for item in self.inputs:
            if item.name.casefold() == needle:
                return item
        return None


ROTEL_MODELS: Mapping[str, RotelModel] = {
    model.key: model
    for model in (
        RotelModel(
            key="ra1572",
            name="Rotel RA-1572",
            rbc="RA1572",
            inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("BALANCED COAX1", "Balanced coax 1", "mdi:surround-sound"),
                RotelInput("BALANCED COAX2", "Balanced coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL COAX1", "Optical coax 1", "mdi:surround-sound"),
                RotelInput("OPTICAL COAX2", "Optical coax 2", "mdi:surround-sound"),
                RotelInput("LINE1", "Line 1", "mdi:audio-input"),
                RotelInput("LINE2", "Line 2", "mdi:audio-input"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        RotelModel(
            key="ra1572mkii",
            name="Rotel RA-1572 MkII",
            rbc="RA1572MKII",
            inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("BALANCED COAX1", "Balanced coax 1", "mdi:surround-sound"),
                RotelInput("BALANCED COAX2", "Balanced coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL COAX1", "Optical coax 1", "mdi:surround-sound"),
                RotelInput("OPTICAL COAX2", "Optical coax 2", "mdi:surround-sound"),
                RotelInput("LINE1", "Line 1", "mdi:audio-input"),
                RotelInput("LINE2", "Line 2", "mdi:audio-input"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        RotelModel(
            key="ra1200",
            name="Rotel RA-1200",
            rbc="RA1200",
            inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("AUX1", "Aux 1", "mdi:audio-input"),
                RotelInput("AUX2", "Aux 2", "mdi:audio-input"),
                RotelInput("COAX1", "Coax 1", "mdi:surround-sound"),
                RotelInput("COAX2", "Coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL1", "Optical 1", "mdi:surround-sound"),
                RotelInput("OPTICAL2", "Optical 2", "mdi:surround-sound"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
                RotelInput("BALANCED", "Balanced", "mdi:surround-sound"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        RotelModel(
            key="rcx1570",
            name="Rotel RCX-1570",
            rbc="RCX1570",
            inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("AUX1", "Aux 1", "mdi:audio-input"),
                RotelInput("AUX2", "Aux 2", "mdi:audio-input"),
                RotelInput("COAX1", "Coax 1", "mdi:surround-sound"),
                RotelInput("COAX2", "Coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL1", "Optical 1", "mdi:surround-sound"),
                RotelInput("OPTICAL2", "Optical 2", "mdi:surround-sound"),
                RotelInput("BALANCED1", "Balanced 1", "mdi:surround-sound"),
                RotelInput("BALANCED2", "Balanced 2", "mdi:surround-sound"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
                RotelInput("BLUETOOTH", "Bluetooth", "mdi:bluetooth"),
            ),
            record_inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("AUX1", "Aux 1", "mdi:audio-input"),
                RotelInput("AUX2", "Aux 2", "mdi:audio-input"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        RotelModel(
            key="rcx1500",
            name="Rotel RCX-1500",
            rbc="RCX1500",
            inputs=(
                RotelInput("HDMI1", "HDMI 1", "mdi:hdmi"),
                RotelInput("HDMI2", "HDMI 2", "mdi:hdmi"),
                RotelInput("HDMI3", "HDMI 3", "mdi:hdmi"),
                RotelInput("HDMI4", "HDMI 4", "mdi:hdmi"),
                RotelInput("COAX1", "Coax 1", "mdi:surround-sound"),
                RotelInput("COAX2", "Coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL1", "Optical 1", "mdi:surround-sound"),
                RotelInput("OPTICAL2", "Optical 2", "mdi:surround-sound"),
                RotelInput("BALANCED1", "Balanced 1", "mdi:surround-sound"),
                RotelInput("BALANCED2", "Balanced 2", "mdi:surround-sound"),
                RotelInput("LINE1", "Line 1", "mdi:audio-input"),
                RotelInput("LINE2", "Line 2", "mdi:audio-input"),
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
            ),
            record_inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
            ),
            volume_min_db=-80.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
            zone2=True,
        ),
        RotelModel(
            key="rbx1500",
            name="Rotel RBX-1500",
            rbc="RBX1500",
            inputs=(
                RotelInput("HDMI1", "HDMI 1", "mdi:hdmi"),
                RotelInput("HDMI2", "HDMI 2", "mdi:hdmi"),
                RotelInput("HDMI3", "HDMI 3", "mdi:hdmi"),
                RotelInput("HDMI4", "HDMI 4", "mdi:hdmi"),
                RotelInput("COAX1", "Coax 1", "mdi:surround-sound"),
                RotelInput("COAX2", "Coax 2", "mdi:surround-sound"),
                RotelInput("OPTICAL1", "Optical 1", "mdi:surround-sound"),
                RotelInput("OPTICAL2", "Optical 2", "mdi:surround-sound"),
                RotelInput("BALANCED1", "Balanced 1", "mdi:surround-sound"),
                RotelInput("BALANCED2", "Balanced 2", "mdi:surround-sound"),
                RotelInput("BALANCED3", "Balanced 3", "mdi:surround-sound"),
                RotelInput("BALANCED4", "Balanced 4", "mdi:surround-sound"),
                RotelInput("LINE1", "Line 1", "mdi:audio-input"),
                RotelInput("LINE2", "Line 2", "mdi:audio-input"),
            ),
            volume_min_db=-80.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        RotelModel(
            key="rca10",
            name="Rotel RCA-10",
            rbc="RCA10",
            inputs=(
                RotelInput("HDMI1", "HDMI 1", "mdi:hdmi"),
                RotelInput("HDMI2", "HDMI 2", "mdi:hdmi"),
                RotelInput("OPTICAL1", "Optical 1", "mdi:surround-sound"),
                RotelInput("COAX1", "Coax 1", "mdi:surround-sound"),
                RotelInput("BALANCED", "Balanced", "mdi:surround-sound"),
                RotelInput("LINE1", "Line 1", "mdi:audio-input"),
                RotelInput("LINE2", "Line 2", "mdi:audio-input"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
        # Generic fallback for unknown Rotel devices: percent volume,
        # 0.5 dB steps, common Rotel input naming.
        RotelModel(
            key="generic",
            name="Rotel (generic)",
            rbc="",
            inputs=(
                RotelInput("CD", "CD", "mdi:compact-disc"),
                RotelInput("TUNER", "Tuner", "mdi:radio"),
                RotelInput("AUX1", "Aux 1", "mdi:audio-input"),
                RotelInput("AUX2", "Aux 2", "mdi:audio-input"),
                RotelInput("COAX", "Coax", "mdi:surround-sound"),
                RotelInput("OPTICAL", "Optical", "mdi:surround-sound"),
                RotelInput("BALANCED", "Balanced", "mdi:surround-sound"),
                RotelInput("PHONO", "Phono", "mdi:music-note"),
                RotelInput("BLUETOOTH", "Bluetooth", "mdi:bluetooth"),
            ),
            volume_min_db=-60.0,
            volume_max_db=20.0,
            volume_step_db=0.5,
        ),
    )
}


def get_model(key: str | None) -> RotelModel:
    """Return the profile for ``key`` falling back to the generic profile."""
    if key and key in ROTEL_MODELS:
        return ROTEL_MODELS[key]
    return ROTEL_MODELS["generic"]


def detect_model(rbc: str | None) -> RotelModel | None:
    """Return the profile whose RBC matches ``rbc`` (case insensitive)."""
    if not rbc:
        return None
    needle = rbc.strip().upper().replace("-", "").replace(" ", "")
    for model in ROTEL_MODELS.values():
        if model.rbc and model.rbc.upper() == needle:
            return model
    return None


def build_command(command: RotelCommand | str, value: object | None = None) -> str:
    """Build a single command line (without terminator)."""
    base = str(command)
    if value is None:
        return base
    if isinstance(value, bool):
        return f"{base} {'on' if value else 'off'}"
    return f"{base} {value}"


# --- volume -------------------------------------------------------------


def clamp_volume(volume_db: float, model: RotelModel) -> float:
    """Clamp ``volume_db`` into the range supported by ``model``."""
    return min(max(volume_db, model.volume_min_db), model.volume_max_db)


def snap_volume(volume_db: float, model: RotelModel) -> float:
    """Clamp and round ``volume_db`` to a position the device can hold."""
    clamped = clamp_volume(volume_db, model)
    offset = clamped - model.volume_min_db
    steps = round(offset / model.volume_step_db)
    snapped = model.volume_min_db + steps * model.volume_step_db
    # Guard against floating point drift such as -60.00000000000001.
    return round(snapped, 2)


def volume_to_payload(volume_db: float, model: RotelModel) -> int:
    """Encode a volume in dB as the integer expected by the device."""
    snapped = snap_volume(volume_db, model)
    if model.volume_scale is VolumeScale.STEPS:
        return round((snapped - model.volume_min_db) / model.volume_step_db)
    percent = (snapped - model.volume_min_db) / model.volume_range_db * 100
    return int(round(percent))


def volume_payload_range(model: RotelModel) -> tuple[int, int]:
    """Return the inclusive range of volume payloads ``model`` understands."""
    if model.volume_scale is VolumeScale.STEPS:
        return 0, model.volume_steps
    return 0, 100


def volume_from_payload(payload: int | float, model: RotelModel) -> float:
    """Decode the integer reported by the device back to dB."""
    if model.volume_scale is VolumeScale.STEPS:
        raw = model.volume_min_db + float(payload) * model.volume_step_db
    else:
        percent = min(max(float(payload), 0.0), 100.0)
        raw = model.volume_min_db + (percent / 100.0) * model.volume_range_db
    return snap_volume(raw, model)


# --- reply parsing ------------------------------------------------------


def strip_echo(line: str, command: str | None = None) -> str:
    """Remove CRLF, a duplicated command prefix and stray quotes."""
    cleaned = line.strip().strip('"').strip()
    if command and cleaned.upper().startswith(f"{command.upper()} "):
        cleaned = cleaned[len(command) + 1 :].strip()
    elif command and cleaned.upper() == command.upper():
        return ""
    return cleaned


def is_ack(line: str) -> bool:
    """Return True for acknowledgements that carry no state."""
    return line.strip().strip('"').casefold() in ACK_RESPONSES


def parse_on_off(value: str) -> bool:
    """Parse the many spellings Rotel uses for booleans."""
    token = value.strip().strip('"').casefold()
    if token in {"1", "on", "true", "yes", "standby_off"}:
        return True
    if token in {"0", "off", "false", "no", "standby"}:
        return False
    raise ProtocolError(f"Unexpected boolean value: {value!r}")


def parse_command(line: str) -> tuple[str, str] | None:
    """Split a reply into ``(command, payload)``.

    Returns ``None`` for empty lines, echoes of our own command and
    acknowledgements, so callers can simply keep reading.
    """
    cleaned = line.strip().strip('"')
    if not cleaned or is_ack(cleaned) or cleaned == "?":
        return None
    command, _, payload = cleaned.partition(" ")
    return command.strip().upper(), payload.strip()


def parse_source(payload: str, model: RotelModel, *, record: bool = False) -> RotelInput:
    """Resolve a reported input value to a :class:`RotelInput`."""
    inputs = model.record_inputs if record else model.inputs
    needle = payload.strip().strip('"')
    for item in inputs:
        if item.value.upper() == needle.upper():
            return item
    # Some firmware revisions report the label instead of the protocol value.
    for item in inputs:
        if item.name.casefold() == needle.casefold():
            return item
    raise ProtocolError(f"Unknown input {payload!r} for {model.name}")


def source_payload(value: str, model: RotelModel, *, record: bool = False) -> str:
    """Return the protocol value for an input label or value."""
    inputs = model.record_inputs if record else model.inputs
    for item in inputs:
        if value.casefold() in {item.value.casefold(), item.name.casefold()}:
            return item.value
    raise ProtocolError(f"Unknown input {value!r} for {model.name}")


__all__ = (
    "ACK_RESPONSES",
    "NEGATIVE_RESPONSES",
    "ROTEL_MODELS",
    "TERMINATOR",
    "ProtocolError",
    "RotelCommand",
    "RotelInput",
    "RotelModel",
    "VolumeScale",
    "build_command",
    "clamp_volume",
    "detect_model",
    "get_model",
    "is_ack",
    "parse_command",
    "parse_on_off",
    "parse_source",
    "snap_volume",
    "source_payload",
    "strip_echo",
    "volume_from_payload",
    "volume_payload_range",
    "volume_to_payload",
)

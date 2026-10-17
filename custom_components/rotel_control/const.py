"""Constants for the Rotel Amplifier integration.

Everything that describes a *device* rather than a transport lives here: the
inputs a Rotel can have, and the limits of the tone controls. The wire format
is built on top of these values in :mod:`.protocol`, which therefore imports
this module and not the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

DOMAIN: Final = "rotel_control"
LOGGER: Final = "custom_components.rotel_control"

# --- Configuration keys -------------------------------------------------
CONF_MODEL_PROFILE: Final = "model_profile"
CONF_POLL_INTERVAL: Final = "poll_interval"
#: Protocol values of the inputs the device has, empty = use the profile.
CONF_INPUTS: Final = "inputs"
#: React to what the amplifier reports without being asked (front panel, remote
#: control, the Rotel app) instead of waiting for the next poll.
CONF_PUSH_UPDATES: Final = "push_updates"
DEFAULT_PUSH_UPDATES: Final = True

# Host/port/name come from homeassistant.const.
#: TCP port of the Rotel ASCII control interface. 9590 is used by every model
#: with a network port; 9600/9500 show up on older units.
DEFAULT_PORT: Final = 9590
#: Ports that are probed when discovery did not announce a usable one.
CANDIDATE_PORTS: Final = (DEFAULT_PORT, 9600, 9500)
DEFAULT_POLL_INTERVAL: Final = 30
MIN_POLL_INTERVAL: Final = 1
MAX_POLL_INTERVAL: Final = 300
#: Minimum poll interval when push updates are enabled, so the device can idle.
MIN_PUSH_POLL_INTERVAL: Final = 1800
#: How long the client keeps a connection open after the last exchange before
#: closing it, giving an amplifier with Auto Power Down a chance to engage.
IDLE_TIMEOUT: Final = 300.0

# --- Networking ---------------------------------------------------------
PORT_MIN: Final = 1
PORT_MAX: Final = 65535
CONNECT_TIMEOUT: Final = 5.0
SOCKET_TIMEOUT: Final = 3.0
# Amount of time we are willing to wait for a *late* answer to a command
# that does not return data (e.g. "vol_42!") before discarding it. The wait is
# restarted while the device keeps sending, so a slow burst is still collected.
DRAIN_TIMEOUT: Final = 0.05
#: Hard limit for one drain, so a device that streams forever cannot stall a poll.
DRAIN_MAX_TIMEOUT: Final = 1.0
#: Short timeouts used while probing a discovered host, so a wrong port fails
#: fast instead of blocking the config flow.
PROBE_CONNECT_TIMEOUT: Final = 2.0
PROBE_TIMEOUT: Final = 1.0
#: How long a command waits for the amplifier to answer with the field it
#: changed. A Rotel confirms a command (``bass_+05!`` -> ``bass=+05$``), and
#: that answer is the freshest reading of the setting: asking the amplifier
#: for its state again right afterwards can only return a value from *before*
#: the change, which is what made a setting snap back. A unit that answers
#: nothing must not make a change feel stuck, so the wait is short and a
#: silence is not an error.
COMMAND_CONFIRM_TIMEOUT: Final = 0.5
#: Largest amount of unterminated data kept while waiting for a ``$``. A device
#: that never frames its replies is disconnected instead of filling memory.
MAX_BUFFER_SIZE: Final = 4096
#: Amount of data read from the socket in one go. Replies are a few bytes
#: each, so anything larger is a device that streams (a status dump).
READ_CHUNK: Final = 512
#: When a command produces a reply field, keep an echo marker for that long
#: so an unsolicited report that races the command answer is still treated as
#: the confirmation.
PUSH_ECHO_TTL: Final = 2.0
#: Size of the queue that hands unsolicited reports to the listener.
PUSH_QUEUE_SIZE: Final = 32

# How long to wait after a power-on before the interlock volume command must be
# re-sent (Rotel silences the pre-outs on standby power-up until a volume is
# applied).
INTERLOCK_VOLUME_DELAY: Final = 0.3
#: Maximum length of a string value a device may send.
MAX_VALUE_LENGTH: Final = 32
#: Bass and treble of the Rotel tone block, in dB. The device takes whole
#: steps only, and ``000`` is the neutral position.
TONE_MIN_DB: Final = -10
TONE_MAX_DB: Final = 10
TONE_STEP_DB: Final = 1
#: Balance: ``L01``..``L15`` and ``R01``..``R15``, ``000`` is centred.
BALANCE_MIN: Final = -15
BALANCE_MAX: Final = 15
BALANCE_STEP: Final = 1
#: Speaker groups that can be switched on their own.
SPEAKER_GROUPS: Final[tuple[str, ...]] = ("a", "b")
#: Front display brightness, ``0`` is the brightest and ``6`` the dimmest.
DIMMER_MIN: Final = 0
DIMMER_MAX: Final = 6
#: Speaker state tokens of a ``speaker=`` reply.
SPEAKER_STATES: Final[tuple[str, ...]] = ("off", "a", "b", "a_b")
#: Options of the display dimmer select, as strings so they can be translated.
DIMMER_OPTIONS: Final[tuple[str, ...]] = tuple(
    str(level) for level in range(DIMMER_MIN, DIMMER_MAX + 1)
)
@dataclass(frozen=True, slots=True)
class RotelInput:
    """A single input (source) of an amplifier.

    ``value`` is what is sent to the device (``coax1`` -> ``coax1!``) and
    ``name`` is what the user interface shows. ``aliases`` carries the other
    spellings a firmware may report, so a device that answers ``source=aux1``
    still resolves to the ``Aux`` input of the catalogue.
    """

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


#: Inputs of the current Rotel network units, in the order of the catalogue a
#: user picks from. Every profile in ``protocol.ROTEL_MODELS`` is assembled
#: from these entries, so a name means the same thing on every device.
#:
#: A device only reports the inputs it has, and the selection is verified
#: against the answer, so an input that does not exist is harmless: it is never
#: selected by accident and a firmware that rejects it changes nothing.
SOURCE_CD: Final = RotelInput("cd", "CD", "mdi:compact-disc", ("analog_cd",))
SOURCE_TUNER: Final = RotelInput("tuner", "Tuner", "mdi:radio")
SOURCE_PHONO: Final = RotelInput("phono", "Phono", "mdi:music-note")
SOURCE_COAX1: Final = RotelInput(
    "coax1", "Coax 1", "mdi:surround-sound", ("coax 1", "coaxial 1")
)
SOURCE_COAX2: Final = RotelInput(
    "coax2", "Coax 2", "mdi:surround-sound", ("coax 2", "coaxial 2")
)
SOURCE_OPTICAL1: Final = RotelInput(
    "opt1", "Optical 1", "mdi:surround-sound", ("optical1", "opt 1", "optical 1")
)
SOURCE_OPTICAL2: Final = RotelInput(
    "opt2", "Optical 2", "mdi:surround-sound", ("optical2", "opt 2", "optical 2")
)
#: The line level input of the RC/RA series. Older units number it (``aux1``)
#: and some report it as ``line1``, both resolve to this one entry.
#: Named in upper case so Google Home and other Matter controllers render it
#: as an audio label ("AUX") rather than a sentence-cased word.
SOURCE_AUX: Final = RotelInput(
    "aux", "AUX", "mdi:audio-input", ("aux1", "line1", "line 1")
)
#: Front USB of the models with a USB input. ``pcusb`` below is the *PC-USB*
#: input, which is a different physical connector. The trailing "Audio" makes
#: the voice assistant answer "USB Audio" instead of the bare "USB", which a
#: controller could otherwise confuse with a PC-USB or storage device.
SOURCE_USB: Final = RotelInput("usb", "USB Audio", "mdi:usb", ("usb1", "front usb"))
SOURCE_BLUETOOTH: Final = RotelInput(
    "bluetooth", "Bluetooth", "mdi:bluetooth", ("bt", "bluetooth1")
)
#: The single balanced (XLR) input of the integrated amplifiers. Models with
#: more of them build their own entries in ``protocol.py``; a unit never
#: reports two balanced inputs under one label.
SOURCE_BALANCED: Final = RotelInput(
    "bal_xlr",
    "Balanced",
    "mdi:surround-sound",
    ("bal_xlr1", "balanced", "bal", "balanced 1", "xlr"),
)
SOURCE_PCUSB: Final = RotelInput(
    "pcusb", "PC USB", "mdi:usb", ("pc_usb", "pc-usb", "pc usb")
)

#: Every input a Rotel with a network port is documented to have.
ROTEL_INPUTS: Final[tuple[RotelInput, ...]] = (
    SOURCE_CD,
    SOURCE_TUNER,
    SOURCE_PHONO,
    SOURCE_COAX1,
    SOURCE_COAX2,
    SOURCE_OPTICAL1,
    SOURCE_OPTICAL2,
    SOURCE_AUX,
    SOURCE_BALANCED,
    SOURCE_USB,
    SOURCE_PCUSB,
    SOURCE_BLUETOOTH,
)# --- Volume scale ------------------------------------------------------
#: the wire. This is the only volume scale the protocol speaks for them, and it
#: is what Home Assistant is shown, so it is the one thing the range of a
#: profile has to be exactly as long as.
VOLUME_UNITS: Final = 96
#: Decibel the front panel scale is anchored at. Those units never report
#: decibels themselves, so this only decides where the raw positions sit on the
#: linear decibel scale the integration carries internally: :data:`VOLUME_UNITS`
#: positions of :data:`VOLUME_STEP_DB` starting at :data:`VOLUME_MIN_DB`.
VOLUME_MIN_DB: Final = -60.0
VOLUME_STEP_DB: Final = 0.5
#: Decibel of the topmost position of the front panel scale, i.e. the default
#: upper bound of a profile that reports a raw volume.
VOLUME_MAX_DB: Final = VOLUME_MIN_DB + VOLUME_UNITS * VOLUME_STEP_DB
# Model profile used when the device does not report its model. Add new
# profiles to ``protocol.ROTEL_MODELS`` as well.
DEFAULT_MODEL_PROFILE: Final = "ra1572"
#: Prefix of the entity name if the user did not choose one.
DEFAULT_NAME: Final = "Rotel"
#: Unique identifier for the SSID discovery.
ROTEL_SSID: Final = "Rotel"
#: When the user presses a physical button, the device may answer after a
#: short delay. The debounce in the coordinator absorbs those rapid reports
#: so the event bus is not flooded.
PUSH_DEBOUNCE_SECONDS: Final = 0.1
# --- Entities -----------------------------------------------------------
PLATFORMS: Final[list[str]] = [
    "media_player",
    "number",
    "select",
    "sensor",
    "switch",
]
# --- Events ------------------------------------------------------------
#: Event fired when the device reports a change made elsewhere.
EVENT_COMMAND_RECEIVED: Final = f"{DOMAIN}_command_received"
# --- Origins ------------------------------------------------------------
#: Where a change comes from when firing :data:`EVENT_COMMAND_RECEIVED`.
ORIGIN_PUSH: Final = "push"
ORIGIN_POLL: Final = "poll"
ORIGIN_COMMAND: Final = "command"
# filtered by ROTEL_NAME_MARKERS and verified against the protocol.
ROTEL_NAME_MARKERS: Final[tuple[str, ...]] = ("rotel",)
#: ``{value: label}`` of the catalogue, used by the config and options flows.
INPUT_LABELS: Final[dict[str, str]] = {
    item.value: item.name for item in ROTEL_INPUTS
}

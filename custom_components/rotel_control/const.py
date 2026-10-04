"""Constants for the Rotel Amplifier integration."""

from __future__ import annotations

from typing import Final

from .protocol import ROTEL_MODELS

DOMAIN: Final = "rotel_control"
LOGGER: Final = "custom_components.rotel_control"

# ``{profile_key: "Rotel RA-1572"}`` for the model selector of the flows.
MODEL_LABELS: Final[dict[str, str]] = {
    key: model.name for key, model in ROTEL_MODELS.items()
}

# --- Configuration keys -------------------------------------------------
CONF_MODEL_PROFILE: Final = "model_profile"
CONF_POLL_INTERVAL: Final = "poll_interval"

# Host/port/name come from homeassistant.const.
#: TCP port of the Rotel ASCII control interface. 9590 is used by every model
#: with a network port; 9600/9500 show up on older units.
DEFAULT_PORT: Final = 9590
#: Ports that are probed when discovery did not announce a usable one.
CANDIDATE_PORTS: Final = (DEFAULT_PORT, 9600, 9500)
DEFAULT_POLL_INTERVAL: Final = 2
MIN_POLL_INTERVAL: Final = 1
MAX_POLL_INTERVAL: Final = 300

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
#: Largest amount of unterminated data kept while waiting for a ``$``. A device
#: that never frames its replies is disconnected instead of filling memory.
MAX_BUFFER_SIZE: Final = 4096
#: Longest value cached from a device reply. Device strings end up in entity
#: attributes and diagnostics, so they must stay short whatever the device sends.
MAX_VALUE_LENGTH: Final = 32

# Model profile used when the device does not report its model. Add new
# profiles to protocol.py (ROTEL_MODELS) — no other file needs changes.
DEFAULT_MODEL_PROFILE: Final = "ra1572"

# --- Entities -----------------------------------------------------------
PLATFORMS: Final[list[str]] = ["media_player", "number", "select", "sensor"]

# How long to wait after a power-on before the interlock volume command must be
# re-sent (Rotel silences the pre-outs on standby power-up until a volume is
# applied).
INTERLOCK_VOLUME_DELAY: Final = 0.3

# --- Discovery ----------------------------------------------------------
# The zeroconf services are declared in manifest.json; announcements are then
# filtered by ROTEL_NAME_MARKERS and verified against the protocol.
#: Names that identify a Rotel in an SSDP/mDNS announcement.
ROTEL_NAME_MARKERS: Final[tuple[str, ...]] = ("rotel",)

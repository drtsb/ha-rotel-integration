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
DEFAULT_PORT: Final = 9500
DEFAULT_POLL_INTERVAL: Final = 2
MIN_POLL_INTERVAL: Final = 1
MAX_POLL_INTERVAL: Final = 300

# --- Networking ---------------------------------------------------------
PORT_MIN: Final = 1
PORT_MAX: Final = 65535
CONNECT_TIMEOUT: Final = 5.0
SOCKET_TIMEOUT: Final = 3.0
# Amount of time we are willing to wait for a *late* answer to a command
# that does not return data (e.g. "VOLUME 30") before discarding it.
DRAIN_TIMEOUT: Final = 0.05
QUERY_ATTEMPTS: Final = 2

# Rotel models that expose the ASCII control protocol on TCP/9500.
# Add new profiles to protocol.py (ROTEL_MODELS) — no other file needs changes.
DEFAULT_MODEL_PROFILE: Final = "ra1572"

# --- Entities -----------------------------------------------------------
PLATFORMS: Final[list[str]] = ["media_player", "number", "select", "sensor"]

# How long to wait after a power-on before the interlock volume command must be
# re-sent (Rotel silences the pre-outs on standby power-up until a volume is
# applied).
INTERLOCK_VOLUME_DELAY: Final = 0.3

# --- Discovery ----------------------------------------------------------
ZEROCONF_TYPE_PREFIX: Final = "_rotel"
SSDP_UPNP_MARKERS: Final = ("rotel",)

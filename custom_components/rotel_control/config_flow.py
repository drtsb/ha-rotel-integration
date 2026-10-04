"""Config flow for the Rotel integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .api import RotelApi, RotelApiError
from .const import (
    CONF_MODEL_PROFILE,
    DEFAULT_MODEL_PROFILE,
    DEFAULT_PORT,
    DOMAIN,
    MODEL_LABELS,
    PORT_MAX,
    PORT_MIN,
    SSDP_UPNP_MARKERS,
)
from .options_flow import RotelOptionsFlow
from .protocol import RotelModel, detect_model, get_model

LOGGER: logging.Logger = logging.getLogger(__package__)


class CannotConnect(Exception):
    """Raised when the device does not answer the Rotel protocol."""


def model_schema(
    default_model: str = DEFAULT_MODEL_PROFILE,
    default_host: str | None = None,
    default_port: int = DEFAULT_PORT,
    suggest_name: str | None = None,
) -> vol.Schema:
    """Return the schema used by the user, reauth and reconfigure steps."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=default_host or vol.UNDEFINED): str,
            vol.Required(CONF_PORT, default=default_port): vol.All(
                vol.Coerce(int), vol.Range(min=PORT_MIN, max=PORT_MAX)
            ),
            vol.Required(CONF_MODEL_PROFILE, default=default_model): vol.In(
                MODEL_LABELS
            ),
            vol.Optional(CONF_NAME, default=suggest_name or vol.UNDEFINED): str,
        }
    )


async def async_validate_input(
    host: str,
    port: int,
    model_key: str,
) -> tuple[RotelModel, dict[str, Any]]:
    """Connect to the amplifier and report which profile to use.

    A model reported by the device always wins over the selected profile,
    because it carries the exact input list of that unit.

    Raises:
        CannotConnect: the socket could not be opened or did not speak Rotel.
    """
    model = get_model(model_key)
    api = RotelApi(host, port, model)
    try:
        info = await api.async_validate()
    except (RotelApiError, TimeoutError, OSError) as err:
        LOGGER.debug("Validation of %s:%s failed: %s", host, port, err)
        raise CannotConnect(str(err)) from err
    finally:
        await api.async_disconnect()

    if info.get("model"):
        if detected := detect_model(info["model"]):
            model = detected
        elif model.rbc and model.rbc.casefold() != str(info["model"]).casefold():
            LOGGER.warning(
                "Device reports model %s, profile %s is in use",
                info["model"],
                model.key,
            )
    info["model_key"] = model.key
    return model, info


def _service_info_value(info: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an ``SsdpServiceInfo``/``ZeroconfServiceInfo``.

    Both are dataclasses in modern Home Assistant but were dicts historically,
    and ``SsdpServiceInfo`` moved around between releases, so stay tolerant.
    """
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


def _discovery_host(info: Any) -> str | None:
    """Extract the host name/IP of a discovered device."""
    host = _service_info_value(info, "host")
    if host:
        return str(host)
    location = _service_info_value(info, "ssdp_location") or _service_info_value(
        info, "location"
    )
    if location:
        # ssdp://192.168.1.50:8080/description.xml
        tail = str(location).removeprefix("ssdp://").split("/", 1)[0]
        return tail.rpartition(":")[0] or tail or None
    upnp = _service_info_value(info, "upnp")
    if isinstance(upnp, dict) and upnp.get("location"):
        return str(upnp["location"]).removeprefix("http://").split("/", 1)[0]
    address = _service_info_value(info, "ip_address")
    return str(address) if address else None


def _discovery_name(info: Any) -> str | None:
    """Extract a friendly name from an SSDP/zeroconf announcement."""
    for key in ("name", "hostname"):
        if value := _service_info_value(info, key):
            return str(value)
    for container in ("upnp", "properties"):
        value = _service_info_value(info, container)
        if isinstance(value, dict):
            for key in ("friendlyName", "modelName", "name"):
                if name := value.get(key):
                    return str(name)
    return None


def _is_rotel(info: Any) -> bool:
    """Return True when an SSDP/zeroconf announcement looks like a Rotel."""
    haystack: list[str] = []
    for key in ("upnp", "properties", "ssdp_usn", "name", "type"):
        value = _service_info_value(info, key)
        if isinstance(value, dict):
            haystack.extend(str(item) for item in value.values())
        elif value:
            haystack.append(str(value))
    blob = " ".join(haystack).casefold()
    return any(marker in blob for marker in SSDP_UPNP_MARKERS)


class RotelConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Rotel config flow."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovered: dict[str, Any] = {}

    # --- discovery -------------------------------------------------------

    async def async_step_ssdp(self, discovery_info: Any) -> ConfigFlowResult:
        """Handle a device found through SSDP."""
        LOGGER.debug("Rotel: SSDP discovery %s", discovery_info)
        if not _is_rotel(discovery_info):
            LOGGER.debug("Rotel: ignoring unrelated SSDP device")
            return self.async_abort(reason="not_rotel")
        if (host := _discovery_host(discovery_info)) is None:
            return self.async_abort(reason="not_supported")
        return await self._async_step_discovered(discovery_info, host, DEFAULT_PORT)

    async def async_step_zeroconf(
        self, discovery_info: Any
    ) -> ConfigFlowResult:
        """Handle a device found through zeroconf.

        ``discovery_info`` is a ``ZeroconfServiceInfo``, but that class moved
        between Home Assistant releases and importing it here would make the
        config flow depend on the zeroconf requirements being installed, so it
        is read through :func:`_service_info_value` instead.
        """
        LOGGER.debug("Rotel: zeroconf discovery %s", discovery_info)
        if not _is_rotel(discovery_info):
            return self.async_abort(reason="not_rotel")
        if (host := _discovery_host(discovery_info)) is None:
            return self.async_abort(reason="not_supported")
        port = int(
            _service_info_value(discovery_info, "port", DEFAULT_PORT) or DEFAULT_PORT
        )
        return await self._async_step_discovered(discovery_info, host, port)

    async def _async_step_discovered(
        self, info: Any, host: str, port: int
    ) -> ConfigFlowResult:
        """Pre-fill the user step with what discovery found."""
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()
        self._discovered = {
            "host": host,
            "port": port,
            "name": _discovery_name(info) or host,
        }
        self.context["title_placeholders"] = {"name": self._discovered["name"]}
        return await self.async_step_user()

    # --- user step -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the host, port and model, then verify the connection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host: str = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            model_key: str = user_input[CONF_MODEL_PROFILE]
            try:
                model, info = await async_validate_input(host, port, model_key)
            except CannotConnect as err:
                errors["base"] = "cannot_connect"
                LOGGER.debug("Rotel: cannot connect to %s: %s", host, err)
            else:
                await self.async_set_unique_id(host, raise_on_progress=False)
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_PORT: port,
                        CONF_MODEL_PROFILE: model.key,
                    }
                )
                title = user_input.get(CONF_NAME) or info.get("model") or host
                LOGGER.debug("Rotel: adding entry %s (profile %s)", title, model.key)
                return self.async_create_entry(
                    title=str(title),
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_NAME: title,
                        CONF_MODEL_PROFILE: model.key,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=model_schema(
                default_model=self._discovered.get(
                    "model_key", DEFAULT_MODEL_PROFILE
                ),
                default_host=self._discovered.get("host"),
                default_port=int(self._discovered.get("port", DEFAULT_PORT)),
                suggest_name=self._discovered.get("name"),
            ),
            errors=errors,
        )

    # --- reauth / reconfigure -------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle the credential-less reconnect of an unreachable device."""
        self._discovered = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user repair the connection of an existing entry."""
        entry = self._entry_from_context()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = await self._async_validate_updates(user_input, errors)
            if updates is not None:
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema_from_entry(entry),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user change host, port or model profile."""
        entry = self._entry_from_context()
        errors: dict[str, str] = {}
        if user_input is not None:
            updates = await self._async_validate_updates(user_input, errors)
            if updates is not None:
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema_from_entry(entry),
            errors=errors,
        )

    async def _async_validate_updates(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Validate input for reauth/reconfigure, filling ``errors`` on failure."""
        host = str(user_input[CONF_HOST]).strip()
        port = int(user_input[CONF_PORT])
        try:
            model, _ = await async_validate_input(
                host, port, user_input[CONF_MODEL_PROFILE]
            )
        except CannotConnect as err:
            errors["base"] = "cannot_connect"
            LOGGER.debug("Rotel: cannot connect to %s: %s", host, err)
            return None
        return {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_MODEL_PROFILE: model.key,
        }

    # --- helpers ---------------------------------------------------------

    def _entry_from_context(self) -> ConfigEntry:
        """Return the entry the flow was started for."""
        entry_id: str = self.context["entry_id"]
        if (entry := self.hass.config_entries.async_get_entry(entry_id)) is None:
            raise ValueError(f"Config entry {entry_id} not found")
        return entry

    @staticmethod
    def _schema_from_entry(entry: ConfigEntry) -> vol.Schema:
        """Build the reauth/reconfigure schema from the stored entry."""
        return model_schema(
            default_model=entry.data.get(CONF_MODEL_PROFILE, DEFAULT_MODEL_PROFILE),
            default_host=entry.data.get(CONF_HOST),
            default_port=int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            suggest_name=entry.data.get(CONF_NAME),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> RotelOptionsFlow:
        """Return the options flow of this handler.

        The entry is not passed to the flow: ``OptionsFlow.config_entry``
        resolves it from the flow handler on every supported release.
        """
        return RotelOptionsFlow()


__all__ = (
    "CannotConnect",
    "RotelConfigFlow",
    "async_validate_input",
    "model_schema",
)

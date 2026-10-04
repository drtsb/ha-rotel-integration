"""Config flow for the Rotel integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .api import (
    RotelApi,
    RotelApiClosedError,
    RotelApiConnectionError,
    RotelApiError,
    RotelApiProtocolError,
)
from .const import (
    CANDIDATE_PORTS,
    CONF_MODEL_PROFILE,
    CONNECT_TIMEOUT,
    DEFAULT_MODEL_PROFILE,
    DEFAULT_PORT,
    DOMAIN,
    MODEL_LABELS,
    PORT_MAX,
    PORT_MIN,
    PROBE_CONNECT_TIMEOUT,
    PROBE_TIMEOUT,
    ROTEL_NAME_MARKERS,
    SOCKET_TIMEOUT,
)
from .options_flow import RotelOptionsFlow
from .protocol import RotelModel, detect_model, get_model

LOGGER: logging.Logger = logging.getLogger(__package__)


class CannotConnect(Exception):
    """The socket could not be opened at all."""


class NoResponse(Exception):
    """The port answered, but not in the Rotel protocol."""


class DeviceClosed(Exception):
    """The device accepted the connection but closed it again."""


def model_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Return the schema used by the user, reauth and reconfigure steps.

    ``defaults`` carries what should be pre-filled: the values a previous
    (failed) attempt of this flow contained, or what discovery found.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_HOST, default=defaults.get(CONF_HOST) or vol.UNDEFINED
            ): str,
            vol.Required(
                CONF_PORT,
                default=int(defaults.get(CONF_PORT, DEFAULT_PORT)),
            ): vol.All(vol.Coerce(int), vol.Range(min=PORT_MIN, max=PORT_MAX)),
            vol.Required(
                CONF_MODEL_PROFILE,
                default=defaults.get(CONF_MODEL_PROFILE, DEFAULT_MODEL_PROFILE),
            ): vol.In(MODEL_LABELS),
            vol.Optional(
                CONF_NAME, default=defaults.get(CONF_NAME) or vol.UNDEFINED
            ): str,
        }
    )


async def _async_probe(
    host: str,
    port: int,
    model_key: str | None = None,
    *,
    connect_timeout: float = CONNECT_TIMEOUT,
    socket_timeout: float = SOCKET_TIMEOUT,
) -> tuple[RotelModel, dict[str, Any]]:
    """Connect to ``host``:``port`` and report which profile to use.

    A model reported by the device always wins over the selected profile,
    because it carries the exact input list of that unit.

    Raises:
        CannotConnect: the socket could not be opened.
        NoResponse: the socket was opened but nothing Rotel answered.
    """
    model = get_model(model_key)
    api = RotelApi(
        host,
        port,
        model,
        connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
    )
    try:
        info = await api.async_validate()
    except RotelApiClosedError as err:
        LOGGER.debug("%s:%s closed the connection: %s", host, port, err)
        raise DeviceClosed(str(err)) from err
    except RotelApiConnectionError as err:
        LOGGER.debug("Cannot open %s:%s: %s", host, port, err)
        raise CannotConnect(str(err)) from err
    except (RotelApiProtocolError, RotelApiError, TimeoutError, OSError) as err:
        LOGGER.debug("%s:%s does not answer the Rotel protocol: %s", host, port, err)
        raise NoResponse(str(err)) from err
    finally:
        await api.async_disconnect()

    return _merge_reported_model(model, info)


def _merge_reported_model(
    model: RotelModel, info: dict[str, Any]
) -> tuple[RotelModel, dict[str, Any]]:
    """Prefer the profile that matches what the device reported."""
    reported = info.get("model")
    if reported:
        if detected := detect_model(reported):
            model = detected
        elif model.rbc and model.rbc.casefold() != str(reported).casefold():
            LOGGER.warning(
                "Device reports model %s, profile %s is in use", reported, model.key
            )
    info["model_key"] = model.key
    return model, info


async def async_validate_input(
    host: str,
    port: int,
    model_key: str,
) -> tuple[RotelModel, dict[str, Any]]:
    """Connect to the amplifier and report which profile to use."""
    return await _async_probe(host, port, model_key)


async def async_find_rotel_port(
    host: str, ports: tuple[int, ...] = CANDIDATE_PORTS
) -> tuple[int, RotelModel, dict[str, Any]] | None:
    """Return the first port that speaks the Rotel protocol, ``None`` if none.

    The ports are probed concurrently: a host that only answers on the last
    candidate — or on none of them — must not serialise four connect timeouts.
    """
    probes = await asyncio.gather(
        *(
            _async_probe(
                host, port, connect_timeout=PROBE_CONNECT_TIMEOUT, socket_timeout=PROBE_TIMEOUT
            )
            for port in ports
        ),
        return_exceptions=True,
    )
    for port, probe in zip(ports, probes, strict=True):
        if isinstance(probe, BaseException):
            continue
        model, info = probe
        LOGGER.debug(
            "Rotel at %s answers on port %s (profile %s)", host, port, model.key
        )
        return port, model, info
    return None


def _service_info_value(info: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an ``SsdpServiceInfo``/``ZeroconfServiceInfo``.

    Both are dataclasses in modern Home Assistant but were dicts historically,
    and ``SsdpServiceInfo`` moved around between releases, so stay tolerant.
    """
    if isinstance(info, dict):
        return info.get(key, default)
    return getattr(info, key, default)


def _host_from_location(location: Any) -> str | None:
    """Return the host of an ``ssdp://``/``http://`` description URL."""
    url = str(location)
    authority = url.partition("://")[2] or url
    authority = authority.split("/", 1)[0]
    return authority.rpartition(":")[0] or authority or None


def _discovery_host(info: Any) -> str | None:
    """Extract the address to connect to from a discovered device.

    ``ip_address`` wins over ``host``: for zeroconf announcements ``host`` was
    a ``.local.`` name on older Home Assistant releases, which cannot be
    resolved reliably, while the IP address is what the socket layer needs.
    """
    for key in ("ip_address", "host"):
        if value := _service_info_value(info, key):
            return str(value)
    if location := _service_info_value(info, "ssdp_location") or _service_info_value(
        info, "location"
    ):
        return _host_from_location(location)
    upnp = _service_info_value(info, "upnp")
    if isinstance(upnp, dict) and upnp.get("location"):
        return _host_from_location(upnp["location"])
    return None


def _discovery_port(info: Any) -> int | None:
    """Return the port an announcement mentions, if it looks like one."""
    for key in ("port",):
        value = _service_info_value(info, key)
        if value and PORT_MIN <= int(value) <= PORT_MAX:
            return int(value)
    return None


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
    for key in (
        "upnp",
        "properties",
        "ssdp_usn",
        "usn",
        "name",
        "hostname",
        "type",
        "manufacturer",
        "model_name",
        "modelNumber",
        "manufacturerURL",
    ):
        value = _service_info_value(info, key)
        if isinstance(value, dict):
            haystack.extend(str(item) for item in value.values())
        elif value:
            haystack.append(str(value))
    blob = " ".join(haystack).casefold()
    return any(marker in blob for marker in ROTEL_NAME_MARKERS)


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
        host = _discovery_host(discovery_info)
        if host is None:
            return self.async_abort(reason="not_supported")
        return await self._async_step_discovered(discovery_info, host)

    async def async_step_zeroconf(self, discovery_info: Any) -> ConfigFlowResult:
        """Handle a device found through zeroconf.

        ``discovery_info`` is a ``ZeroconfServiceInfo``, but that class moved
        between Home Assistant releases and importing it here would make the
        config flow depend on the zeroconf requirements being installed, so it
        is read through :func:`_service_info_value` instead.
        """
        LOGGER.debug("Rotel: zeroconf discovery %s", discovery_info)
        if not _is_rotel(discovery_info):
            return self.async_abort(reason="not_rotel")
        host = _discovery_host(discovery_info)
        if host is None:
            return self.async_abort(reason="not_supported")
        return await self._async_step_discovered(discovery_info, host)

    async def _async_step_discovered(
        self, info: Any, host: str
    ) -> ConfigFlowResult:
        """Verify the announced device and pre-fill the user step with it.

        The announcement only names the device, never the control port, so the
        candidate ports are probed and the one that answers is pre-filled.
        """
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        announced = _discovery_port(info)
        # A zeroconf announcement carries the port of the announced service,
        # which is usually the web interface and not the control port, so the
        # well known ports are probed first.
        ports = list(CANDIDATE_PORTS)
        if announced and announced not in ports:
            ports.append(announced)
        if (found := await async_find_rotel_port(host, tuple(ports))) is None:
            LOGGER.debug("Rotel: %s did not answer on any known port", host)
            return self.async_abort(reason="not_rotel")
        port, model, device_info = found

        self._discovered = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_NAME: _discovery_name(info) or device_info.get("model") or host,
            CONF_MODEL_PROFILE: model.key,
        }
        self.context["title_placeholders"] = {"name": self._discovered[CONF_NAME]}
        return await self.async_step_user()

    # --- user step -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the host, port and model, then verify the connection."""
        errors: dict[str, str] = {}
        # What the form should show: discovery first, then whatever the user
        # just typed, so a failed attempt never clears the form.
        defaults: dict[str, Any] = dict(self._discovered)

        if user_input is not None:
            defaults.update(user_input)
            host: str = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            model_key: str = user_input[CONF_MODEL_PROFILE]
            model, info, errors = await self._async_probe_input(
                host, port, model_key
            )
            if model is not None:
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

        schema = self.add_suggested_values_to_schema(
            model_schema(defaults), user_input or defaults
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
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
        defaults = self._schema_defaults(entry)
        if user_input is not None:
            defaults.update(user_input)
            updates, errors = await self._async_validate_updates(user_input)
            if updates is not None:
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        schema = self.add_suggested_values_to_schema(
            model_schema(defaults), user_input or defaults
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user change host, port or model profile."""
        entry = self._entry_from_context()
        errors: dict[str, str] = {}
        defaults = self._schema_defaults(entry)
        if user_input is not None:
            defaults.update(user_input)
            updates, errors = await self._async_validate_updates(user_input)
            if updates is not None:
                return self.async_update_reload_and_abort(entry, data_updates=updates)

        schema = self.add_suggested_values_to_schema(
            model_schema(defaults), user_input or defaults
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    async def _async_validate_updates(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Validate input for reauth/reconfigure.

        Returns the data updates for the entry, or ``None`` plus the form
        errors when the device could not be verified.
        """
        host = str(user_input[CONF_HOST]).strip()
        port = int(user_input[CONF_PORT])
        model, _, errors = await self._async_probe_input(
            host, port, user_input[CONF_MODEL_PROFILE]
        )
        if model is None:
            return None, errors
        return {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_MODEL_PROFILE: model.key,
        }, errors

    async def _async_probe_input(
        self, host: str, port: int, model_key: str
    ) -> tuple[RotelModel | None, dict[str, Any], dict[str, str]]:
        """Probe a device and translate the failure into a form error.

        The single place where a probe failure becomes an error key, so the
        user, reauth and reconfigure steps cannot drift apart.
        """
        try:
            model, info = await async_validate_input(host, port, model_key)
        except CannotConnect as err:
            LOGGER.debug("Rotel: cannot connect to %s: %s", host, err)
            return None, {}, {"base": "cannot_connect"}
        except DeviceClosed as err:
            LOGGER.debug("Rotel: %s:%s closed the connection: %s", host, port, err)
            return None, {}, {"base": "device_closed"}
        except NoResponse as err:
            LOGGER.debug("Rotel: %s:%s does not speak Rotel: %s", host, port, err)
            return None, {}, {"base": "no_response"}
        return model, info, {}

    # --- helpers ---------------------------------------------------------

    def _entry_from_context(self) -> ConfigEntry:
        """Return the entry the flow was started for."""
        entry_id: str = self.context["entry_id"]
        if (entry := self.hass.config_entries.async_get_entry(entry_id)) is None:
            raise ValueError(f"Config entry {entry_id} not found")
        return entry

    @staticmethod
    def _schema_defaults(entry: ConfigEntry) -> dict[str, Any]:
        """Build the form defaults from the stored entry."""
        defaults = {
            CONF_MODEL_PROFILE: entry.data.get(
                CONF_MODEL_PROFILE, DEFAULT_MODEL_PROFILE
            ),
            CONF_HOST: entry.data.get(CONF_HOST),
            CONF_PORT: int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            CONF_NAME: entry.data.get(CONF_NAME),
        }
        return {key: value for key, value in defaults.items() if value is not None}

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
    "DeviceClosed",
    "NoResponse",
    "RotelConfigFlow",
    "async_find_rotel_port",
    "async_validate_input",
    "model_schema",
)
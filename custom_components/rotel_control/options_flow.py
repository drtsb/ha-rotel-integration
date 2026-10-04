"""Options flow for the Rotel integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow

from .const import (
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    DEFAULT_MODEL_PROFILE,
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MODEL_LABELS,
)


class RotelOptionsFlow(OptionsFlow):
    """Allow changing the polling rate and the model profile at runtime."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the options form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = {
            CONF_POLL_INTERVAL: self.config_entry.options.get(
                CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
            ),
            CONF_MODEL_PROFILE: self.config_entry.options.get(
                CONF_MODEL_PROFILE,
                self.config_entry.data.get(CONF_MODEL_PROFILE, DEFAULT_MODEL_PROFILE),
            ),
        }
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL, default=current[CONF_POLL_INTERVAL]
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
                vol.Required(
                    CONF_MODEL_PROFILE, default=current[CONF_MODEL_PROFILE]
                ): vol.In(MODEL_LABELS),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"name": self.config_entry.title},
        )


__all__ = ("RotelOptionsFlow",)

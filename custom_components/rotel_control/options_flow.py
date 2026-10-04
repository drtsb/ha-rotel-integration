"""Options flow for the Rotel integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_INPUTS,
    CONF_MODEL_PROFILE,
    CONF_POLL_INTERVAL,
    DEFAULT_MODEL_PROFILE,
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .protocol import MODEL_LABELS, get_model, selectable_inputs


def inputs_selector() -> SelectSelector:
    """Return the selector that picks the inputs a device has.

    The options are the protocol values (``coax1``), which are what the entry
    stores, while the labels the user sees come from the ``inputs``
    translation key. Storing the values keeps a selection valid when a label
    is renamed or translated.
    """
    return SelectSelector(
        SelectSelectorConfig(
            options=[item.value for item in selectable_inputs()],
            translation_key="inputs",
            multiple=True,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


class RotelOptionsFlow(OptionsFlow):
    """Allow changing the polling rate, the model and the inputs at runtime."""

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
        # The inputs of the profile in use are the suggestion: changing the
        # model offers that model's inputs instead of an empty list.
        profile = self.config_entry.options.get(
            CONF_MODEL_PROFILE, current[CONF_MODEL_PROFILE]
        )
        selected = self.config_entry.options.get(CONF_INPUTS)
        if selected is None:
            selected = [item.value for item in get_model(profile).inputs]
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
                vol.Required(CONF_INPUTS, default=list(selected)): inputs_selector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"name": self.config_entry.title},
        )


__all__ = ("RotelOptionsFlow", "inputs_selector")
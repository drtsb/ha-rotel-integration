"""Sensor platform: device information and diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RotelConfigEntry, RotelCoordinator
from .entity import RotelEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RotelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rotel sensors from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            RotelFirmwareSensor(coordinator, entry),
            RotelDeviceSensor(coordinator, entry),
            RotelProtocolSensor(coordinator, entry),
        ]
    )


ROTEL_FIRMWARE = SensorEntityDescription(
    key="firmware",
    translation_key="firmware",
)


class RotelFirmwareSensor(RotelEntity, SensorEntity):
    """Firmware version reported by the amplifier."""

    entity_description = ROTEL_FIRMWARE
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the firmware sensor."""
        super().__init__(coordinator, entry, ROTEL_FIRMWARE)

    @property
    def native_value(self) -> str | None:
        """Return the firmware version."""
        return self.data.firmware

    @property
    def available(self) -> bool:
        """Only available when the firmware reported a version."""
        return super().available and bool(self.data.firmware)


ROTEL_DEVICE = SensorEntityDescription(
    key="device_model",
    translation_key="device_model",
    entity_category=EntityCategory.DIAGNOSTIC,
)


class RotelDeviceSensor(RotelEntity, SensorEntity):
    """Rotel Base Code / model string reported by the amplifier."""

    entity_description = ROTEL_DEVICE

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the device sensor."""
        super().__init__(coordinator, entry, ROTEL_DEVICE)

    @property
    def native_value(self) -> str:
        """Return the model string of the amplifier."""
        return self.data.device_model or self.coordinator.model.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the profile in use, which is not what the device reports."""
        return {
            "profile": self.coordinator.model.key,
            "rbc": self.coordinator.model.rbc,
        }


ROTEL_PROTOCOL = SensorEntityDescription(
    key="protocol",
    translation_key="protocol",
    entity_category=EntityCategory.DIAGNOSTIC,
)


class RotelProtocolSensor(RotelEntity, SensorEntity):
    """Health of the control connection, handy for support requests."""

    entity_description = ROTEL_PROTOCOL

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the protocol sensor."""
        super().__init__(coordinator, entry, ROTEL_PROTOCOL)

    @property
    def native_value(self) -> str:
        """Return the state of the control connection."""
        return "online" if self.coordinator.last_update_success else "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details that make a bug report reproducible."""
        return {
            "host": self.coordinator.api.host,
            "port": self.coordinator.api.port,
            "connected": self.coordinator.api.connected,
            "failed_updates": self.coordinator.consecutive_failures,
            "unsupported_queries": sorted(self.data.unsupported),
        }

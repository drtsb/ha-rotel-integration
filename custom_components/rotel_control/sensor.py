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
from .protocol import frequency_is_numeric


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RotelConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Rotel sensors from a config entry."""
    coordinator: RotelCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        RotelFirmwareSensor(coordinator, entry),
        RotelDeviceSensor(coordinator, entry),
        RotelProtocolSensor(coordinator, entry),
    ]
    if coordinator.model.dac:
        entities.append(RotelSampleRateSensor(coordinator, entry))
    async_add_entities(entities)


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


ROTEL_SAMPLE_RATE = SensorEntityDescription(
    key="sample_rate",
    translation_key="sample_rate",
    icon="mdi:waveform",
    entity_category=EntityCategory.DIAGNOSTIC,
)


class RotelSampleRateSensor(RotelEntity, SensorEntity):
    """DAC sample rate the amplifier reports (``freq?``).

    The unit reports the rate the DAC is clocking at as a plain number
    (``44.1``, ``48``, ``96``, ``192``) or ``off`` when no input is clocked.
    While a rate is clocked the sensor is numeric and carries the ``kHz`` unit;
    when it reports ``off`` the sensor shows the string ``off`` instead, so the
    unit is dropped for that one state rather than forcing a number that is not
    there.
    """

    entity_description = ROTEL_SAMPLE_RATE

    def __init__(
        self,
        coordinator: RotelCoordinator,
        entry: RotelConfigEntry,
    ) -> None:
        """Initialise the sample rate sensor."""
        super().__init__(coordinator, entry, ROTEL_SAMPLE_RATE)

    @property
    def native_value(self) -> float | str | None:
        """Return the sample rate in kHz, or ``off`` when none is clocked."""
        rate = self.data.sample_rate
        if rate is None:
            return None
        if frequency_is_numeric(rate):
            return float(rate)
        return rate

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return ``kHz`` only while a real sample rate is clocked.

        A plain ``@property`` shadows the base cached property so the unit can
        follow the value: a numeric rate gets ``kHz``, the ``off`` state gets no
        unit (a non-numeric value must not claim to be numeric).
        """
        rate = self.data.sample_rate
        if rate is not None and frequency_is_numeric(rate):
            return "kHz"
        return None

    @property
    def available(self) -> bool:
        """Only available while the amplifier reports a sample rate."""
        return super().available and self.data.sample_rate is not None


__all__ = (
    "ROTEL_DEVICE",
    "ROTEL_FIRMWARE",
    "ROTEL_PROTOCOL",
    "ROTEL_SAMPLE_RATE",
    "RotelDeviceSensor",
    "RotelFirmwareSensor",
    "RotelProtocolSensor",
    "RotelSampleRateSensor",
)

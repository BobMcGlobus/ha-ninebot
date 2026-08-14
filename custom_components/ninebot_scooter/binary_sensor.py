"""Presence of a Ninebot scooter, straight from its Bluetooth advertisement.

This works on every model, including newer ones whose protocol the integration
cannot speak yet: hearing the advertisement needs no connection and no pairing.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the presence sensor."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NinebotPresence(coordinator)])


class NinebotPresence(NinebotEntity, BinarySensorEntity):
    """True while the scooter is within Bluetooth range."""

    _attr_name = "In range"
    _attr_device_class = BinarySensorDeviceClass.PRESENCE

    def __init__(self, coordinator: NinebotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_in_range"

    @property
    def available(self) -> bool:
        """Presence is known even when the scooter has never been polled."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True if the scooter is being heard right now."""
        return self.coordinator.in_range

"""Base entity for the Ninebot Scooter integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NinebotCoordinator


class NinebotEntity(CoordinatorEntity[NinebotCoordinator]):
    """Common base: device info + naming."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: NinebotCoordinator) -> None:
        super().__init__(coordinator)
        address = coordinator.address
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, address)},
            manufacturer="Segway-Ninebot",
            name=coordinator.name,
            model=coordinator.model,
            hw_version=coordinator.hw_version,
            sw_version=str(coordinator.sw_version) if coordinator.sw_version else None,
            serial_number=coordinator.serial,
        )

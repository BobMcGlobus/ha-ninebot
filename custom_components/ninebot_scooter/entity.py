"""Base entity for the Ninebot Scooter integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NinebotCoordinator


class NinebotEntity(CoordinatorEntity[NinebotCoordinator]):
    """Common base: device info + naming."""

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Stay available once we have data, and keep the last values.

        The scooter is only reachable while it is awake (a few times a day). We
        deliberately do NOT tie availability to the latest poll's success, so the
        last known values remain visible between rides instead of dropping to
        "unavailable". Freshness is exposed separately via the "Last updated"
        sensor.
        """
        return self.coordinator.data is not None

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

"""Yes/no states of a Ninebot scooter.

Presence comes straight from the Bluetooth advertisement, so it works on every
model, including newer ones whose protocol the integration cannot speak yet:
hearing the advertisement needs no connection and no pairing. The charger flags
are registers on the newer protocol.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, PROTOCOL_V2
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity, translation_key_for
from .ninebot_ble.protocol_v2 import BOARD_VCU, binary_registers


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the presence sensor and, on the newer protocol, the charger flags."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [NinebotPresence(coordinator)]
    if coordinator.protocol == PROTOCOL_V2:
        entities.extend(
            NinebotV2BinarySensor(coordinator, reg)
            for reg in binary_registers(
                coordinator.v2_board or BOARD_VCU, coordinator.v2_bms_board
            )
        )
    async_add_entities(entities)


class NinebotPresence(NinebotEntity, BinarySensorEntity):
    """True while the scooter is within Bluetooth range."""

    _attr_translation_key = "in_range"
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


class NinebotV2BinarySensor(NinebotEntity, BinarySensorEntity, RestoreEntity):
    """A one-bit register, such as "Charger connected", shown as on/off.

    They were sensors reading 1 and 0 until v0.18.4. The unique id is the one
    the sensor had; the old sensor entry is removed on setup.
    """

    def __init__(self, coordinator: NinebotCoordinator, reg) -> None:
        super().__init__(coordinator)
        self._reg = reg
        self._attr_translation_key = translation_key_for(reg.key)
        self._attr_device_class = (
            BinarySensorDeviceClass(reg.device_class) if reg.device_class else None
        )
        self._attr_unique_id = f"{coordinator.address}_v2_{reg.key}"
        self._restored: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Keep the last state across a restart, like the register sensors."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._restored = last.state == STATE_ON

    @property
    def is_on(self) -> bool | None:
        current = (self.coordinator.data or {}).get(self._reg.key)
        return bool(current) if current is not None else self._restored

    @property
    def available(self) -> bool:
        return self.is_on is not None

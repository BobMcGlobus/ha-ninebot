"""Writable speed limit for a Ninebot scooter.

Exposes the scooter's normal-mode speed limit register. Note that on a G30D this
register was accepted (the scooter acknowledges the write and reports the new
value) but did **not** change the speed actually ridden - the model's top speed is
enforced elsewhere. It is kept as an opt-in entity, disabled by default: it writes
to a speed limiter, and raising a limit beyond the speed a model is homologated
for voids its type approval and insurance cover in most countries.
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfSpeed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_SPEED_LIMIT, MIN_SPEED_LIMIT
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import CtrlIdx

_LOGGER = logging.getLogger(__name__)

_REGISTER = CtrlIdx.NB_CTL_NOMALSPEED

# Raw units per km/h for this register (confirmed on a G30D: a 20 km/h limit is
# stored as 20000). The register description scales reads by the same factor, so
# values here are plain km/h.
_RAW_PER_KMH = 1000

# Refuse to write if the scooter reports something we cannot read as a speed -
# writing a misinterpreted value to a speed limiter is what must not happen.
_PLAUSIBLE = (5.0, 35.0)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot number entities."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NinebotMaxSpeedNumber(coordinator)])


class NinebotMaxSpeedNumber(NinebotEntity, NumberEntity):
    """Normal-mode speed limit, writable."""

    _attr_name = "Normal mode speed limit"
    _attr_icon = "mdi:speedometer"
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_native_min_value = MIN_SPEED_LIMIT
    _attr_native_max_value = MAX_SPEED_LIMIT
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    # Opt-in: see the module docstring.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: NinebotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_max_speed"

    @property
    def native_value(self) -> float | None:
        """Current normal-mode speed limit in km/h."""
        value = (self.coordinator.data or {}).get(str(_REGISTER))
        return None if value is None else round(float(value), 1)

    async def async_set_native_value(self, value: float) -> None:
        """Change the normal-mode speed limit."""
        current = self.native_value
        if current is None:
            raise HomeAssistantError(
                "The scooter's speed limit has not been read yet - wake it and "
                "wait for an update before changing the limit"
            )
        if not _PLAUSIBLE[0] <= current <= _PLAUSIBLE[1]:
            raise HomeAssistantError(
                f"Cannot interpret this scooter's speed limit register (reads "
                f"{current} km/h). Refusing to write to it. Please report this "
                "value so the model can be supported properly."
            )

        raw = int(round(value * _RAW_PER_KMH))
        _LOGGER.debug("Setting normal mode speed limit to %s km/h (raw %d)", value, raw)
        readback = await self.coordinator.async_write_and_verify(_REGISTER, raw)

        if readback is None or abs(float(readback) - value) > 0.6:
            raise HomeAssistantError(
                f"Scooter did not accept the new speed limit (it now reports "
                f"{readback} km/h). The limit was not changed as requested."
            )

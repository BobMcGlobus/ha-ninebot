"""Temporary top-speed override for a Ninebot scooter.

The scooter stores its normal-mode speed limit in a control register. Writing to
it raises the cap until the scooter is powered off, which is what the various
"unlock" apps do. This is exposed as an opt-in entity, disabled by default:
raising the limit beyond the model's homologated speed voids the type approval
(and with it the insurance cover) in most countries, so it is not something to
enable by accident.
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

# How the register encodes km/h differs between firmwares: some report tenths
# (20.0 km/h reads as 20.0 after scaling), others thousandths (the same limit
# reads as 2000.0). Rather than assume, derive the factor from the value the
# scooter currently reports, and refuse to write if it matches neither - writing a
# misinterpreted value to a speed limiter is exactly what must not happen.
_ENCODINGS: tuple[tuple[float, float, int], ...] = (
    # (plausible low, plausible high, raw value per km/h)
    # read_reg already divides the raw register by 10, so a firmware storing
    # tenths reads as ~20, one storing thousandths reads as ~2000.
    (5.0, 35.0, 10),
    (500.0, 3500.0, 1000),
)


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

    _attr_name = "Max speed override"
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
    def _raw_reading(self) -> float | None:
        value = (self.coordinator.data or {}).get(str(_REGISTER))
        return None if value is None else float(value)

    @staticmethod
    def _encoding_for(reading: float) -> tuple[float, int] | None:
        """Return (km/h, factor) for a reading, or None if it makes no sense."""
        for low, high, factor in _ENCODINGS:
            if low <= reading <= high:
                return reading / (factor / 10), factor
        return None

    @property
    def native_value(self) -> float | None:
        """Current speed limit in km/h."""
        reading = self._raw_reading
        if reading is None:
            return None
        resolved = self._encoding_for(reading)
        return None if resolved is None else round(resolved[0], 1)

    async def async_set_native_value(self, value: float) -> None:
        """Raise or lower the speed limit until the scooter is powered off."""
        reading = self._raw_reading
        if reading is None:
            raise HomeAssistantError(
                "The scooter's speed limit has not been read yet - wake it and "
                "wait for an update before changing the limit"
            )

        resolved = self._encoding_for(reading)
        if resolved is None:
            raise HomeAssistantError(
                f"Cannot interpret this scooter's speed limit register (reads "
                f"{reading}). Refusing to write to it. Please report this value "
                "so the model can be supported properly."
            )
        _, factor = resolved

        raw = int(round(value * factor))
        _LOGGER.debug("Setting speed limit to %s km/h (raw %d)", value, raw)
        readback = await self.coordinator.async_write_and_verify(_REGISTER, raw)

        confirmed = self._encoding_for(float(readback)) if readback is not None else None
        if confirmed is None or abs(confirmed[0] - value) > 0.6:
            raise HomeAssistantError(
                f"Scooter did not accept the new speed limit (it now reports "
                f"{readback}). The limit was not changed as requested."
            )

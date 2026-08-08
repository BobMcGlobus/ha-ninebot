"""Lock control for a Ninebot scooter.

The lock state lives as a single bit in a packed status word, so locking is a
read-modify-write of that word: the other flags in it must survive untouched.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NinebotCoordinator
from .entity import NinebotEntity
from .ninebot_ble import CtrlIdx

_LOGGER = logging.getLogger(__name__)

# Bit 1 of the boolean status word, matching NB_INF_BOOL_LOCK.
_STATUS_REGISTER = CtrlIdx.NB_INF_BOOL_LOCK
_LOCK_BIT = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Ninebot lock."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NinebotLock(coordinator)])


class NinebotLock(NinebotEntity, LockEntity):
    """The scooter's built-in lock."""

    _attr_name = None  # the lock *is* the device

    def __init__(self, coordinator: NinebotCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_lock"

    @property
    def is_locked(self) -> bool | None:
        """Return whether the scooter is locked."""
        value = (self.coordinator.data or {}).get(str(_STATUS_REGISTER))
        return None if value is None else bool(value)

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the scooter."""
        await self._async_set(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the scooter."""
        await self._async_set(False)

    async def _async_set(self, locked: bool) -> None:
        ok = await self.coordinator.async_set_status_bit(
            _STATUS_REGISTER, _LOCK_BIT, locked
        )
        if not ok:
            raise HomeAssistantError(
                f"Scooter did not accept {'locking' if locked else 'unlocking'}. "
                "Some firmwares only allow this from the official app."
            )

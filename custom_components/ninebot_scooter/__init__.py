"""The Ninebot Scooter integration."""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .coordinator import NinebotCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Ninebot scooter from a config entry."""
    coordinator = NinebotCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Passive presence: only connect when the scooter is actually advertising.
    entry.async_on_unload(coordinator.async_start_bluetooth())

    # If it is already in range, kick off a first read in the background so device
    # info and entity values populate quickly. Never block setup on it (the first
    # ever pairing may wait for a power-button press).
    if bluetooth.async_address_present(hass, entry.unique_id, connectable=True):  # type: ignore[arg-type]
        entry.async_create_background_task(
            hass, coordinator.async_refresh(), "ninebot_scooter initial poll"
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. poll interval)."""
    await hass.config_entries.async_reload(entry.entry_id)

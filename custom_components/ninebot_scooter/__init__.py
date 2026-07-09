"""The Ninebot Scooter integration."""
from __future__ import annotations

import logging
import secrets

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, DOMAIN, PLATFORMS
from .coordinator import NinebotCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Ninebot scooter from a config entry."""
    # Persist a stable pairing key so the power-button pairing is only needed
    # once, not on every restart (a new random key would force re-pairing).
    app_key_hex = entry.data.get(CONF_APP_KEY)
    if not app_key_hex:
        app_key_hex = secrets.token_bytes(16).hex()
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_APP_KEY: app_key_hex}
        )

    coordinator = NinebotCoordinator(hass, entry, bytes.fromhex(app_key_hex))
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

"""The Ninebot Scooter integration."""
from __future__ import annotations

import logging
import secrets

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_APP_KEY, DOMAIN, PLATFORMS, PROTOCOL_V2
from .services import async_setup_services
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

    _async_drop_stale_entities(hass, entry, coordinator)

    async_setup_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _async_drop_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: NinebotCoordinator
) -> None:
    """Remove entities a vehicle on the newer protocol can never fill.

    Several things leave dead registry entries behind, and listing them turned
    out to be the wrong way round: a vehicle that starts on the classic protocol
    and is then found to speak the newer one keeps its classic entities; the
    controls are classic-only; the newer protocol's keys were renamed in v0.12.0
    so the pre-rename ones linger; and registers dropped over time - "Scooter
    power" - leave entries behind that no table mentions any more. A list of
    what to delete missed three of those four.

    So this states what may EXIST instead. On a vehicle speaking the newer
    protocol that is the "v2_" register entities and the four that come from the
    Bluetooth advertisement, which work on any model. Anything else in this
    entry is left over from a layout we no longer produce.
    """
    if coordinator.protocol != PROTOCOL_V2:
        return

    address = entry.unique_id
    # These four are built by sensor.py and binary_sensor.py regardless of
    # protocol. Add to this set when adding an entity that is not a register.
    keep = {f"{address}_{suffix}" for suffix in ("in_range", "rssi", "last_seen", "last_updated")}

    registry = er.async_get(hass)
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = reg_entry.unique_id
        if unique_id in keep or unique_id.startswith(f"{address}_v2_"):
            continue
        registry.async_remove(reg_entry.entity_id)
        removed += 1
    if removed:
        _LOGGER.info(
            "Removed %d entities from an older layout; %s speaks the newer "
            "protocol and would never have filled them",
            removed,
            entry.title,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (e.g. poll interval).

    Only on options. The coordinator also writes entry *data* mid-poll - the
    discovered board, the protocol, the pairing password - and every one of
    those fired this listener too, reloading the entry and cancelling the poll
    that was still running. On a first pairing that was destructive: the vehicle
    had already stored the new password, and the write that would have saved our
    copy came later in the same poll and never ran.
    """
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and coordinator.loaded_options == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)

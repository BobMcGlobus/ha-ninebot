"""Diagnostics support for the Ninebot Scooter integration.

The download from the device page is the quickest way for someone with a model we
don't support yet to send everything needed to judge whether it can work: what the
scooter advertises, which GATT services it exposes, and which registers could be
read.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APP_KEY, DOMAIN
from .coordinator import NinebotCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: NinebotCoordinator = hass.data[DOMAIN][entry.entry_id]
    address: str = entry.unique_id  # type: ignore[assignment]

    diag: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            # The pairing key is a secret and the MAC identifies the device.
            "data_keys": sorted(k for k in entry.data if k != CONF_APP_KEY),
            "options": dict(entry.options),
        },
        "device": {
            # Only the serial prefix: it identifies the series/model, which is
            # what matters for support, without exposing the full serial.
            "serial_prefix": (coordinator.serial or "")[:4] or None,
            "model": coordinator.model,
            "hw_version": coordinator.hw_version,
            "sw_version": coordinator.sw_version,
        },
        "poll": {
            "last_update_success": coordinator.last_update_success,
            "last_update_time": (
                coordinator.last_update_time.isoformat()
                if coordinator.last_update_time
                else None
            ),
            "register_count": len(coordinator.data or {}),
            "registers": coordinator.data or {},
        },
        # The GATT layout distinguishes the protocol generation: the classic
        # Nordic UART UUIDs (6e400001-b5a3-...) mean this integration's protocol
        # may apply, other variants mean a newer, unsupported scheme.
        "gatt_services": coordinator.gatt_services,
    }

    # What the scooter advertises - the key question for an unsupported model.
    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)
    if service_info is None:
        diag["bluetooth"] = {"seen": False}
    else:
        diag["bluetooth"] = {
            "seen": True,
            "name": service_info.name,
            "rssi": service_info.rssi,
            "connectable": service_info.connectable,
            "source": service_info.source,
            "manufacturer_ids": sorted(service_info.manufacturer_data),
            "manufacturer_data": {
                str(key): value.hex()
                for key, value in service_info.manufacturer_data.items()
            },
            "service_uuids": list(service_info.service_uuids),
            "service_data_uuids": sorted(service_info.service_data),
        }

    return diag

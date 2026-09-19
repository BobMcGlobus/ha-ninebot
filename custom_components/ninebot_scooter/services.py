"""Services for probing a scooter's registers.

The register tables in this integration only describe what somebody has already
worked out. Every new model has arrived as "the connection works but the values
are wrong", and answering that has so far meant Bluetooth captures, debug logs
and a round trip through the maintainer for each guess.

These two services let the owner of an unmapped scooter do that themselves: read
any address, sweep a range, change something physical, sweep again, and see which
register moved. No code, no capture.
"""
from __future__ import annotations

import struct
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import DOMAIN
from .coordinator import NinebotCoordinator

SERVICE_READ_REGISTER = "read_register"
SERVICE_SCAN_REGISTERS = "scan_registers"

ATTR_DEVICE_ID = "device_id"
ATTR_BOARD = "board"
ATTR_INDEX = "index"
ATTR_LENGTH = "length"
ATTR_START = "start"
ATTR_COUNT = "count"

# A sweep holds the Bluetooth connection for its whole duration, and the scooter
# is only awake for so long. 128 registers is already the far end of useful.
_MAX_COUNT = 128

_READ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_BOARD): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Required(ATTR_INDEX): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_LENGTH, default=2): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
    }
)

_SCAN_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_BOARD): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
        vol.Optional(ATTR_START, default=0x20): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(ATTR_COUNT, default=64): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=_MAX_COUNT)
        ),
        vol.Optional(ATTR_LENGTH, default=2): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
    }
)


def _coordinator(hass: HomeAssistant, device_id: str) -> NinebotCoordinator:
    """Resolve the device the call targets to its coordinator."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(f"No such device: {device_id}")
    for entry_id in device.config_entries:
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is not None:
            return coordinator
    raise HomeAssistantError(
        f"{device.name or device_id} is not a Ninebot scooter set up by this integration"
    )


def _interpret(raw: bytes) -> dict[str, Any]:
    """Describe one register's bytes every way that might be the right one.

    Which encoding a register uses is exactly what the caller is trying to find
    out, so guessing one and hiding the rest would defeat the point.
    """
    out: dict[str, Any] = {"hex": raw.hex().upper(), "bytes": list(raw)}
    if len(raw) >= 2:
        out["u16_le"] = struct.unpack("<H", raw[:2])[0]
        out["u16_be"] = struct.unpack(">H", raw[:2])[0]
    if len(raw) >= 4:
        out["u32_le"] = struct.unpack("<I", raw[:4])[0]
    # Only when every byte is printable. A number that happens to contain one
    # letter-shaped byte is not text, and offering it as text is how "1924.9 km"
    # was read out of a serial number in the first place.
    if raw and all(32 <= b < 127 for b in raw):
        out["ascii"] = raw.decode("ascii")
    return out


async def _async_read(call: ServiceCall) -> ServiceResponse:
    """Read a single register."""
    coordinator = _coordinator(call.hass, call.data[ATTR_DEVICE_ID])
    board = call.data[ATTR_BOARD]
    index = call.data[ATTR_INDEX]
    try:
        raw = await coordinator.async_read_raw(board, index, call.data[ATTR_LENGTH])
    except Exception as err:  # noqa: BLE001 - surfaced to the caller as-is
        raise HomeAssistantError(
            f"Reading 0x{index:02X} on board 0x{board:02X} failed: {err}"
        ) from err
    return {
        "board": f"0x{board:02X}",
        "index": f"0x{index:02X}",
        **_interpret(raw),
    }


async def _async_scan(call: ServiceCall) -> ServiceResponse:
    """Sweep a range of registers in one connection."""
    coordinator = _coordinator(call.hass, call.data[ATTR_DEVICE_ID])
    board = call.data[ATTR_BOARD]
    start = call.data[ATTR_START]
    count = call.data[ATTR_COUNT]
    try:
        found = await coordinator.async_scan(
            board, start, count, call.data[ATTR_LENGTH]
        )
    except Exception as err:  # noqa: BLE001
        raise HomeAssistantError(
            f"Scanning board 0x{board:02X} failed: {err}"
        ) from err

    # Registers that read as all-zero are almost always unpopulated, and listing
    # them buries the handful that carry something.
    nonzero = {i: r for i, r in found.items() if any(r)}
    return {
        "board": f"0x{board:02X}",
        "range": f"0x{start:02X}-0x{start + count - 1:02X}",
        "answered": len(found),
        "non_zero": len(nonzero),
        "registers": {
            f"0x{index:02X}": _interpret(raw) for index, raw in sorted(nonzero.items())
        },
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the probing services once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_READ_REGISTER):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_READ_REGISTER,
        _async_read,
        schema=_READ_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN_REGISTERS,
        _async_scan,
        schema=_SCAN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

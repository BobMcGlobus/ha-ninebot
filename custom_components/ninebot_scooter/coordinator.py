"""DataUpdateCoordinator owning the BLE lifecycle for a Ninebot scooter."""
from __future__ import annotations

import asyncio
import enum
import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable, TypeVar

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN
from .ninebot_ble import BmsIdx, CtrlIdx, NinebotClient, get_register_desc, iter_register
from .ninebot_ble.serial_parser import SerialParser

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class NinebotCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Connect-per-poll coordinator for a single scooter.

    The whole BLE session (connect -> auth -> action -> disconnect) is wrapped in
    :meth:`_with_client`, so both the periodic poll and one-off writes go through
    one place and are serialised by a single lock. Disconnecting after every
    session lets the scooter keep advertising so Home Assistant can always find it
    again for the next connection.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(seconds=interval),
        )
        self.address: str = entry.unique_id  # type: ignore[assignment]
        self._lock = asyncio.Lock()

        # Cached device metadata (read once, on the first successful poll).
        self.serial: str | None = None
        self.model: str | None = None
        self.hw_version: str | None = None
        self.sw_version: str | None = None

    async def _with_client(self, action: Callable[[NinebotClient], Awaitable[_T]]) -> _T:
        """Run ``action`` inside a fresh, authenticated BLE session."""
        async with self._lock:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                raise UpdateFailed(f"{self.address} is not in Bluetooth range")

            client = NinebotClient()
            try:
                await client.connect(ble_device)
                return await action(client)
            finally:
                await client.disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll all registers."""

        async def _read_all(client: NinebotClient) -> dict[str, Any]:
            # Device metadata: read once and cache.
            if self.serial is None:
                try:
                    raw_serial = await client.read_reg(CtrlIdx.NB_INF_SN)
                    parsed = SerialParser(raw_serial)
                    self.serial = raw_serial
                    self.model = str(parsed)
                    self.hw_version = (
                        f"Rev {parsed.product_revision}, "
                        f"{parsed.production_date.year}/{parsed.production_date.month}"
                    )
                except Exception as err:  # noqa: BLE001 - metadata is best-effort
                    _LOGGER.debug("Could not read/parse serial number: %s", err)
                try:
                    self.sw_version = await client.read_reg(CtrlIdx.NB_FW_VER)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Could not read firmware version: %s", err)

            data: dict[str, Any] = {}
            for idx in iter_register(CtrlIdx, BmsIdx):
                try:
                    val = await client.read_reg(idx)
                except Exception as err:  # noqa: BLE001 - one bad read must not fail the poll
                    _LOGGER.debug("Failed reading register %s: %s", idx, err)
                    continue
                if isinstance(val, enum.Enum):
                    val = val.name
                data[str(idx)] = val
            return data

        try:
            return await self._with_client(_read_all)
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error communicating with scooter: {err}") from err

    async def async_write_register(self, index: CtrlIdx | BmsIdx, raw_value: int) -> None:
        """Write a raw register value, then refresh so state reflects the device."""

        async def _write(client: NinebotClient) -> None:
            await client.write_reg(index, raw_value)

        await self._with_client(_write)
        await self.async_request_refresh()

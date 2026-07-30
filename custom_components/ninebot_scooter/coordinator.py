"""DataUpdateCoordinator owning the BLE lifecycle for a Ninebot scooter.

Polling is passive/advertisement-driven: Home Assistant listens for the scooter's
Bluetooth advertisement and only opens a connection when the scooter is actually
seen (awake and in range), throttled to the configured interval. There is no
periodic timer, so a sleeping scooter is never dialled.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.components.bluetooth.match import BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .ninebot_ble import BmsIdx, CtrlIdx, NinebotClient, iter_register
from .ninebot_ble.serial_parser import SerialParser

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


class NinebotCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Advertisement-triggered, connect-per-poll coordinator for one scooter.

    The whole BLE session (connect -> auth -> action -> disconnect) is wrapped in
    :meth:`_with_client`, serialised by a single lock, so both the poll and one-off
    writes go through one place. Disconnecting after every session lets the scooter
    keep advertising so it can always be found again.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, app_key: bytes) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=None,  # advertisement-driven, no periodic timer
        )
        self.address: str = entry.unique_id  # type: ignore[assignment]
        self._app_key = app_key
        self._min_interval: float = float(
            entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )
        self._lock = asyncio.Lock()
        self._last_success = 0.0  # monotonic time of last SUCCESSFUL poll
        self._poll_task: asyncio.Task | None = None

        # Wall-clock time of the last successful poll (for the "last updated" sensor).
        self.last_update_time: datetime | None = None

        # Cached device metadata (read once, on the first successful poll).
        self.serial: str | None = None
        self.model: str | None = None
        self.hw_version: str | None = None
        self.sw_version: str | None = None

    # -- Bluetooth presence wiring ------------------------------------------------

    @callback
    def async_start_bluetooth(self) -> CALLBACK_TYPE:
        """Start listening for the scooter's advertisement. Returns an unsub."""
        unsubs = [
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                BluetoothCallbackMatcher(address=self.address, connectable=False),
                BluetoothScanningMode.PASSIVE,
            ),
            bluetooth.async_track_unavailable(
                self.hass, self._async_on_unavailable, self.address, connectable=False
            ),
        ]

        @callback
        def _unsub() -> None:
            for unsub in unsubs:
                unsub()

        return _unsub

    @callback
    def _async_on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """Scooter is advertising: poll it, unless one is running or too recent."""
        # Don't overlap an in-flight poll.
        if self._poll_task is not None and not self._poll_task.done():
            return
        # Throttle by time since last SUCCESS. Failed polls are NOT throttled, so
        # the next advertisement retries immediately - important because the
        # scooter is often only awake for a short window (arriving/leaving).
        if time.monotonic() - self._last_success < self._min_interval:
            return
        self._poll_task = self.hass.async_create_task(
            self.async_refresh(), "ninebot_scooter poll", eager_start=False
        )

    @callback
    def _async_on_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Scooter is no longer heard; keep last values, just note it."""
        _LOGGER.debug("Scooter %s no longer seen over Bluetooth", self.address)

    # -- BLE session --------------------------------------------------------------

    async def _with_client(self, action: Callable[[NinebotClient], Awaitable[_T]]) -> _T:
        """Run ``action`` inside a fresh, authenticated BLE session."""
        async with self._lock:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if ble_device is None:
                raise UpdateFailed(f"{self.address} is not in Bluetooth range")

            client = NinebotClient(app_key=self._app_key)
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

            # The BMS reports a placeholder 100% for a moment after the scooter
            # wakes up, before it has measured the real state of charge. We poll
            # right when it wakes (advertisement-triggered), so re-read the charge
            # once after a short delay and prefer a non-placeholder value.
            soc_key = str(BmsIdx.BAT_REMAINING_CAP_PERCENT)
            if data.get(soc_key) == 100:
                await asyncio.sleep(2)
                for _ in range(3):
                    try:
                        retry = await client.read_reg(BmsIdx.BAT_REMAINING_CAP_PERCENT)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Battery re-read failed: %s", err)
                        break
                    if retry != 100:
                        _LOGGER.debug("Battery placeholder 100%% replaced by %s%%", retry)
                        data[soc_key] = retry
                        # Remaining capacity in mAh settles at the same time.
                        try:
                            data[str(BmsIdx.BAT_REMAINING_CAP)] = await client.read_reg(
                                BmsIdx.BAT_REMAINING_CAP
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        break
                    await asyncio.sleep(2)
            return data

        try:
            data = await self._with_client(_read_all)
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error communicating with scooter: {err}") from err

        self.last_update_time = dt_util.utcnow()
        self._last_success = time.monotonic()
        return data

    async def async_write_register(self, index: CtrlIdx | BmsIdx, raw_value: int) -> None:
        """Write a raw register value, then refresh so state reflects the device."""

        async def _write(client: NinebotClient) -> None:
            await client.write_reg(index, raw_value)

        await self._with_client(_write)
        await self.async_request_refresh()

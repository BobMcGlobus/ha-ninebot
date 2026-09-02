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

from homeassistant.components import bluetooth, persistent_notification
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

from .const import (
    CONF_POLL_INTERVAL,
    CONF_POLL_TIMEOUT,
    CONF_PROTOCOL,
    CONF_V2_BOARD,
    CONF_V2_GENERATION,
    CONF_V2_PASSWORD,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_TIMEOUT,
    PROTOCOL_LEGACY,
    PROTOCOL_V2,
)
from .ninebot_ble import BmsIdx, CtrlIdx, NinebotClient, iter_register
from .ninebot_ble.protocol_v2 import (
    BOARD_DIS,
    BOARD_VCU,
    SERVICE_UUID as V2_SERVICE_UUID,
    NinebotV2Client,
    registers_for_board,
)
from .ninebot_ble.serial_parser import SerialParser

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Treat a signal this much stronger than at the last attempt as "it has arrived
# and parked", and poll even inside the throttle window.
_ARRIVAL_RSSI_GAIN = 12

# How long an in-flight poll must have been running before a much better
# signal is allowed to cancel it. Short enough to catch the scooter before it
# is switched off, long enough that a healthy poll finishes undisturbed.
_STALLED_POLL_SECONDS = 8.0

# The scooter is often only awake for a minute or two, and reading every register
# takes ~45 round trips - if the session ends early, whatever is read last is
# lost. Read the registers people actually care about first; the battery lives at
# the very end of the register tables and was consistently the casualty.
_PRIORITY_REGISTERS: tuple[CtrlIdx | BmsIdx, ...] = (
    BmsIdx.BAT_REMAINING_CAP_PERCENT,
    CtrlIdx.NB_INF_RID_MIL,
    CtrlIdx.NB_INF_ACTUAL_MIL,
    BmsIdx.BAT_REMAINING_CAP,
    BmsIdx.BAT_VOLTAGE_CUR,
    BmsIdx.BAT_CURRENT_CUR,
    BmsIdx.BAT_TEMP_CUR1,
    BmsIdx.BAT_HEALTHY,
    CtrlIdx.NB_CTL_WORKMODE,
    CtrlIdx.NB_CTL_KERS,
    CtrlIdx.NB_INF_PRD_RID_MIL,
    CtrlIdx.NB_INF_RUN_TIM,
    CtrlIdx.NB_INF_RID_TIM,
)


def _ordered_registers() -> list[CtrlIdx | BmsIdx]:
    """All registers, most important first."""
    rest = [idx for idx in iter_register(CtrlIdx, BmsIdx) if idx not in _PRIORITY_REGISTERS]
    return [*_PRIORITY_REGISTERS, *rest]


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
        self.protocol: str = entry.data.get(CONF_PROTOCOL, PROTOCOL_LEGACY)
        self._v2_generation: str | None = entry.data.get(CONF_V2_GENERATION)
        stored_password = entry.data.get(CONF_V2_PASSWORD)
        self._v2_password: bytes | None = (
            bytes.fromhex(stored_password) if stored_password else None
        )
        self._min_interval: float = float(
            entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )
        self._poll_timeout: float = float(
            entry.options.get(CONF_POLL_TIMEOUT, DEFAULT_POLL_TIMEOUT)
        )
        self._lock = asyncio.Lock()
        self._last_success = 0.0  # monotonic time of last SUCCESSFUL poll
        self._failures = 0  # consecutive failures, used to back off
        self._last_attempt = 0.0  # monotonic time of the last poll ATTEMPT
        self._last_attempt_rssi: int | None = None  # signal at the last poll
        self._v2_board: int | None = entry.data.get(CONF_V2_BOARD)
        self._button_notification_id = f"ninebot_pair_{entry.entry_id}"
        self._poll_task: asyncio.Task | None = None

        # Wall-clock time of the last successful poll (for the "last updated" sensor).
        self.last_update_time: datetime | None = None

        # Presence, straight from the advertisement. This works on every model,
        # including ones whose protocol we cannot speak yet.
        self.in_range = False
        self.rssi: int | None = None
        self.last_seen: datetime | None = None

        # GATT layout seen on the last connection attempt (for diagnostics).
        self.gatt_services: dict[str, list[str]] = {}
        # Why the last attempt failed, kept for diagnostics.
        self.last_error: str | None = None

        # Cached device metadata (read once, on the first successful poll).
        self.serial: str | None = None
        self.model: str | None = None
        self.hw_version: str | None = None
        self.sw_version: str | None = None

    # -- Bluetooth presence wiring ------------------------------------------------

    @callback
    def async_start_bluetooth(self) -> CALLBACK_TYPE:
        """Start listening for the scooter's advertisement. Returns an unsub."""
        # Seed presence from what Home Assistant has already heard, so the state
        # is right immediately instead of after the next advertisement.
        self.in_range = bluetooth.async_address_present(
            self.hass, self.address, connectable=False
        )
        if service_info := bluetooth.async_last_service_info(
            self.hass, self.address, connectable=False
        ):
            self.rssi = service_info.rssi

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
        self.in_range = True
        self.rssi = service_info.rssi
        self.last_seen = dt_util.utcnow()
        self.async_update_listeners()

        # A big jump in signal means the scooter has come closer and stopped -
        # riding past a proxy and then parking next to one looks exactly like
        # this, and it is the best moment to read it. So it beats both the
        # throttle and an attempt still limping along from further away.
        arrived = (
            self._last_attempt_rssi is not None
            and service_info.rssi - self._last_attempt_rssi >= _ARRIVAL_RSSI_GAIN
        )

        running = self._poll_task is not None and not self._poll_task.done()
        if running:
            stalled = time.monotonic() - self._last_attempt >= _STALLED_POLL_SECONDS
            if not (arrived and stalled):
                return
            # Started while the scooter was out of reach and still has not
            # finished; the signal we have now is far better than the one it is
            # struggling with. Drop it and read from where we actually are.
            _LOGGER.debug(
                "Cancelling stalled poll of %s: signal improved %d -> %d dBm",
                self.address,
                self._last_attempt_rssi,
                service_info.rssi,
            )
            self._poll_task.cancel()

        # Throttle by time since the last SUCCESS, so a good poll is not repeated
        # needlessly. After failures back off from the last ATTEMPT instead:
        # measuring the backoff from a success that may be hours old left the
        # gate permanently open and hammered the adapter.
        if self._failures:
            interval = min(self._min_interval * 2**self._failures, self._min_interval * 20)
            since = time.monotonic() - self._last_attempt
        else:
            interval = self._min_interval
            since = time.monotonic() - self._last_success
        if since < interval and not arrived:
            return

        self._last_attempt = time.monotonic()
        self._last_attempt_rssi = service_info.rssi
        self._poll_task = self.hass.async_create_task(
            self._run_poll(), "ninebot_scooter poll", eager_start=False
        )

    async def _run_poll(self) -> None:
        """Refresh, but never hold the poll slot indefinitely."""
        try:
            async with asyncio.timeout(self._poll_timeout):
                await self.async_refresh()
        except asyncio.CancelledError:
            # Superseded by a closer sighting, not a fault of the vehicle.
            raise
        except TimeoutError:
            self._failures += 1
            self.last_error = (
                f"Poll gave up after {self._poll_timeout:.0f}s - either the "
                "scooter went out of range, or it needs longer than this to "
                "answer and the timeout should be raised under Configure"
            )
            _LOGGER.warning(
                "Poll of %s timed out after %.0fs. If this repeats while the "
                "scooter is parked and in range, raise the poll timeout.",
                self.address,
                self._poll_timeout,
            )

    @callback
    def _async_on_unavailable(self, service_info: BluetoothServiceInfoBleak) -> None:
        """Scooter is no longer heard; keep last values, just note it."""
        _LOGGER.debug("Scooter %s no longer seen over Bluetooth", self.address)
        self.in_range = False
        self.rssi = None
        # Failures while it was riding out of range say nothing about the next
        # time it turns up. Clear the backoff so arriving home polls at once.
        self._failures = 0
        self._last_attempt_rssi = None
        self.async_update_listeners()

    # -- BLE session --------------------------------------------------------------

    async def _with_client(self, action: Callable[[NinebotClient], Awaitable[_T]]) -> _T:
        """Run ``action`` inside a fresh, authenticated legacy BLE session."""
        async with self._lock:
            ble_device = self._ble_device()
            client = NinebotClient(app_key=self._app_key)
            try:
                await client.connect(ble_device)
                return await action(client)
            finally:
                # Keep the GATT layout even when the connection failed - it is the
                # most useful clue when someone reports an unsupported model.
                if client.gatt_services:
                    self.gatt_services = client.gatt_services
                await client.disconnect()

    def _ble_device(self):
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            # A firmware update can change the Bluetooth address, and some
            # firmwares rotate it for privacy. The advertised name is the serial
            # number, so fall back to that rather than going blind.
            for info in bluetooth.async_discovered_service_info(self.hass, True):
                if info.name and info.name == self.name:
                    _LOGGER.info(
                        "%s now advertises from %s; using that address",
                        self.name,
                        info.address,
                    )
                    return info.device
        if ble_device is None:
            raise UpdateFailed(f"{self.address} is not in Bluetooth range")
        return ble_device

    async def _with_v2_client(
        self, action: Callable[[NinebotV2Client], Awaitable[_T]]
    ) -> _T:
        """Run ``action`` inside a fresh Encryption2 session (newer vehicles)."""
        async with self._lock:
            ble_device = self._ble_device()
            client = NinebotV2Client(
                password=self._v2_password, generation=self._v2_generation
            )
            try:
                await client.connect(
                    ble_device, on_wait_for_button=self._async_ask_for_button
                )
                self._dismiss_button_prompt()
                result = await action(client)
            finally:
                if client.gatt_services:
                    self.gatt_services = client.gatt_services
                await client.disconnect()

            updates: dict[str, Any] = {}
            if client.password and client.password != self._v2_password:
                self._v2_password = client.password
                updates[CONF_V2_PASSWORD] = client.password.hex()
            # Remember the generation so later connections skip the sweep - it is
            # slow, and holding the adapter that long exhausts proxy slots.
            if client.generation and client.generation != self._v2_generation:
                self._v2_generation = client.generation
                updates[CONF_V2_GENERATION] = client.generation
            if updates:
                self._persist(updates)
            if client.serial and not self.serial:
                self.serial = client.serial
            return result

    @callback
    def _async_ask_for_button(self) -> None:
        """Ask the user to confirm pairing, visibly rather than only in the log.

        The vehicle will not register us until its power button is pressed, and
        the window is short - a log line nobody is watching is no use.
        """
        _LOGGER.warning("Waiting for the power button to be pressed on %s", self.name)
        persistent_notification.async_create(
            self.hass,
            (
                f"**{self.name}** is waiting to be paired.\n\n"
                "Press the **power button on the scooter once, now** to confirm. "
                "Home Assistant will keep asking for about a minute.\n\n"
                "This is only needed once."
            ),
            title="Ninebot: confirm pairing on the scooter",
            notification_id=self._button_notification_id,
        )

    @callback
    def _dismiss_button_prompt(self) -> None:
        persistent_notification.async_dismiss(self.hass, self._button_notification_id)

    @callback
    def _persist(self, updates: dict[str, Any]) -> None:
        """Store values in the config entry so they survive a restart."""
        entry = self.config_entry
        if entry is None:
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, **updates}
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the vehicle, using whichever protocol it speaks."""
        try:
            if self.protocol == PROTOCOL_V2:
                data = await self._with_v2_client(self._read_all_v2)
            else:
                data = await self._legacy_poll()
        except UpdateFailed as err:
            self.last_error = str(err)
            self._failures += 1
            raise
        except Exception as err:  # noqa: BLE001
            self.last_error = str(err)
            self._failures += 1
            raise UpdateFailed(f"Error communicating with scooter: {err}") from err

        self._failures = 0
        self.last_error = None
        self.last_update_time = dt_util.utcnow()
        self._last_success = time.monotonic()
        return data

    async def _legacy_poll(self) -> dict[str, Any]:
        """Poll over the legacy protocol, switching if the vehicle is a newer one."""
        try:
            return await self._with_client(self._read_all_legacy)
        except Exception:
            # A newer vehicle advertises the classic service but never answers on
            # it. If we saw its own service during the attempt, switch protocol
            # and let the reload rebuild the entities to match.
            if any(
                uuid.lower() == V2_SERVICE_UUID for uuid in self.gatt_services
            ) and self.protocol != PROTOCOL_V2:
                _LOGGER.info(
                    "%s speaks the newer Ninebot protocol; switching", self.address
                )
                self.protocol = PROTOCOL_V2
                self._persist({CONF_PROTOCOL: PROTOCOL_V2})
                return await self._with_v2_client(self._read_all_v2)
            raise

    @property
    def v2_board(self) -> int | None:
        """The board this vehicle answers register reads on, once discovered."""
        return self._v2_board

    async def _find_v2_board(self, client: NinebotV2Client) -> int:
        """Work out which board answers register reads on this vehicle.

        Kick scooters keep vehicle data on the VCU, the E-series mopeds on the
        dashboard, and the module layout differs per model - so ask rather than
        assume.
        """
        if self._v2_board is not None:
            return self._v2_board
        # Probe each board with a register from its own table. Probing both with
        # one index proves nothing: a VCU answers a dashboard index quite happily,
        # it just returns something else entirely.
        for board in (BOARD_VCU, BOARD_DIS):
            probe = registers_for_board(board)[0]
            try:
                raw = await client.read_register(board, probe.index, probe.length)
            except Exception:  # noqa: BLE001 - probing, failure is expected
                continue
            if len(raw) < probe.length or not any(raw):
                # An all-zero read means the index is not populated on this board.
                continue
            _LOGGER.info("Vehicle answers register reads on board 0x%02X", board)
            self._v2_board = board
            self._persist({CONF_V2_BOARD: board})
            return board
        # Nothing answered; keep the documented default so the poll still reports
        # a useful failure rather than silently doing nothing.
        return BOARD_VCU

    async def _read_all_v2(self, client: NinebotV2Client) -> dict[str, Any]:
        """Read the documented registers of a newer vehicle."""
        if client.serial:
            self.serial = client.serial
            self.model = self.model or "Ninebot (newer protocol)"

        board = await self._find_v2_board(client)
        registers = registers_for_board(board)
        data: dict[str, Any] = {}
        failed: list[str] = []
        for reg in registers:
            try:
                raw = await client.read_register(board, reg.index, reg.length)
            except Exception as err:  # noqa: BLE001 - one bad read must not fail the poll
                _LOGGER.debug("Failed reading %s: %s", reg.key, err)
                failed.append(reg.key)
                continue
            if len(raw) < reg.length:
                failed.append(reg.key)
                continue
            value = reg.unpack(raw)
            data[reg.key] = round(value * reg.scale, 3) if reg.scale != 1.0 else value

        if not data:
            raise UpdateFailed(
                f"Connected but no register could be read ({len(failed)} attempted)"
            )
        if failed:
            _LOGGER.warning(
                "Read %d/%d registers; %d failed: %s",
                len(data),
                len(data) + len(failed),
                len(failed),
                ", ".join(failed),
            )
        return data

    async def _read_all_legacy(self, client: NinebotClient) -> dict[str, Any]:
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
            failed: list[str] = []
            for idx in _ordered_registers():
                try:
                    val = await client.read_reg(idx)
                except Exception as err:  # noqa: BLE001 - one bad read must not fail the poll
                    _LOGGER.debug("Failed reading register %s: %s", idx, err)
                    failed.append(str(idx))
                    continue
                if isinstance(val, enum.Enum):
                    val = val.name
                data[str(idx)] = val

                # The BMS reports a placeholder 100% for a moment after the scooter
                # wakes, before it has measured the real charge - and we poll exactly
                # at wake-up. Re-read straight away (the session is healthiest here)
                # and prefer a non-placeholder value.
                if idx is BmsIdx.BAT_REMAINING_CAP_PERCENT and val == 100:
                    for _ in range(3):
                        await asyncio.sleep(2)
                        try:
                            retry = await client.read_reg(BmsIdx.BAT_REMAINING_CAP_PERCENT)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug("Battery re-read failed: %s", err)
                            break
                        if retry != 100:
                            _LOGGER.debug(
                                "Battery placeholder 100%% replaced by %s%%", retry
                            )
                            data[str(idx)] = retry
                            break

            if not data:
                # Nothing came back at all: the session is not usable. Fail the
                # poll instead of reporting success with an empty result, which
                # would advance "Last updated" and make stale values look fresh.
                raise UpdateFailed(
                    f"Connected but no register could be read ({len(failed)} attempted)"
                )

            if failed:
                # Surface partial polls: a silently skipped register otherwise looks
                # identical to an unchanged value, because entities fall back to the
                # last known reading.
                _LOGGER.warning(
                    "Read %d/%d registers; %d failed: %s",
                    len(data),
                    len(data) + len(failed),
                    len(failed),
                    ", ".join(failed),
                )

            return data

    async def async_capture_gatt(self) -> str | None:
        """Connect once purely to record the GATT layout, for diagnostics.

        Returns the error message if the attempt failed, else None. Used when a
        model has never connected successfully - the failure reason and the
        services it does expose are exactly what's needed to judge new hardware.
        """

        async def _noop(client: Any) -> None:
            return None

        try:
            if self.protocol == PROTOCOL_V2:
                await self._with_v2_client(_noop)
            else:
                await self._with_client(_noop)
        except Exception as err:  # noqa: BLE001 - diagnostics must never raise
            return str(err)
        return None

    async def async_write_and_verify(
        self, index: CtrlIdx | BmsIdx, raw_value: int
    ) -> Any:
        """Write a register and read it straight back.

        Returns the value read back (scaled, as read_reg returns it) so the caller
        can confirm the scooter actually accepted the change. Only the touched
        register is re-read - a full refresh would be ~45 round trips.
        """

        async def _write_read(client: NinebotClient) -> Any:
            await client.write_reg(index, raw_value)
            await asyncio.sleep(0.5)
            return await client.read_reg(index)

        result = await self._with_client(_write_read)

        if self.data is not None:
            updated = dict(self.data)
            updated[str(index)] = (
                result.name if isinstance(result, enum.Enum) else result
            )
            self.async_set_updated_data(updated)
        return result

    async def async_set_status_bit(
        self, index: CtrlIdx | BmsIdx, bit: int, value: bool
    ) -> bool:
        """Flip a single bit of a packed status word, preserving the others.

        Returns whether the scooter reports the bit as requested afterwards.
        """

        async def _rmw(client: NinebotClient) -> bool:
            raw = await client.read_reg_bytes(index)
            word = (raw[1] << 8) | raw[0]
            updated = word | (1 << bit) if value else word & ~(1 << bit)
            _LOGGER.debug(
                "Status word %s: 0x%04X -> 0x%04X (bit %d = %s)",
                index,
                word,
                updated,
                bit,
                value,
            )
            if updated != word:
                await client.write_reg(index, updated)
                await asyncio.sleep(0.5)
            check = await client.read_reg_bytes(index)
            return bool(((check[1] << 8) | check[0]) & (1 << bit)) is value

        ok = await self._with_client(_rmw)
        await self.async_request_refresh()
        return ok

    async def async_write_register(self, index: CtrlIdx | BmsIdx, raw_value: int) -> None:
        """Write a raw register value, then refresh so state reflects the device."""

        async def _write(client: NinebotClient) -> None:
            await client.write_reg(index, raw_value)

        await self._with_client(_write)
        await self.async_request_refresh()

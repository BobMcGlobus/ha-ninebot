"""Encryption2 client for newer Segway-Ninebot vehicles (Max G3, G2, E-series).

Protocol documentation: https://codeberg.org/NootNooot/segway-ninebot-ble (MIT).

These vehicles expose their own GATT service and speak an AES-encrypted protocol
behind a three-phase handshake. Confusingly they usually still advertise the
classic Nordic UART service used by older models, but never answer on it.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import struct
from dataclasses import dataclass
from typing import Any, Callable

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from .crypto_v2 import NbCryptoV2

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "6e400001-0000-0000-006e-696e65626f74"
WRITE_UUID = "6e400002-0000-0000-006e-696e65626f74"
NOTIFY_UUID = "6e400004-0000-0000-006e-696e65626f74"

SYNC = b"\x5a\xa5"
BT_ID = 0x3E

# Boards
BOARD_DIS = 0x01  # dashboard; stays awake and proxies values from sleeping boards
BOARD_BLE = 0x04

# Commands
CMD_READ = 0x01
CMD_READ_RESP = 0x04
CMD_PRE_COMM = 0x5B
CMD_SET_PWD = 0x5C
CMD_AUTH = 0x5D

# Writes must be split at MTU - 3; 20 bytes is the safe BLE 4.0 default.
_CHUNK = 20
_CHUNK_DELAY = 0.01


@dataclass(frozen=True)
class Frame:
    """A decoded plaintext frame."""

    target: int
    command: int
    index: int
    payload: bytes


def build_frame(target: int, command: int, index: int, payload: bytes = b"") -> bytes:
    """Assemble a plaintext Encryption2 frame."""
    return SYNC + bytes([len(payload), BT_ID, target, command, index]) + payload


def parse_frame(plaintext: bytes) -> Frame | None:
    """Decode a plaintext frame, or None if it is malformed."""
    if len(plaintext) < 7 or plaintext[:2] != SYNC:
        return None
    length = plaintext[2]
    return Frame(
        target=plaintext[4],
        command=plaintext[5],
        index=plaintext[6],
        payload=plaintext[7 : 7 + length],
    )


class NinebotV2Client:
    """Talks Encryption2 to a single vehicle."""

    def __init__(self, password: bytes | None = None) -> None:
        self.crypto = NbCryptoV2()
        self.client: BleakClient | None = None
        self.gatt_services: dict[str, list[str]] = {}
        # Session password: reused across connections so the pairing button press
        # is only needed once.
        self.password: bytes | None = password
        self.serial: str | None = None
        self._auth_param: bytes = b""
        self._rx_buffer = bytearray()
        self._queue: asyncio.Queue[Frame] = asyncio.Queue(32)
        # Set once the write characteristic's properties are known.
        self._write_with_response = False

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    # -- connection ------------------------------------------------------------

    async def connect(
        self,
        device: BLEDevice,
        pair_timeout: float = 60.0,
        on_wait_for_button: Callable[[], None] | None = None,
    ) -> None:
        """Connect and run the three-phase handshake."""
        name = device.name or ""
        _LOGGER.debug("Connecting to %s (%s)", name, device.address)
        self.client = await establish_connection(BleakClient, device, device.address)

        self.gatt_services = {
            str(service.uuid): [str(char.uuid) for char in service.characteristics]
            for service in self.client.services
        }
        if SERVICE_UUID not in {uuid.lower() for uuid in self.gatt_services}:
            raise TimeoutError(
                "Vehicle does not expose the Ninebot service - it does not speak "
                f"this protocol. Services seen: {sorted(self.gatt_services)}"
            )

        # Not every module accepts write-without-response; using the wrong write
        # type means the vehicle silently never receives the frame.
        write_char = self.client.services.get_characteristic(WRITE_UUID)
        properties = list(write_char.properties) if write_char else []
        self._write_with_response = "write-without-response" not in properties
        _LOGGER.debug(
            "Write characteristic properties: %s (MTU %s)",
            properties,
            getattr(self.client, "mtu_size", "unknown"),
        )

        await self.client.start_notify(NOTIFY_UUID, self._on_notify)
        # Newer firmwares may flush cached notifications from a previous session;
        # let them arrive and be dropped before the handshake starts.
        await asyncio.sleep(0.2)
        self._drain()

        await self._handshake(name, pair_timeout, on_wait_for_button)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:  # noqa: BLE001 - best effort on teardown
                pass
            await self.client.disconnect()
        self.client = None

    # -- handshake -------------------------------------------------------------

    async def _handshake(
        self,
        name: str,
        pair_timeout: float,
        on_wait_for_button: Callable[[], None] | None,
    ) -> None:
        # Phase 1 - PRE_COMM: ask for the challenge and serial, keyed on the
        # advertised device name, in non-SN mode.
        #
        # Which static block feeds the keystream here depends on the device
        # generation, and getting it wrong is invisible: the vehicle simply cannot
        # decrypt the frame and never answers. Try both rather than assume.
        response = None
        for gen2 in (False, True):
            self.crypto = NbCryptoV2(gen2=gen2)
            self.crypto.reset_sn()
            self.crypto.set_key(name.encode(), None)
            _LOGGER.debug("PRE_COMM attempt with %s keystream", "Gen2" if gen2 else "Gen3")
            for attempt in range(3):
                try:
                    response = await self._request(
                        BOARD_BLE, CMD_PRE_COMM, 0, b"", expect=CMD_PRE_COMM, timeout=2.5
                    )
                    break
                except TimeoutError:
                    _LOGGER.debug("PRE_COMM attempt %d timed out", attempt + 1)
            if response is not None:
                _LOGGER.debug("PRE_COMM answered with the %s keystream", "Gen2" if gen2 else "Gen3")
                break

        if response is None:
            raise TimeoutError(
                "Vehicle never answered the PRE_COMM handshake (tried both key "
                "variants). It may need to be woken up, or it uses a variant of "
                "the protocol that is not supported yet"
            )

        if len(response.payload) < 30:
            raise TimeoutError(
                f"Short PRE_COMM response ({len(response.payload)} bytes) - "
                "cannot read the authentication parameters"
            )

        self._auth_param = response.payload[0:16]
        self.serial = response.payload[16:30].decode(errors="replace").strip("\x00")
        has_stored_password = response.index != 0
        _LOGGER.debug(
            "PRE_COMM ok, serial %s, device reports stored password: %s",
            self.serial,
            has_stored_password,
        )

        self.crypto.set_auth(self._auth_param)
        self.crypto.start_sn()

        # Phase 2 - SET_PWD, unless we can reuse a password from a previous
        # session (which avoids asking the user for a button press again).
        if not (self.password and has_stored_password):
            self.crypto.set_key(name.encode(), self._auth_param)
            await self._set_password(pair_timeout, on_wait_for_button)

        # Phase 3 - AUTH, keyed on the session password.
        assert self.password is not None
        self.crypto.set_key(self.password, self._auth_param)
        auth = await self._request(
            BOARD_BLE,
            CMD_AUTH,
            0,
            (self.serial or "").encode().ljust(14, b"\x00")[:14],
            expect=CMD_AUTH,
            timeout=4,
        )
        if auth.index != 1:
            self.password = None  # force a fresh pairing next time
            raise TimeoutError(
                "Vehicle rejected authentication - the stored pairing is no longer "
                "valid, reconnect and confirm pairing on the vehicle"
            )
        _LOGGER.debug("Authenticated successfully")

    async def _set_password(
        self, pair_timeout: float, on_wait_for_button: Callable[[], None] | None
    ) -> None:
        """Register a session password, waiting for the user to confirm."""
        # The vehicle simply stores whatever we send, so a strong random value is
        # both simpler and safer than reproducing the app's weak PRNG.
        password = secrets.token_bytes(16)
        deadline = asyncio.get_running_loop().time() + pair_timeout
        notified = False

        while True:
            response = await self._request(
                BOARD_BLE, CMD_SET_PWD, 0, password, expect=CMD_SET_PWD, timeout=4
            )
            if response.index == 1:
                self.password = password
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    "Pairing not confirmed on the vehicle - press its power button "
                    "while Home Assistant is connecting"
                )
            if not notified and on_wait_for_button is not None:
                on_wait_for_button()
                notified = True
            await asyncio.sleep(2)

    # -- register access -------------------------------------------------------

    async def read_register(self, board: int, index: int, length: int) -> bytes:
        """Read ``length`` bytes from a register."""
        response = await self._request(
            board, CMD_READ, index, bytes([length]), expect=CMD_READ_RESP
        )
        return response.payload[:length]

    # -- framing ---------------------------------------------------------------

    async def _request(
        self,
        target: int,
        command: int,
        index: int,
        payload: bytes,
        *,
        expect: int,
        timeout: float = 3.0,
    ) -> Frame:
        """Send a frame and wait for the matching reply."""
        self._drain()
        await self._send(build_frame(target, command, index, payload))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                frame = await asyncio.wait_for(
                    self._queue.get(), timeout=max(0.1, deadline - loop.time())
                )
            except (TimeoutError, asyncio.TimeoutError):
                break
            if frame.command == expect:
                return frame
            _LOGGER.debug("Ignoring unexpected frame: %s", frame)

        raise TimeoutError(
            f"No response to command 0x{command:02X} index 0x{index:02X}"
        )

    async def _send(self, plaintext: bytes) -> None:
        assert self.client is not None, "Must be connected first"
        data = self.crypto.encrypt(plaintext)
        _LOGGER.debug(
            "TX plain %s -> enc %s (%d bytes, %s)",
            plaintext.hex().upper(),
            data.hex().upper(),
            len(data),
            "with response" if self._write_with_response else "no response",
        )
        for offset in range(0, len(data), _CHUNK):
            chunk = data[offset : offset + _CHUNK]
            await self.client.write_gatt_char(
                WRITE_UUID, chunk, response=self._write_with_response
            )
            if offset + _CHUNK < len(data):
                await asyncio.sleep(_CHUNK_DELAY)

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _on_notify(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        """Reassemble notifications into whole frames and decrypt them."""
        _LOGGER.debug("RX raw %s (%d bytes)", bytes(data).hex().upper(), len(data))
        self._rx_buffer += data

        while True:
            start = self._rx_buffer.find(SYNC)
            if start < 0:
                # Keep only a trailing partial sync byte.
                self._rx_buffer = self._rx_buffer[-1:]
                return
            if start:
                del self._rx_buffer[:start]
            if len(self._rx_buffer) < 3:
                return

            # Encrypted frame = 7 header/plaintext bytes + payload + 6 byte tail.
            total = self._rx_buffer[2] + 13
            if len(self._rx_buffer) < total:
                return

            raw = bytes(self._rx_buffer[:total])
            del self._rx_buffer[:total]

            plaintext, status = self.crypto.decrypt(raw)
            if status != 0:
                _LOGGER.debug("Dropping frame, decrypt status %d", status)
                continue
            frame = parse_frame(plaintext)
            if frame is None:
                _LOGGER.debug("Dropping malformed frame")
                continue
            if self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(frame)


# --- Register map -----------------------------------------------------------
# Read from the dashboard board, which stays awake and caches values from boards
# that are asleep.


def _u16(data: bytes) -> int:
    return struct.unpack("<H", data[:2])[0]


def _u32(data: bytes) -> int:
    return struct.unpack("<I", data[:4])[0]


@dataclass(frozen=True, kw_only=True)
class V2Register:
    """One readable value on a newer vehicle."""

    key: str
    board: int
    index: int
    length: int
    unpack: Callable[[bytes], Any]
    scale: float = 1.0
    unit: str | None = None
    device_class: str | None = None
    primary: bool = False


V2_REGISTERS: tuple[V2Register, ...] = (
    V2Register(
        key="Battery",
        board=BOARD_DIS,
        index=0xB5,
        length=2,
        unpack=_u16,
        unit="%",
        device_class="battery",
        primary=True,
    ),
    V2Register(
        key="Total mileage",
        board=BOARD_DIS,
        index=0xB7,
        length=4,
        unpack=_u32,
        scale=0.001,
        unit="km",
        device_class="distance",
        primary=True,
    ),
    V2Register(
        key="Remaining range",
        board=BOARD_DIS,
        index=0x25,
        length=2,
        unpack=_u16,
        scale=0.1,
        unit="km",
        device_class="distance",
        primary=True,
    ),
    V2Register(
        key="Trip mileage",
        board=BOARD_DIS,
        index=0xB9,
        length=4,
        unpack=_u32,
        scale=0.001,
        unit="km",
        device_class="distance",
    ),
    V2Register(
        key="Current speed",
        board=BOARD_DIS,
        index=0x26,
        length=2,
        unpack=_u16,
        scale=0.1,
        unit="km/h",
    ),
    V2Register(
        key="Average speed",
        board=BOARD_DIS,
        index=0x27,
        length=2,
        unpack=_u16,
        scale=0.1,
        unit="km/h",
    ),
    V2Register(
        key="Rated max speed",
        board=BOARD_DIS,
        index=0x48,
        length=2,
        unpack=_u16,
        scale=0.1,
        unit="km/h",
    ),
    V2Register(
        key="Riding mode",
        board=BOARD_DIS,
        index=0x74,
        length=2,
        unpack=_u16,
    ),
)

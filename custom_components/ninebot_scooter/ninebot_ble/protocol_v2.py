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

from .crypto_v2 import FW_DATA, NbCryptoV2

_LOGGER = logging.getLogger(__name__)

SERVICE_UUID = "6e400001-0000-0000-006e-696e65626f74"
WRITE_UUID = "6e400002-0000-0000-006e-696e65626f74"
NOTIFY_UUID = "6e400004-0000-0000-006e-696e65626f74"
RCTP_WRITE_UUID = "6e400003-0000-0000-006e-696e65626f74"
NORDIC_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
# Standard GATT Device Name: the vehicle's serial, which is the encryption key.
GATT_DEVICE_NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"

NORDIC_WRITE_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_NOTIFY_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

SYNC1 = 0x5A
BT_ID = 0x3E

# The two generations differ in more than the keystream: the second sync byte and
# the preferred GATT service change too. Sending a frame with the wrong sync byte
# is invisible - the vehicle's parser never recognises a frame and stays silent.
GEN2 = "gen2"  # 0x5AA5, fw_data keystream, Nordic UART - E125S and older
GEN3 = "gen3"  # 0x5AB5, zero keystream, Ninebot Custom - newer models

GENERATIONS: dict[str, dict[str, Any]] = {
    GEN3: {
        "sync2": 0xB5,
        "gen2_keystream": False,
        "write": WRITE_UUID,
        "notify": NOTIFY_UUID,
    },
    GEN2: {
        "sync2": 0xA5,
        "gen2_keystream": True,
        "write": NORDIC_WRITE_UUID,
        "notify": NORDIC_NOTIFY_UUID,
    },
}
VALID_SYNC2 = {0xA5, 0xB5}

# Boards
BOARD_DIS = 0x01  # dashboard on E-series; proxies values from sleeping boards
BOARD_VCU = 0x16  # vehicle control unit; where kick scooters keep their data
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


def build_frame(
    target: int, command: int, index: int, payload: bytes = b"", sync2: int = 0xA5
) -> bytes:
    """Assemble a plaintext frame for the given generation."""
    return bytes([SYNC1, sync2, len(payload), BT_ID, target, command, index]) + payload


def parse_frame(plaintext: bytes) -> Frame | None:
    """Decode a plaintext reply, or None if it is malformed.

    Replies carry the source board where requests carry the protocol id, so the
    two fields are swapped compared to :func:`build_frame`.
    """
    if len(plaintext) < 7 or plaintext[0] != SYNC1 or plaintext[1] not in VALID_SYNC2:
        return None
    length = plaintext[2]
    return Frame(
        target=plaintext[3],
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
        # Discovered on connect; the working combination is found by trying.
        self._write_with_response = False
        self._write_uuid = WRITE_UUID
        self._sync2 = 0xA5
        self.generation: str | None = None
        self._write_chars: list[str] = []
        self._notify_chars: list[str] = []

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

        # Subscribe to every characteristic that can notify, not just the one the
        # documentation names: if a model answers on a different channel we would
        # otherwise see complete silence and have no way to tell.
        self._notify_chars = []
        self._write_chars = []
        for service in self.client.services:
            if str(service.uuid).lower() not in (SERVICE_UUID, NORDIC_SERVICE_UUID):
                continue
            for char in service.characteristics:
                properties = set(char.properties)
                if properties & {"notify", "indicate"}:
                    try:
                        await self.client.start_notify(char, self._on_notify)
                        self._notify_chars.append(str(char.uuid))
                    except Exception as err:  # noqa: BLE001 - best effort per channel
                        _LOGGER.debug("Could not subscribe to %s: %s", char.uuid, err)
                if properties & {"write", "write-without-response"}:
                    self._write_chars.append(str(char.uuid).lower())
                if str(char.uuid).lower() == WRITE_UUID:
                    _LOGGER.debug(
                        "Write characteristic properties: %s (MTU %s)",
                        sorted(properties),
                        getattr(self.client, "mtu_size", "unknown"),
                    )
        _LOGGER.debug("Listening on: %s", self._notify_chars)
        if not self._notify_chars:
            raise TimeoutError("Vehicle exposes no notification channel to listen on")
        # Newer firmwares may flush cached notifications from a previous session;
        # let them arrive and be dropped before the handshake starts.
        await asyncio.sleep(0.2)
        self._drain()

        # The name is the encryption key, and a wrong one is rejected silently.
        # Prefer the name the vehicle reports over GATT: the advertised name can
        # be shortened or stale.
        try:
            raw_name = await self.client.read_gatt_char(GATT_DEVICE_NAME_UUID)
            gatt_name = raw_name.decode("ascii", errors="ignore").replace("\x00", "").strip()
            if gatt_name and gatt_name != name:
                _LOGGER.debug("Using GATT device name %r instead of %r", gatt_name, name)
                name = gatt_name
        except Exception as err:  # noqa: BLE001 - advisory, the advert name may do
            _LOGGER.debug("Could not read the GATT device name: %s", err)

        await self._handshake(name, pair_timeout, on_wait_for_button)

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            for uuid in self._notify_chars:
                try:
                    await self.client.stop_notify(uuid)
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
        # A vehicle answers only when the sync byte, the keystream and the GATT
        # channel all match its generation. Every wrong combination fails the same
        # silent way, so try the real ones rather than mixing them.
        response = None
        for gen in (GEN3, GEN2):
            spec = GENERATIONS[gen]
            if spec["write"] not in self._write_chars:
                _LOGGER.debug("Skipping %s: no %s characteristic", gen, spec["write"][:8])
                continue
            for with_response in (False, True):
                self._sync2 = spec["sync2"]
                self._write_uuid = spec["write"]
                self._write_with_response = with_response
                self.crypto = NbCryptoV2(gen2=spec["gen2_keystream"])
                self.crypto.reset_sn()
                # On Gen2 the constant is both the static keystream block and the
                # second half of the key material; Gen3 uses zeros for both.
                self.crypto.set_key(
                    name.encode(), FW_DATA if spec["gen2_keystream"] else None
                )
                _LOGGER.debug(
                    "PRE_COMM try: %s (sync 5A%02X, write %s, %s)",
                    gen,
                    spec["sync2"],
                    spec["write"][:8],
                    "with response" if with_response else "no response",
                )
                try:
                    response = await self._request(
                        BOARD_BLE, CMD_PRE_COMM, 0, b"", expect=CMD_PRE_COMM, timeout=2.5
                    )
                except TimeoutError:
                    continue
                except Exception as err:  # noqa: BLE001 - a rejected write is informative
                    _LOGGER.debug("Write failed on this combination: %s", err)
                    continue
                _LOGGER.info("Handshake accepted by the vehicle as %s", gen)
                self.generation = gen
                break
            if response is not None:
                break

        if response is None:
            raise TimeoutError(
                "Vehicle never answered the handshake on any protocol generation "
                "(tried 5AA5 over Nordic UART and 5AB5 over the Ninebot service, "
                "with and without write acknowledgement). Make sure it is switched "
                "on and that the official Segway-Ninebot app is not connected"
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
        await self._send(self._frame(target, command, index, payload))

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

    def _frame(self, target: int, command: int, index: int, payload: bytes = b"") -> bytes:
        return build_frame(target, command, index, payload, sync2=self._sync2)

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
                self._write_uuid, chunk, response=self._write_with_response
            )
            if offset + _CHUNK < len(data):
                await asyncio.sleep(_CHUNK_DELAY)

    def _drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _on_notify(
        self, sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Reassemble notifications into whole frames and decrypt them."""
        _LOGGER.debug(
            "RX on %s: %s (%d bytes)",
            getattr(sender, "uuid", "?"),
            bytes(data).hex().upper(),
            len(data),
        )
        self._rx_buffer += data

        while True:
            start = -1
            for candidate in (bytes([SYNC1, 0xA5]), bytes([SYNC1, 0xB5])):
                found = self._rx_buffer.find(candidate)
                if found >= 0 and (start < 0 or found < start):
                    start = found
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

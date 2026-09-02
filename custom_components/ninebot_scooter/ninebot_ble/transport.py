from __future__ import annotations

import asyncio
import enum
import logging
import secrets
import time
from binascii import hexlify
from struct import pack
from typing import Any

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection, retry_bluetooth_connection_error
from ._miauth.nbcrypto import NbCrypto

from .register import BmsIdx, CtrlIdx, get_register_desc

_LOGGER = logging.getLogger(__name__)

NORDIC_UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Newer Segway-Ninebot models (Max G3, E-series ...) expose their own service -
# the tail is ASCII for "ninebot" - and speak a different, encrypted protocol on
# it. They often still advertise the classic Nordic UART service above, but never
# answer on it, so its presence alone means nothing.
NINEBOT_V2_SERVICE_UUID = "6e400001-0000-0000-006e-696e65626f74"


class Command(enum.Enum):
    READ = 0x01
    """Read control table data."""
    WRITE = 0x02
    """Write control table data, with reply."""
    WRITE_ACK_NO_REPLY = 0x03
    """Write control table data, without reply."""
    READ_ACK = 0x04
    """Response packet to instruction reading."""
    WRITE_ACK = 0x05
    """Response packet to instruction writing."""
    INIT = 0x5B
    PING = 0x5C
    PAIR = 0x5D


class DeviceId(enum.Enum):
    ES_CONTROL = 0x20
    """Master control of electric scooter (ES)"""
    ES_BLE = 0x21
    """Bluetooth instrument of ES"""
    ES_BATT = 0x22
    """Built-in battery of ES"""
    PC = 0x3D
    """PC upper computer connected through serial port/CAN debugger/IoT equipment"""
    PHONE = 0x3E
    """Mobile phone linked through Bluetooth serial port (BLE)"""


class Packet:
    MAGIC = [0x5A, 0xA5]
    """All packets sent to scooter must start with this preamble."""

    def __init__(
        self,
        source: DeviceId,
        target: DeviceId,
        command: Command,
        data_index: int,
        data: list[int] | bytes | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.command = command
        self.data_index = data_index
        self.data_segment = list(data) if data else []

    def pack(self) -> bytearray:
        payload = pack(
            "BBBBB", len(self.data_segment), self.source.value, self.target.value, self.command.value, self.data_index
        ) + bytes(self.data_segment)
        return bytearray(bytes(self.MAGIC) + payload)

    @staticmethod
    def unpack(data: bytearray) -> Packet | None:
        if len(data) < 7 or list(data[:2]) != Packet.MAGIC:
            return None
        segment_len = data[2]
        if len(data) < 7 + segment_len:
            return None
        return Packet(DeviceId(data[3]), DeviceId(data[4]), Command(data[5]), data[6], list(data[7:]))

    def __str__(self) -> str:
        ds = ""
        if len(self.data_segment) > 0:
            ds = ", data=" + hexlify(bytes(self.data_segment)).upper().decode()
        return (
            f"Packet[{self.source.name} -> {self.target.name},"
            f" cmd={self.command.name}, idx={self.data_index:02X}{ds}]"
        )


class NinebotClient:
    def __init__(self, app_key: bytes | None = None) -> None:
        # The app key is registered on the scooter during pairing. Reuse a
        # persisted key across sessions so pairing (a power-button press) is only
        # needed once; fall back to a random key if none is supplied.
        self.app_key = app_key if app_key is not None else secrets.token_bytes(16)
        self.crypto = NbCrypto()
        self.receive_queue: asyncio.Queue[Packet] = asyncio.Queue(100)
        self.receive_buffer = bytearray()
        self.client: BleakClient | None = None
        # Populated on connect: {service_uuid: [characteristic_uuid, ...]}. Useful
        # for diagnosing models we don't support yet, which use different UUIDs.
        self.gatt_services: dict[str, list[str]] = {}

    async def connect(self, device: BLEDevice, pair_timeout: float = 45.0) -> None:
        """Connect and handshake the scooter.

        This function must be called before any other. ``pair_timeout`` bounds how
        long the initial pairing waits for the user to press the power button.
        """
        self.crypto.set_name(device.name.encode() if device.name else b"Unnamed")

        _LOGGER.info("Connecting to %s (%s): ...", device.name, device.address)
        self.client = await establish_connection(BleakClient, device, device.address)

        # Record the GATT layout before touching any characteristic, so that an
        # unsupported model still reports what it offers instead of only failing.
        self.gatt_services = {
            str(service.uuid): [str(char.uuid) for char in service.characteristics]
            for service in self.client.services
        }
        if NORDIC_UART_TX_UUID not in [
            uuid for uuids in self.gatt_services.values() for uuid in uuids
        ]:
            raise TimeoutError(
                "Scooter does not expose the expected Nordic UART service - this "
                f"model is probably not supported yet. Services seen: {self.gatt_services}"
            )

        await self.client.start_notify(NORDIC_UART_TX_UUID, self._read_callback)

        _LOGGER.debug("Authenticating ...")

        # Init
        try:
            resp = await self.request(Packet(DeviceId.PC, DeviceId.ES_BLE, Command.INIT, 0))
        except TimeoutError:
            # Silence on the very first packet, on a model that also offers the
            # newer Ninebot service, means we are talking the wrong protocol
            # entirely rather than having a connection problem.
            if any(
                uuid.lower() == NINEBOT_V2_SERVICE_UUID for uuid in self.gatt_services
            ):
                raise TimeoutError(
                    "This scooter uses Segway-Ninebot's newer BLE protocol "
                    f"(service {NINEBOT_V2_SERVICE_UUID}), which this integration "
                    "does not support yet. Models such as the Max G3 are affected: "
                    "they still advertise the classic service but never answer on it"
                ) from None
            raise
        received_key = resp.data_segment[:16]
        received_serial = resp.data_segment[16:]

        _LOGGER.debug("> BLE Key: %s", hexlify(bytes(received_key)).upper().decode())
        _LOGGER.debug("> Serial: %s", bytes(received_serial).decode())
        self.crypto.set_ble_data(received_key)

        # Ping
        resp = await self.request(Packet(DeviceId.PC, DeviceId.ES_BLE, Command.PING, 0, self.app_key))
        if resp.data_index == 0:
            # Zero (0) indicates we are not paired yet. Loop for a bounded time
            # waiting for the user to confirm pairing with the power button.
            paired = False
            deadline = time.time() + pair_timeout
            while time.time() < deadline:
                await asyncio.sleep(1.0)
                # Sending pair request here seem to pair the device. Unclear why.
                await self.send(Packet(DeviceId.PC, DeviceId.ES_BLE, Command.PAIR, 0, received_serial))
                try:
                    resp = await self.receive()
                except TimeoutError:
                    pass
                if resp.command == Command.PING and resp.data_index == 1:
                    self.crypto.set_app_data(self.app_key)
                    paired = True
                    break
                if resp.command == Command.PAIR and resp.data_index == 1:
                    paired = True
                    break
                # If we get here, the button on the scooter need to be pressed.
                _LOGGER.info("Please press power button on scooter!")
            if not paired:
                raise TimeoutError(
                    "Pairing not confirmed. On models that pair this way, press "
                    "the scooter's power button once while setup is waiting. If a "
                    "short press only toggles the headlight, this model probably "
                    "does not use button pairing at all and speaks the newer "
                    "protocol instead"
                )

        # Final PAIR handshake. Best-effort: some firmwares don't acknowledge a
        # redundant PAIR when the scooter is already registered from a previous
        # session, so a missing response here must not fail the connection - the
        # encrypted session is already established for reads.
        try:
            await self.request(
                Packet(DeviceId.PC, DeviceId.ES_BLE, Command.PAIR, 0, received_serial),
                timeout=3,
            )
        except TimeoutError:
            _LOGGER.debug("Final PAIR not acknowledged; continuing (likely already paired)")

        # Settle on the session key. Which derivation the scooter expects depends
        # on its pairing state, and picking wrong fails *silently*: the scooter
        # simply cannot decrypt our requests and never answers, so every register
        # read times out. Probe with a cheap read and fall back to the other
        # derivation rather than guessing.
        if not await self._session_works():
            _LOGGER.debug("Session key rejected; retrying with the app-data key")
            self.crypto.set_app_data(self.app_key)
            if not await self._session_works():
                _LOGGER.debug("App-data key rejected too; retrying with the BLE-data key")
                self.crypto.set_ble_data(received_key)
                if not await self._session_works():
                    raise TimeoutError(
                        "Scooter never answered an encrypted read. Either it needs "
                        "re-pairing (remove and re-add the integration, then press "
                        "the power button), or this model uses a newer protocol "
                        "that is not supported yet"
                    )

        _LOGGER.debug("Connected and authenticated successfully!")

    async def _session_works(self) -> bool:
        """Cheap probe read to verify the encrypted session is understood."""
        try:
            await self.request(
                Packet(DeviceId.PC, DeviceId.ES_CONTROL, Command.READ, 0x1A, [2]),
                timeout=3,
            )
        except TimeoutError:
            return False
        return True

    async def disconnect(self) -> None:
        if self.client and self.client.is_connected:
            await self.client.stop_notify(NORDIC_UART_TX_UUID)
            await self.client.disconnect()
            self.client = None

    @retry_bluetooth_connection_error()
    async def send(self, packet: Packet) -> None:
        """Send a BLE-UART packet to scooter."""
        assert self.client is not None, "Must be connected first."
        _LOGGER.debug("Sending %s", packet)
        msg = self.crypto.encrypt(packet.pack())
        msg_len = len(msg)
        byte_idx = 0
        while msg_len > 0:
            tmp_len = msg_len if msg_len <= 20 else 20
            buf = msg[byte_idx : byte_idx + tmp_len]
            _LOGGER.debug("Sending chuck %d/%d: %s", byte_idx + tmp_len, len(msg), hexlify(buf).upper().decode())
            await self.client.write_gatt_char(NORDIC_UART_RX_UUID, buf)
            msg_len -= tmp_len
            byte_idx += tmp_len

    @property
    def is_connected(self) -> bool:
        """Returns True if scooter is connected, otherwise False."""
        return self.client is not None and self.client.is_connected

    async def receive(self, timeout: float = 1) -> Packet:
        """Receive one BLE-UART packet from scooter."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.receive_queue.empty():
                await asyncio.sleep(0.1)
                continue
            return await self.receive_queue.get()
        raise TimeoutError("Timeout receiving packet")

    async def request(self, request: Packet, timeout: float = 5) -> Packet:
        """Sends request and returns matching response.

        Helper that combines send() and receive(). This function only works for some types of
        messages (e.g. register and symmetric send/receive packets).
        """
        command_replies = {Command.READ: Command.READ_ACK}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                await self.send(request)

                while time.time() < deadline:
                    recv_packet = await self.receive()
                    if (
                        recv_packet.source == request.target
                        and recv_packet.target == request.source
                        and recv_packet.command == command_replies.get(request.command, request.command)
                        and (request.command.value > 0x5 or recv_packet.data_index == request.data_index)
                    ):
                        return recv_packet
                raise TimeoutError(f"Timeout waiting for response for: {request}")
            except TimeoutError:
                _LOGGER.debug("Retrying request ...")
        raise TimeoutError(f"Did not get a response on {request}")

    async def read_reg_bytes(self, index: CtrlIdx | BmsIdx) -> list[int]:
        """Read a register's raw bytes, before any unpacking or scaling.

        Needed to modify single bits of a packed status word without disturbing
        the other flags in it.
        """
        target = DeviceId.ES_CONTROL if isinstance(index, CtrlIdx) else DeviceId.ES_BATT
        reg = get_register_desc(index)

        data: list[int] = []
        for i in range(reg.index_len):
            resp = await self.request(
                Packet(DeviceId.PC, target, Command.READ, reg.index_start + i, [reg.read_len])
            )
            data.extend(resp.data_segment)
        return data

    async def read_reg(self, index: CtrlIdx | BmsIdx) -> Any:
        """Read scooter memory register.

        Just tell which one and this function will do the rest.
        """
        reg = get_register_desc(index)
        data = await self.read_reg_bytes(index)

        unpacked = reg.unpacker(data)
        if reg.scaler:
            unpacked = reg.scaler(unpacked)
        return unpacked

    async def write_reg(self, index: CtrlIdx | BmsIdx, value: int) -> None:
        """Write a 16-bit little-endian raw value to a control-table register.

        ``value`` is the raw register value (i.e. already scaled to the device's
        integer representation, NOT the human-facing unit). Uses the
        "write, no reply" command (0x03); confirm the effect by re-reading the
        register afterwards.

        NOTE: the write path is community-derived and not verified across all
        models/firmwares. Callers should treat writes as best-effort and read
        back to confirm.
        """
        if isinstance(index, CtrlIdx):
            target = DeviceId.ES_CONTROL
        else:
            target = DeviceId.ES_BATT

        reg = get_register_desc(index)
        payload = [value & 0xFF, (value >> 8) & 0xFF]
        _LOGGER.debug("Writing register %s (0x%02X) = %d", index, reg.index_start, value)
        await self.send(Packet(DeviceId.PC, target, Command.WRITE_ACK_NO_REPLY, reg.index_start, payload))

    async def _read_callback(self, _: BleakGATTCharacteristic, data: bytearray) -> None:
        if list(data[:2]) == Packet.MAGIC:
            self.receive_buffer = data
        else:
            self.receive_buffer += data

        decrypted = self.crypto.decrypt(self.receive_buffer)
        total_len = self.receive_buffer[2] + 7
        _LOGGER.debug(f"Decrypted {len(decrypted)}/{total_len}: {hexlify(decrypted).upper().decode()}")
        if len(decrypted) == total_len:
            packet = Packet.unpack(decrypted)
            if packet is None:
                _LOGGER.warning("Failed to decode received packet")
            else:
                await self.receive_queue.put(packet)
        elif len(decrypted) >= total_len:
            self.receive_buffer = bytearray()
            _LOGGER.warning(
                "Malformed packet received, expected packet size %d bytes, received %d bytes",
                total_len,
                len(decrypted),
            )

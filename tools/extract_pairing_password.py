#!/usr/bin/env python3
"""Recover a Segway-Ninebot pairing password from an Android HCI snoop log.

Newer vehicles refuse to register a second client once the official app has
paired with them. The way in is to reuse the password the app already uses - and
that password is recoverable from a capture of the app pairing, because the key
protecting it is derived from the vehicle's advertised name and a challenge the
vehicle itself sends in the clear moments earlier.

Usage:
    python3 tools/extract_pairing_password.py btsnoop_hci.log [--name SERIAL]

The capture must contain a *fresh pairing* (remove the vehicle in the app, then
add it again), not merely a reconnect - a reconnect never transmits the password.

Every decrypted frame is printed as well, which is also the easiest way to learn
which boards and registers a given model actually uses.
"""
from __future__ import annotations

import argparse
import pathlib
import struct
import sys
from dataclasses import dataclass

# Load the crypto module straight from its file: importing it as part of the
# integration package would drag in Home Assistant dependencies this standalone
# tool does not need.
import importlib.util  # noqa: E402

_CRYPTO_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "ninebot_scooter"
    / "ninebot_ble"
    / "crypto_v2.py"
)
_spec = importlib.util.spec_from_file_location("nb_crypto_v2", _CRYPTO_PATH)
_crypto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_crypto)

FW_DATA = _crypto.FW_DATA
ZEROS16 = _crypto.ZEROS16
aes_ecb_block = _crypto.aes_ecb_block
build_nonce = _crypto.build_nonce
derive_key = _crypto.derive_key

SYNC1 = 0x5A
VALID_SYNC2 = (0xA5, 0xB5)
CMD_PRE_COMM = 0x5B
CMD_SET_PWD = 0x5C

HOST_TO_CONTROLLER = 0
CONTROLLER_TO_HOST = 1


@dataclass
class AttPayload:
    """One ATT write or notification, with its direction."""

    outgoing: bool
    data: bytes


def read_btsnoop(path: pathlib.Path) -> list[AttPayload]:
    """Pull ATT writes and notifications out of a btsnoop capture."""
    raw = path.read_bytes()
    if not raw.startswith(b"btsnoop\x00"):
        raise SystemExit(f"{path} is not a btsnoop capture")

    offset = 16  # identification pattern + version + datalink
    payloads: list[AttPayload] = []
    # ACL reassembly, per direction
    pending: dict[tuple[bool, int], bytearray] = {}
    expected: dict[tuple[bool, int], int] = {}

    while offset + 24 <= len(raw):
        _orig_len, incl_len, flags, _drops, _ts = struct.unpack_from(">IIIIq", raw, offset)
        offset += 24
        packet = raw[offset : offset + incl_len]
        offset += incl_len
        if len(packet) < 2:
            continue

        outgoing = (flags & 0x01) == HOST_TO_CONTROLLER
        if packet[0] != 0x02:  # H4 packet type: ACL data only
            continue
        if len(packet) < 5:
            continue

        handle_flags, acl_len = struct.unpack_from("<HH", packet, 1)
        handle = handle_flags & 0x0FFF
        pb_flag = (handle_flags >> 12) & 0x03
        body = packet[5 : 5 + acl_len]
        key = (outgoing, handle)

        if pb_flag == 0x01:  # continuation of an earlier L2CAP packet
            if key in pending:
                pending[key] += body
        else:
            if len(body) < 4:
                continue
            l2cap_len, cid = struct.unpack_from("<HH", body, 0)
            if cid != 0x0004:  # ATT
                continue
            pending[key] = bytearray(body[4:])
            expected[key] = l2cap_len

        buffered = pending.get(key)
        if buffered is None or len(buffered) < expected.get(key, 0):
            continue

        att = bytes(buffered)
        pending.pop(key, None)
        expected.pop(key, None)
        if not att:
            continue

        opcode = att[0]
        if opcode in (0x12, 0x52) and len(att) > 3:  # write request / command
            payloads.append(AttPayload(True, att[3:]))
        elif opcode in (0x1B, 0x1D) and len(att) > 3:  # notification / indication
            payloads.append(AttPayload(False, att[3:]))

    return payloads


def extract_frames(payloads: list[AttPayload], outgoing: bool) -> list[bytes]:
    """Reassemble whole Ninebot frames from one direction's ATT payloads."""
    stream = bytearray()
    for entry in payloads:
        if entry.outgoing == outgoing:
            stream += entry.data

    frames: list[bytes] = []
    index = 0
    while index < len(stream) - 2:
        if stream[index] == SYNC1 and stream[index + 1] in VALID_SYNC2:
            total = stream[index + 2] + 13
            if index + total <= len(stream):
                frames.append(bytes(stream[index : index + total]))
                index += total
                continue
        index += 1
    return frames


def decrypt(frame: bytes, aes_key: bytes, auth: bytes, ecb_input: bytes) -> bytes | None:
    """Decrypt one captured frame; replay protection deliberately not applied."""
    if len(frame) < 9:
        return None
    header, payload = frame[:3], frame[3:-6]
    counter = struct.unpack(">H", frame[-2:])[0]

    if counter == 0:
        # Non-SN mode reuses one keystream block for every block of payload.
        keystream = aes_ecb_block(aes_key, ecb_input)
        plain = bytearray()
        for start in range(0, len(payload), 16):
            block = payload[start : start + 16]
            plain += bytes(a ^ b for a, b in zip(block, keystream))
        return header + bytes(plain)

    nonce = build_nonce(counter, auth)
    plain = bytearray()
    for number, start in enumerate(range(0, len(payload), 16), start=1):
        block = payload[start : start + 16]
        keystream = aes_ecb_block(aes_key, b"\x01" + nonce + bytes([0x00, number & 0xFF]))
        plain += bytes(a ^ b for a, b in zip(block, keystream))
    return header + bytes(plain)


def describe(frame: bytes) -> str:
    length = frame[2]
    return (
        f"board=0x{frame[3]:02X} cmd=0x{frame[5]:02X} idx=0x{frame[6]:02X} "
        f"data={frame[7 : 7 + length].hex().upper()}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument(
        "--name",
        required=True,
        help="the vehicle's advertised Bluetooth name (its serial, e.g. 1CGBC2510C1691)",
    )
    parser.add_argument("--all", action="store_true", help="print every decrypted frame")
    args = parser.parse_args()

    payloads = read_btsnoop(args.capture)
    incoming = extract_frames(payloads, outgoing=False)
    outgoing = extract_frames(payloads, outgoing=True)
    print(f"{len(payloads)} ATT payloads -> {len(outgoing)} sent, {len(incoming)} received frames")
    if not incoming:
        print("No vehicle replies found. Was Bluetooth HCI snoop enabled for the whole session?")
        return 1

    name = args.name
    for generation, ecb_input in (("gen2", FW_DATA), ("gen3", ZEROS16)):
        key = derive_key(name.encode(), FW_DATA if generation == "gen2" else None)
        for frame in incoming:
            plain = decrypt(frame, key, ZEROS16, ecb_input)
            if plain and len(plain) >= 37 and plain[5] == CMD_PRE_COMM:
                auth = plain[7:23]
                serial = plain[23:37].decode("ascii", "replace").strip("\x00")
                if serial.isprintable() and serial.strip():
                    return _recover(name, generation, auth, serial, outgoing, args.all)

    print(
        "Could not identify the handshake. Pass the vehicle's exact advertised name "
        "with --name, and make sure the capture covers a fresh pairing."
    )
    return 1


def _recover(
    name: str,
    generation: str,
    auth: bytes,
    serial: str,
    outgoing: list[bytes],
    dump_all: bool,
) -> int:
    print(f"\nVehicle:    {serial}")
    print(f"Protocol:   {generation}")
    print(f"Challenge:  {auth.hex().upper()}")

    session_key = derive_key(name.encode(), auth)
    ecb_input = FW_DATA if generation == "gen2" else ZEROS16

    password = None
    for frame in outgoing:
        plain = decrypt(frame, session_key, auth, ecb_input)
        if plain is None or len(plain) < 7:
            continue
        if dump_all:
            print("  app ->", describe(plain))
        if plain[5] == CMD_SET_PWD and plain[2] == 16:
            password = plain[7:23]

    if password is None:
        print(
            "\nNo password found. The capture probably shows a reconnect rather than "
            "a fresh pairing - remove the vehicle in the app, capture again while "
            "adding it back."
        )
        return 1

    print("\n" + "=" * 60)
    print(f"Pairing password: {password.hex().upper()}")
    print("=" * 60)
    print(
        "\nPaste this into the integration's options, in the "
        "'Pairing password' field. Treat it like a key to the vehicle."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

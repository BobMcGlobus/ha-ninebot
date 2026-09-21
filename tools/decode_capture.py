#!/usr/bin/env python3
"""Decrypt a whole Bluetooth capture of the official app talking to a scooter.

`extract_pairing_password.py` recovers the password and stops there: everything
the app sends after authenticating is encrypted under that password, so its
`--all` dump turns to noise at exactly the point the interesting traffic starts.

This tool takes the password as input and decrypts the rest. What that buys:

  * which registers the app reads, so an unmapped model can be mapped by
    watching the app instead of sweeping addresses blindly;
  * the write commands, which nothing in this project can do yet on the newer
    protocol - the only way to learn them is to see the app issue one;
  * the app's own AUTH frame in plaintext, which is the thing to compare against
    when a vehicle answers the app but ignores us.

The password never leaves your machine. Run this yourself and share the output,
not the key.

Usage:
    python3 tools/decode_capture.py capture.log --name SERIAL --password HEX
    python3 tools/decode_capture.py capture.log --name SERIAL --password HEX --all
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import struct
import sys
from collections import Counter

_HERE = pathlib.Path(__file__).resolve().parent

# Reuse the capture parser rather than keeping a second copy of the ACL and ATT
# reassembly, which is the fiddly part and has already been debugged once.
_spec = importlib.util.spec_from_file_location(
    "nb_extract", _HERE / "extract_pairing_password.py"
)
_extract = importlib.util.module_from_spec(_spec)
# Register before executing: that module defines a dataclass, and from Python
# 3.12 on @dataclass resolves its annotations through sys.modules. Loading it
# without this raises an AttributeError that says nothing about the cause.
sys.modules["nb_extract"] = _extract
_spec.loader.exec_module(_extract)

FW_DATA = _extract.FW_DATA
ZEROS16 = _extract.ZEROS16
build_nonce = _extract.build_nonce
decrypt = _extract.decrypt
derive_key = _extract.derive_key
extract_frames = _extract.extract_frames
read_btsnoop = _extract.read_btsnoop

CMD_READ = 0x01
CMD_READ_RESP = 0x04
CMD_PRE_COMM = 0x5B
CMD_SET_PWD = 0x5C
CMD_AUTH = 0x5D

# Seen on an F3: 0x02 is answered with 0x05, 0x03 is not answered at all.
CMD_WRITE_ACKED = 0x02
CMD_WRITE_ACK = 0x05
CMD_WRITE_PLAIN = 0x03

_COMMANDS = {
    CMD_READ: "READ",
    CMD_WRITE_ACKED: "WRITE",
    CMD_WRITE_PLAIN: "WRITE!",  # no reply expected
    CMD_READ_RESP: "read->",
    CMD_WRITE_ACK: "write->",
    CMD_PRE_COMM: "PRE_COMM",
    CMD_SET_PWD: "SET_PWD",
    CMD_AUTH: "AUTH",
}
_WRITES = (CMD_WRITE_ACKED, CMD_WRITE_PLAIN)

_BOARDS = {0x01: "dashboard", 0x04: "BLE", 0x07: "battery", 0x16: "VCU"}


def _board(index: int) -> str:
    name = _BOARDS.get(index)
    return f"0x{index:02X} ({name})" if name else f"0x{index:02X}"


def _describe(plain: bytes) -> str:
    """One line for a decrypted frame: who, what, where, how much."""
    length, source, target, command, index = plain[2], plain[3], plain[4], plain[5], plain[6]
    payload = plain[7 : 7 + length]
    name = _COMMANDS.get(command, f"cmd 0x{command:02X}")
    line = (
        f"{name:<9} {_board(source):<16} -> {_board(target):<16} "
        f"reg 0x{index:02X}  {payload.hex().upper()}"
    )
    if command in _WRITES and len(payload) >= 2:
        line += f"   = {struct.unpack('<H', payload[:2])[0]}"
    return line


def _endpoint(value: int) -> bool:
    """Whether a byte could be a board or the phone, rather than noise."""
    return value == 0x3E or value <= 0x30


def _try_keys(frame: bytes, keys: list[tuple[str, bytes]], auth: bytes, ecb: bytes):
    """Decrypt under whichever key produces a frame that parses."""
    for label, key in keys:
        # Pre-handshake frames are keyed before any challenge exists, so they
        # carry no auth material in their nonce.
        plain = decrypt(frame, key, ZEROS16 if label == "handshake" else auth, ecb)
        if plain is None or len(plain) < 7:
            continue
        # A wrong key yields noise. Three things have to agree before a frame
        # counts as decoded: a known command, a length matching the frame it
        # came out of, and endpoints that could exist. Command and length alone
        # still match random bytes often enough to fill a report with registers
        # that were never read.
        if (
            plain[5] in _COMMANDS
            and plain[2] + 13 == len(frame)
            and _endpoint(plain[3])
            and _endpoint(plain[4])
        ):
            return label, plain
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("--name", required=True, help="the vehicle's advertised name / serial")
    parser.add_argument(
        "--password", required=True, help="32 hex characters, from the app or the extractor"
    )
    parser.add_argument("--all", action="store_true", help="print every frame, not just a summary")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="on failure, show what the first few frames decoded to under each key",
    )
    args = parser.parse_args()

    try:
        password = bytes.fromhex(args.password.replace(" ", ""))
    except ValueError:
        print("The password must be hex.")
        return 1
    if len(password) != 16:
        print(f"The password must be 16 bytes (32 hex characters); got {len(password)}.")
        return 1

    payloads = read_btsnoop(args.capture)
    sent = extract_frames(payloads, outgoing=True)
    received = extract_frames(payloads, outgoing=False)
    print(f"{len(payloads)} ATT payloads -> {len(sent)} sent, {len(received)} received frames")
    if not sent:
        print("No frames found. Was the snoop log running for the whole session?")
        return 1

    # Phase 1 is keyed on the vehicle name, and carries the challenge that keys
    # everything after it. Find it before anything else can be read.
    name_key = derive_key(args.name.encode(), FW_DATA)
    auth = None
    for frame in received:
        plain = decrypt(frame, name_key, ZEROS16, FW_DATA)
        if plain and len(plain) >= 37 and plain[5] == CMD_PRE_COMM:
            auth = plain[7:23]
            break
    if auth is None:
        print(
            "\nNo PRE_COMM reply found, so the session key cannot be derived. The "
            "capture needs to start before the app connects."
        )
        return 1
    print(f"Challenge:  {auth.hex().upper()}")

    # Three keys are in play across one session, and which applies depends on how
    # far the handshake had got when the frame was sent:
    #   handshake - before the challenge is known, keyed on the name and fw data
    #   name      - after it, used for SET_PWD
    #   session   - after AUTH, used for everything worth reading
    keys = [
        ("session", derive_key(password, auth)),
        ("name", derive_key(args.name.encode(), auth)),
        ("handshake", derive_key(args.name.encode(), FW_DATA)),
    ]

    # Decode everything first and check how much of it worked. A wrong password
    # decodes a handful of frames by chance, and printing those as findings is
    # worse than printing nothing: they look exactly like real ones.
    decoded: list[tuple[str, bytes]] = []
    undecodable = 0
    for direction, frames in (("app ->", sent), ("  <- veh", received)):
        for frame in frames:
            _, plain = _try_keys(frame, keys, auth, FW_DATA)
            if plain is None:
                undecodable += 1
            else:
                decoded.append((direction, plain))

    total = len(sent) + len(received)
    rate = len(decoded) / total if total else 0.0
    print(f"Decoded:    {len(decoded)}/{total} frames ({rate:.0%})")
    if rate < 0.5 and args.explain:
        print("\n--- what the first frames decoded to ---------------------------")
        print("Safe to share: this is derived from the capture, not from your key.\n")
        for frame in sent[:4]:
            counter = struct.unpack(">H", frame[-2:])[0]
            print(f"  raw       {frame.hex().upper()}")
            print(f"            length byte {frame[2]}, counter {counter}")
            for label, key in keys:
                plain = decrypt(
                    frame, key, ZEROS16 if label == "handshake" else auth, FW_DATA
                )
                if plain is None:
                    print(f"    {label:<8} -> could not decrypt at all")
                    continue
                cmd = plain[5]
                why = []
                if cmd not in _COMMANDS:
                    why.append(f"command 0x{cmd:02X} unknown")
                if plain[2] + 13 != len(frame):
                    why.append(f"length says {plain[2]} but frame is {len(frame)}")
                if not _endpoint(plain[3]) or not _endpoint(plain[4]):
                    why.append(f"endpoints 0x{plain[3]:02X}/0x{plain[4]:02X} implausible")
                verdict = "accepted" if not why else "rejected: " + ", ".join(why)
                print(f"    {label:<8} -> {plain[:8].hex().upper()}  {verdict}")
            print()

    if rate < 0.5:
        print(
            "\nToo little of this capture decoded for the results to mean anything.\n"
            "Almost always the password: it has to be the one this vehicle is "
            "currently\npaired with. If it came from the app's stored data, check it "
            "is the entry for\nthis serial, and that the app has not re-paired since.\n"
            "\nThe vehicle name matters too - it must be exactly as advertised."
        )
        if not args.explain:
            print("\nRun again with --explain to see what the first frames decoded to.")
        return 1

    reads: Counter[tuple[int, int]] = Counter()
    writes: list[tuple[int, int, bytes]] = []
    if args.all:
        print("\n--- every frame ------------------------------------------------")
    for direction, plain in decoded:
        if args.all:
            print(f"{direction} {_describe(plain)}")
        command, index = plain[5], plain[6]
        if direction.startswith("app"):
            if command == CMD_READ:
                reads[(plain[4], index)] += 1
            elif command in _WRITES:
                writes.append((plain[4], index, bytes(plain[7 : 7 + plain[2]])))

    print("\n--- registers the app READ -------------------------------------")
    if reads:
        for (board, index), count in sorted(reads.items()):
            print(f"  {_board(board):<16} reg 0x{index:02X}   {count}x")
    else:
        print("  none")

    print("\n--- registers the app WROTE ------------------------------------")
    if writes:
        for board, index, payload in writes:
            value = (
                f"   = {struct.unpack('<H', payload[:2])[0]}" if len(payload) >= 2 else ""
            )
            print(f"  {_board(board):<16} reg 0x{index:02X}   {payload.hex().upper()}{value}")
        print(
            "\n  These are the commands this project cannot issue yet on the newer "
            "protocol.\n  Please include this section when reporting."
        )
    else:
        print("  none seen - the capture may not contain a settings change")

    if undecodable:
        print(f"\n{undecodable} frames did not decode; some of that is normal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

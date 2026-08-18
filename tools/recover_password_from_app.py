#!/usr/bin/env python3
"""Recover a Segway-Ninebot pairing password from the official app's local data.

A scooter already paired with the app stores its 16-byte pairing password
*inside the vehicle*, so it is never re-sent over Bluetooth and cannot be read
from a BLE capture. But the app keeps its own copy locally, keyed by the serial
number (iOS: com.ninebot.segway.plist "<serial>_decrypt"; Android: the app's
shared_prefs / database). This recovers it from a dump of that data and, crucially,
*verifies* it against a real BLE capture so you know it is correct before use.

    python3 tools/recover_password_from_app.py APPDATA --capture btsnoop_hci.log --name SERIAL

APPDATA may be an `adb backup` .ab file, a shared_prefs .xml, a sqlite db, or any
file/blob that might contain the password. Every 16-byte value in it (raw, hex or
base64) is tried against the capture's AUTH handshake; only an exact match is
printed.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import pathlib
import re
import struct
import sys
import zlib

_HERE = pathlib.Path(__file__).resolve().parent
_CRYPTO = _HERE.parent / "custom_components" / "ninebot_scooter" / "ninebot_ble" / "crypto_v2.py"
_spec = importlib.util.spec_from_file_location("nb_crypto_v2", _CRYPTO)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

_ext_spec = importlib.util.spec_from_file_location("extractor", _HERE / "extract_pairing_password.py")
ex = importlib.util.module_from_spec(_ext_spec)
sys.modules["extractor"] = ex
_ext_spec.loader.exec_module(ex)


def build_oracle(capture: pathlib.Path, name: str):
    """From a BLE capture, return (auth_challenge, auth_plaintext, auth_ciphertext)."""
    payloads = ex.read_btsnoop(capture)
    recv = ex.extract_frames(payloads, outgoing=False)
    sent = ex.extract_frames(payloads, outgoing=True)

    key_pre = cv.derive_key(name.encode(), cv.FW_DATA)
    challenge = None
    serial = name
    for frame in recv:
        plain = ex.decrypt(frame, key_pre, cv.ZEROS16, cv.FW_DATA)
        if plain and len(plain) >= 37 and plain[5] == 0x5B:
            challenge = plain[7:23]
            serial = plain[23:37].decode("ascii", "replace").strip("\x00") or name
            break
    if challenge is None:
        raise SystemExit("No PRE_COMM handshake found in the capture - wrong file?")

    # AUTH plaintext: 5A A5 LEN 3E 04 5D 00 + serial(14)
    body = serial.encode().ljust(14, b"\x00")[:14]
    auth_plain = bytes([0x5A, 0xA5, len(body), 0x3E, 0x04, 0x5D, 0x00]) + body

    # AUTH ciphertext: the first SN-mode sent frame matching that length.
    want_len = len(auth_plain) + 6
    for frame in sent:
        if len(frame) == want_len and struct.unpack(">H", frame[-2:])[0] > 0:
            counter = struct.unpack(">H", frame[-2:])[0]
            return challenge, auth_plain, frame, counter
    raise SystemExit("No AUTH frame found in the capture - is it a real app session?")


def verify(password: bytes, challenge: bytes, auth_plain: bytes, auth_ct: bytes, counter: int) -> bool:
    """True if this password reproduces the captured AUTH frame exactly."""
    crypto = cv.NbCryptoV2(gen2=True)
    crypto.set_key(password, challenge)
    crypto.set_auth(challenge)
    crypto.start_sn()
    crypto.counter = counter - 1  # next encrypt() lands on `counter`
    return crypto.encrypt(auth_plain) == auth_ct


def candidate_blobs(raw: bytes):
    """Yield everything that might contain the stored value: raw + unpacked layers."""
    yield raw
    # adb backup: 24-byte header then zlib stream
    if raw[:15] == b"ANDROID BACKUP\n":
        parts = raw.split(b"\n", 4)
        if len(parts) == 5:
            try:
                yield zlib.decompress(parts[4])
            except Exception:  # noqa: BLE001
                pass


def iter_candidates(blob: bytes):
    """Yield 16-byte password candidates: raw windows, hex tokens, base64 tokens."""
    seen: set[bytes] = set()

    def offer(value: bytes):
        if len(value) == 16 and value not in seen:
            seen.add(value)
            return value
        return None

    # hex tokens (…_decrypt values are often hex or base64)
    for match in re.findall(rb"[0-9A-Fa-f]{32}", blob):
        v = offer(bytes.fromhex(match.decode()))
        if v:
            yield v
    # base64 tokens decoding to 16 bytes
    for match in re.findall(rb"[A-Za-z0-9+/]{22,24}={0,2}", blob):
        try:
            dec = base64.b64decode(match + b"==")
        except Exception:  # noqa: BLE001
            continue
        v = offer(dec[:16]) if len(dec) >= 16 else None
        if v:
            yield v
    # every raw 16-byte window (certain, slower)
    for i in range(len(blob) - 15):
        v = offer(blob[i : i + 16])
        if v:
            yield v


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("appdata", type=pathlib.Path, help="app-data dump (.ab, .xml, .db, …)")
    parser.add_argument("--capture", type=pathlib.Path, required=True, help="a BLE btsnoop capture of the same scooter")
    parser.add_argument("--name", required=True, help="the scooter's advertised name / serial")
    args = parser.parse_args()

    challenge, auth_plain, auth_ct, counter = build_oracle(args.capture, args.name)
    print(f"Oracle ready (challenge {challenge.hex().upper()}, AUTH counter {counter}).")

    raw = args.appdata.read_bytes()
    tried = 0
    for blob in candidate_blobs(raw):
        for pw in iter_candidates(blob):
            tried += 1
            if verify(pw, challenge, auth_plain, auth_ct, counter):
                print(f"\nChecked {tried} candidates.")
                print("=" * 60)
                print(f"Pairing password: {pw.hex().upper()}")
                print("=" * 60)
                print("\nPaste into the integration's 'Pairing password' option.")
                return 0

    print(f"\nChecked {tried} candidates - none matched.")
    print("The password is not in this dump. On Android it usually needs root or a")
    print("full app-data backup; make sure the scooter is currently paired in the app.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

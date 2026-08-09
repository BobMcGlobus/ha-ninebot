"""Encryption2 crypto used by newer Segway-Ninebot vehicles.

AES-128 in a custom CTR-like mode with CBC-MAC authentication, as documented by
the segway-ninebot-ble project (MIT, see the top-level NOTICE). Implemented on
Home Assistant's own ``cryptography`` package so no extra dependency is needed.
"""
from __future__ import annotations

import hashlib
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Fixed protocol parameter shared by every device and app version (not a secret,
# not device specific). Gen2 devices use it as the static ECB input in non-SN
# mode; Gen3 devices use zeros.
FW_DATA = bytes.fromhex("97CFB802844143DE56002B3B34780A5D")

ZEROS16 = b"\x00" * 16

SUCCESS = 0
ERR_AUTH = -2
ERR_REPLAY = -3


def aes_ecb_block(key: bytes, block: bytes) -> bytes:
    """Encrypt exactly one 16-byte block with AES-128-ECB."""
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def derive_key(key1: bytes, key2: bytes | None) -> bytes:
    """AES key for a key pair: SHA-1 of both keys, padded to 16 bytes each."""
    k1 = (key1 + ZEROS16)[:16]
    k2 = (key2 + ZEROS16)[:16] if key2 else ZEROS16
    return hashlib.sha1(k1 + k2).digest()[:16]


def build_nonce(counter: int, auth: bytes) -> bytes:
    """13-byte nonce: counter (big endian) + half the auth parameter + padding."""
    return struct.pack(">I", counter) + (auth + ZEROS16)[:8] + b"\x00"


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class NbCryptoV2:
    """Encryption2 session state: keys, auth parameter and packet counters."""

    def __init__(self, *, gen2: bool = False) -> None:
        self._key1: bytes = b""
        self._key2: bytes | None = None
        self._aes_key: bytes = derive_key(b"", None)
        self.auth: bytes = ZEROS16
        # Static ECB input for non-SN mode; differs between device generations.
        self.ecb_input: bytes = FW_DATA if gen2 else ZEROS16
        self.counter = 0
        self.sn_mode = False
        self.last_rx_counter = -1

    # -- key / state management ------------------------------------------------

    def set_key(self, key1: bytes, key2: bytes | None) -> None:
        """Set the key pair for the current handshake phase."""
        self._key1, self._key2 = key1, key2
        self._aes_key = derive_key(key1, key2)

    def set_auth(self, auth: bytes) -> None:
        """Store the authentication parameter the vehicle handed us."""
        self.auth = auth

    def reset_sn(self) -> None:
        """Go back to non-SN mode (counter 0), used for PRE_COMM."""
        self.counter = 0
        self.sn_mode = False
        self.last_rx_counter = -1

    def start_sn(self) -> None:
        """Enable SN mode; the first encrypted frame will use counter 2."""
        self.counter = 1
        self.sn_mode = True

    # -- encryption ------------------------------------------------------------

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt a plaintext frame (header stays in the clear)."""
        if not self.sn_mode:
            return self._encrypt_non_sn(plaintext)

        self.counter += 1
        counter = self.counter
        nonce = build_nonce(counter, self.auth)
        payload = plaintext[3:]

        raw_tag = self._cbc_mac(plaintext, nonce)

        ciphertext = bytearray()
        for index, offset in enumerate(range(0, len(payload), 16), start=1):
            block = payload[offset : offset + 16]
            keystream = aes_ecb_block(
                self._aes_key, b"\x01" + nonce + bytes([0x00, index & 0xFF])
            )
            ciphertext += _xor(block, keystream[: len(block)])

        a0 = aes_ecb_block(self._aes_key, b"\x01" + nonce + b"\x00\x00")
        enc_tag = _xor(raw_tag, a0[:4])

        return plaintext[:3] + bytes(ciphertext) + enc_tag + struct.pack(">H", counter & 0xFFFF)

    def _encrypt_non_sn(self, plaintext: bytes) -> bytes:
        payload = plaintext[3:]
        checksum = (~sum(payload)) & 0xFFFF
        keystream = aes_ecb_block(self._aes_key, self.ecb_input)

        ciphertext = bytearray()
        for offset in range(0, len(payload), 16):
            block = payload[offset : offset + 16]
            ciphertext += _xor(block, keystream[: len(block)])

        return (
            plaintext[:3]
            + bytes(ciphertext)
            + bytes([0x00, 0x00, checksum & 0xFF, (checksum >> 8) & 0xFF, 0x00, 0x00])
        )

    # -- decryption ------------------------------------------------------------

    def decrypt(self, data: bytes) -> tuple[bytes, int]:
        """Decrypt a received frame. Returns (plaintext, status code)."""
        if len(data) < 9:
            return b"", ERR_AUTH

        header = data[:3]
        counter = struct.unpack(">H", data[-2:])[0]
        received_tag = data[-6:-2]
        payload = data[3:-6]

        if counter == 0:
            keystream = aes_ecb_block(self._aes_key, self.ecb_input)
            plain = bytearray()
            for offset in range(0, len(payload), 16):
                block = payload[offset : offset + 16]
                plain += _xor(block, keystream[: len(block)])
            return header + bytes(plain), SUCCESS

        if counter <= self.last_rx_counter:
            return b"", ERR_REPLAY

        nonce = build_nonce(counter, self.auth)
        plain = bytearray()
        for index, offset in enumerate(range(0, len(payload), 16), start=1):
            block = payload[offset : offset + 16]
            keystream = aes_ecb_block(
                self._aes_key, b"\x01" + nonce + bytes([0x00, index & 0xFF])
            )
            plain += _xor(block, keystream[: len(block)])

        plaintext = header + bytes(plain)

        a0 = aes_ecb_block(self._aes_key, b"\x01" + nonce + b"\x00\x00")
        expected = _xor(received_tag, a0[:4])
        if self._cbc_mac(plaintext, nonce) != expected:
            return b"", ERR_AUTH

        self.last_rx_counter = counter
        return plaintext, SUCCESS

    # -- MAC -------------------------------------------------------------------

    def _cbc_mac(self, plaintext: bytes, nonce: bytes) -> bytes:
        payload = plaintext[3:]
        b0 = b"\x59" + nonce + bytes([0x00, len(payload) & 0xFF])
        x = aes_ecb_block(self._aes_key, b0)
        x = aes_ecb_block(self._aes_key, _xor(x, plaintext[:3] + b"\x00" * 13))
        for offset in range(0, len(payload), 16):
            chunk = payload[offset : offset + 16].ljust(16, b"\x00")
            x = aes_ecb_block(self._aes_key, _xor(x, chunk))
        return x[:4]

from __future__ import annotations

import hashlib
from typing import Iterable


DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def _strip_0x(text: str) -> str:
    s = (text or "").strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    return s


def normalize_hex(text: str, even_length: bool = True) -> str:
    s = _strip_0x(text)
    if even_length and (len(s) % 2):
        s = "0" + s
    return s.lower()


def hex_to_bytes(text: str) -> bytes:
    s = normalize_hex(text, even_length=True)
    if not s:
        return b""
    return bytes.fromhex(s)


def bytes_to_hex(data: bytes) -> str:
    return bytes(data).hex()


def bytes_to_hex0x(data: bytes) -> str:
    return "0x" + bytes(data).hex()


def sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def compact_to_target(bits: int) -> int:
    bits = int(bits) & 0xFFFFFFFF
    exponent = (bits >> 24) & 0xFF
    mantissa = bits & 0x007FFFFF

    if bits & 0x00800000:
        return 0

    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def target_from_difficulty(difficulty: float | int) -> int:
    diff = float(difficulty)
    if diff <= 0:
        return DIFF1_TARGET
    target = int(DIFF1_TARGET / diff)
    if target < 0:
        target = 0
    if target > (1 << 256) - 1:
        target = (1 << 256) - 1
    return target


def target_from_nbits_hex(nbits_hex: str) -> int:
    raw = hex_to_bytes(nbits_hex)
    if len(raw) != 4:
        raise ValueError("nbits_hex must decode to exactly 4 bytes")

    # Stratum sends the compact bits value as display-order hex like "1c2ac4af".
    bits = int.from_bytes(raw, "big", signed=False)
    return compact_to_target(bits)


def hash_meets_target_be(hash32_be: bytes, target: int | bytes) -> bool:
    h = bytes(hash32_be)
    if len(h) != 32:
        raise ValueError("hash32_be must be exactly 32 bytes")

    if isinstance(target, (bytes, bytearray)):
        t = bytes(target)
        if len(t) != 32:
            raise ValueError("target bytes must be exactly 32 bytes")
        return h <= t

    return int.from_bytes(h, "big", signed=False) <= int(target)


def make_extranonce2(counter: int, extranonce2_size: int) -> str:
    size = max(0, int(extranonce2_size))
    if size <= 0:
        return ""
    value = int(counter) & ((1 << (size * 8)) - 1)
    return value.to_bytes(size, "big", signed=False).hex()


def build_coinbase(
    coinb1_hex: str,
    extranonce1_hex: str,
    extranonce2_hex: str,
    coinb2_hex: str,
) -> bytes:
    return (
        hex_to_bytes(coinb1_hex)
        + hex_to_bytes(extranonce1_hex)
        + hex_to_bytes(extranonce2_hex)
        + hex_to_bytes(coinb2_hex)
    )


def build_merkle_root_from_coinbase(
    coinbase_tx: bytes,
    merkle_branch_hex: Iterable[str],
) -> bytes:
    merkle_root = sha256d(coinbase_tx)
    for branch_hex in merkle_branch_hex:
        branch = hex_to_bytes(branch_hex)
        if len(branch) != 32:
            raise ValueError("each merkle branch must be exactly 32 bytes")
        merkle_root = sha256d(merkle_root + branch)
    return merkle_root


def _swap_prevhash_words(prevhash_hex: str) -> bytes:
    raw = hex_to_bytes(prevhash_hex)
    if len(raw) != 32:
        raise ValueError("prevhash must decode to exactly 32 bytes")
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 32, 4))


def _u32_hex_to_le(text: str) -> bytes:
    raw = hex_to_bytes(text)
    if len(raw) != 4:
        raise ValueError("expected exactly 4 bytes")
    value = int.from_bytes(raw, "big", signed=False)
    return value.to_bytes(4, "little", signed=False)


def build_header76(
    version_hex: str,
    prevhash_hex: str,
    merkle_root: bytes,
    ntime_hex: str,
    nbits_hex: str,
) -> bytes:
    version = _u32_hex_to_le(version_hex)
    prevhash = _swap_prevhash_words(prevhash_hex)
    merkle = bytes(merkle_root)  # keep raw digest bytes unless your native test proves otherwise
    ntime = _u32_hex_to_le(ntime_hex)
    nbits = _u32_hex_to_le(nbits_hex)

    if len(prevhash) != 32:
        raise ValueError("prevhash must decode to 32 bytes")
    if len(merkle) != 32:
        raise ValueError("merkle_root must be exactly 32 bytes")

    out = version + prevhash + merkle + ntime + nbits
    if len(out) != 76:
        raise ValueError(f"header76 length mismatch: got {len(out)}")
    return out
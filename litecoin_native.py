from __future__ import annotations

import ctypes
import os
from typing import Callable, Optional

from litecoin_models import LitecoinCandidateShare, LitecoinMinerConfig, LitecoinPreparedWork


U8_P = ctypes.POINTER(ctypes.c_ubyte)
U32_P = ctypes.POINTER(ctypes.c_uint32)
INT_P = ctypes.POINTER(ctypes.c_int)


def _module_dir() -> str:
    return os.path.abspath(os.path.dirname(__file__))


def _resolve_path(path: str) -> str:
    text = (path or "").strip()
    if not text:
        return os.path.join(_module_dir(), "LitecoinProject.dll")
    if os.path.isabs(text):
        return text
    return os.path.abspath(os.path.join(_module_dir(), text))


class LitecoinNativeBridge:
    def __init__(self, dll_path: str, on_log: Optional[Callable[[str], None]] = None) -> None:
        self.dll_path = _resolve_path(dll_path)
        self.on_log = on_log or (lambda msg: None)

        self.lib = None
        self.available = False
        self.load_error = ""
        self._dll_dir_handles: list[object] = []

        self._fn_scrypt_hash = None
        self._fn_scrypt_scan = None
        self._fn_hash_meets_target = None
        self._fn_last_error_code = None
        self._fn_last_error_message = None

        try:
            if os.name == "nt":
                dll_dir = os.path.dirname(self.dll_path)
                if dll_dir and hasattr(os, "add_dll_directory"):
                    self._dll_dir_handles.append(os.add_dll_directory(dll_dir))

            self.lib = ctypes.CDLL(self.dll_path)
            self._bind_functions()

            self.available = bool(
                self._fn_scrypt_hash is not None
                and self._fn_scrypt_scan is not None
                and self._fn_hash_meets_target is not None
            )

            if self.available:
                self.on_log(
                    f"[native] loaded {self.dll_path} "
                    f"(scrypt_hash=yes, scrypt_scan=yes, hash_meets_target=yes)"
                )
            else:
                missing = []
                if self._fn_scrypt_hash is None:
                    missing.append("ltc_scrypt_hash")
                if self._fn_scrypt_scan is None:
                    missing.append("ltc_scrypt_scan")
                if self._fn_hash_meets_target is None:
                    missing.append("ltc_hash_meets_target")
                self.load_error = "missing required exports: " + ", ".join(missing)
                self.on_log(f"[native] unavailable: {self.load_error}")
        except Exception as exc:
            self.lib = None
            self.available = False
            self.load_error = str(exc)
            self.on_log(f"[native] unavailable: {exc}")

    def _bind_optional(self, name: str, argtypes, restype):
        if self.lib is None:
            return None
        func = getattr(self.lib, name, None)
        if func is None:
            return None
        func.argtypes = argtypes
        func.restype = restype
        return func

    def _bind_functions(self) -> None:
        if self.lib is None:
            raise RuntimeError("DLL not loaded")

        self._fn_scrypt_hash = self._bind_optional(
            "ltc_scrypt_hash",
            [U8_P, U8_P],
            ctypes.c_int,
        )
        self._fn_scrypt_scan = self._bind_optional(
            "ltc_scrypt_scan",
            [U8_P, U8_P, ctypes.c_uint32, ctypes.c_uint32, U32_P, U8_P, INT_P],
            ctypes.c_int,
        )
        self._fn_hash_meets_target = self._bind_optional(
            "ltc_hash_meets_target",
            [U8_P, U8_P],
            ctypes.c_int,
        )
        self._fn_last_error_code = self._bind_optional(
            "ltc_last_error_code",
            [],
            ctypes.c_int,
        )
        self._fn_last_error_message = self._bind_optional(
            "ltc_last_error_message",
            [ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_uint32,
        )

    @staticmethod
    def _u8_array(data: bytes, expected_len: int):
        if len(data) != expected_len:
            raise ValueError(f"expected exactly {expected_len} bytes, got {len(data)}")
        return (ctypes.c_ubyte * expected_len).from_buffer_copy(data)

    def _dll_error_suffix(self) -> str:
        parts: list[str] = []

        try:
            if self._fn_last_error_code is not None:
                parts.append(f"code={int(self._fn_last_error_code())}")
        except Exception:
            pass

        try:
            if self._fn_last_error_message is not None:
                buf = ctypes.create_string_buffer(1024)
                self._fn_last_error_message(ctypes.cast(buf, ctypes.c_void_p), 1024)
                msg = buf.value.decode("utf-8", errors="replace").strip()
                if msg:
                    parts.append(f"msg={msg}")
        except Exception:
            pass

        return f" ({', '.join(parts)})" if parts else ""

    def scrypt_hash(self, header80: bytes) -> bytes:
        if self._fn_scrypt_hash is None:
            raise RuntimeError("ltc_scrypt_hash is not available")
        header_arr = self._u8_array(header80, 80)
        out_arr = (ctypes.c_ubyte * 32)()
        rc = self._fn_scrypt_hash(header_arr, out_arr)
        if rc != 0:
            raise RuntimeError(f"ltc_scrypt_hash failed: rc={rc}{self._dll_error_suffix()}")
        return bytes(out_arr)

    def scrypt_scan(
        self,
        header76: bytes,
        target32_le: bytes,
        start_nonce: int,
        iterations: int,
    ) -> tuple[int, bytes] | None:
        if self._fn_scrypt_scan is None:
            raise RuntimeError("ltc_scrypt_scan is not available")

        header_arr = self._u8_array(header76, 76)
        target_arr = self._u8_array(target32_le, 32)
        out_nonce = ctypes.c_uint32(0)
        out_hash = (ctypes.c_ubyte * 32)()
        out_found = ctypes.c_int(0)

        rc = self._fn_scrypt_scan(
            header_arr,
            target_arr,
            ctypes.c_uint32(int(start_nonce) & 0xFFFFFFFF),
            ctypes.c_uint32(max(0, int(iterations))),
            ctypes.byref(out_nonce),
            out_hash,
            ctypes.byref(out_found),
        )
        if rc != 0:
            raise RuntimeError(f"ltc_scrypt_scan failed: rc={rc}{self._dll_error_suffix()}")

        if int(out_found.value) != 1:
            return None

        return int(out_nonce.value), bytes(out_hash)

    def hash_meets_target(self, hash32_le: bytes, target32_le: bytes) -> bool:
        if self._fn_hash_meets_target is None:
            raise RuntimeError("ltc_hash_meets_target is not available")
        hash_arr = self._u8_array(hash32_le, 32)
        tgt_arr = self._u8_array(target32_le, 32)
        rc = self._fn_hash_meets_target(hash_arr, tgt_arr)
        return int(rc) == 1

    def close(self) -> None:
        self.lib = None
        self.available = False

        self._fn_scrypt_hash = None
        self._fn_scrypt_scan = None
        self._fn_hash_meets_target = None
        self._fn_last_error_code = None
        self._fn_last_error_message = None

        for handle in self._dll_dir_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._dll_dir_handles.clear()


class NativeLitecoinScanner:
    name = "native"

    def __init__(
        self,
        config: LitecoinMinerConfig,
        on_log: Callable[[str], None],
        native: Optional[LitecoinNativeBridge] = None,
    ) -> None:
        self.config = config
        self.on_log = on_log
        self.native = native

    def initialize(self) -> None:
        if self.native is None or not self.native.available:
            raise RuntimeError("Native scanner requires LitecoinProject.dll")

    def scan(
        self,
        work: LitecoinPreparedWork,
        start_nonce: int,
        count: int,
        max_results: int | None = None,
    ) -> list[LitecoinCandidateShare]:
        if self.native is None or not self.native.available:
            raise RuntimeError("Native scanner is not available")

        count = max(1, int(count))

        result = self.native.scrypt_scan(
            header76=work.header76,
            target32_le=work.share_target32_le,
            start_nonce=start_nonce,
            iterations=count,
        )

        if result is None:
            return []

        if not isinstance(result, tuple) or len(result) != 2:
            self.on_log(f"[native] unexpected scan result: {result!r}")
            return []

        nonce, hash32_le = result

        if not isinstance(hash32_le, (bytes, bytearray)) or len(hash32_le) != 32:
            self.on_log(f"[native] invalid hash result for nonce={nonce!r}")
            return []

        if not self.native.hash_meets_target(bytes(hash32_le), work.share_target32_le):
            return []

        return [
            LitecoinCandidateShare(
                job_id=work.job_id,
                extranonce2_hex=work.extranonce2_hex,
                ntime_hex=work.ntime_hex,
                nonce_hex=f"{int(nonce) & 0xFFFFFFFF:08x}",
                hash_hex=bytes(hash32_le)[::-1].hex(),
                backend="native",
            )
        ]

    def close(self) -> None:
        return
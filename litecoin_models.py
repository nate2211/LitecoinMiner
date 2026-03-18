from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from typing import Any, Mapping


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return int(default)
            if text.lower().startswith("0x"):
                return int(text, 16)
            return int(float(text))
        return int(value)
    except Exception:
        return int(default)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _coerce_scan_backend(value: Any, default: str = "native") -> str:
    text = str(value or default).strip().lower()
    if text in {"native", "opencl"}:
        return text
    return default


@dataclass
class LitecoinMinerConfig:
    host: str = "us.litecoinpool.org"
    port: int = 3333
    login: str = "your_username.1"
    password: str = "1"
    agent: str = "Python-LTC/1.0"
    use_tls: bool = False

    socket_timeout_s: float = 30.0
    rpc_timeout_s: float = 15.0
    submit_timeout_s: float = 15.0
    reconnect_delay_s: float = 5.0
    idle_sleep_s: float = 0.05
    log_hashrate_interval_s: float = 30.0

    native_dll_path: str = "LitecoinProject.dll"
    scan_backend: str = "native"
    scan_window_nonces: int = 4096

    platform_index: int = 0
    device_index: int = 0
    kernel_path: str = "litecoin_scrypt_scan.cl"
    opencl_kernel_name: str = "ltc_scrypt_scan"
    local_work_size: int = 128
    build_options: str = "-cl-std=CL1.2"
    max_results_per_scan: int = 32

    @property
    def uses_opencl(self) -> bool:
        return self.scan_backend == "opencl"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "LitecoinMinerConfig":
        if not isinstance(raw, Mapping):
            return cls()

        valid_names = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for name in valid_names:
            if name in raw:
                kwargs[name] = raw[name]

        cfg = cls(**kwargs)

        cfg.host = str(cfg.host or "").strip() or "us.litecoinpool.org"
        cfg.port = max(1, _coerce_int(cfg.port, 3333))
        cfg.login = str(cfg.login or "").strip()
        cfg.password = str(cfg.password or "1").strip() or "1"
        cfg.agent = str(cfg.agent or "Python-LTC/1.0").strip() or "Python-LTC/1.0"
        cfg.use_tls = _coerce_bool(cfg.use_tls, False)

        cfg.socket_timeout_s = max(0.1, _coerce_float(cfg.socket_timeout_s, 30.0))
        cfg.rpc_timeout_s = max(0.1, _coerce_float(cfg.rpc_timeout_s, 15.0))
        cfg.submit_timeout_s = max(0.1, _coerce_float(cfg.submit_timeout_s, 15.0))
        cfg.reconnect_delay_s = max(0.1, _coerce_float(cfg.reconnect_delay_s, 5.0))
        cfg.idle_sleep_s = max(0.001, _coerce_float(cfg.idle_sleep_s, 0.05))
        cfg.log_hashrate_interval_s = max(1.0, _coerce_float(cfg.log_hashrate_interval_s, 30.0))

        cfg.native_dll_path = str(cfg.native_dll_path or "LitecoinProject.dll").strip() or "LitecoinProject.dll"
        cfg.scan_backend = _coerce_scan_backend(cfg.scan_backend, "native")
        cfg.scan_window_nonces = max(1, _coerce_int(cfg.scan_window_nonces, 4096))

        cfg.platform_index = max(0, _coerce_int(cfg.platform_index, 0))
        cfg.device_index = max(0, _coerce_int(cfg.device_index, 0))
        cfg.kernel_path = str(cfg.kernel_path or "litecoin_scrypt_scan.cl").strip() or "litecoin_scrypt_scan.cl"
        cfg.opencl_kernel_name = str(cfg.opencl_kernel_name or "ltc_scrypt_scan").strip() or "ltc_scrypt_scan"
        cfg.local_work_size = max(1, _coerce_int(cfg.local_work_size, 128))
        cfg.build_options = str(cfg.build_options or "-cl-std=CL1.2").strip() or "-cl-std=CL1.2"
        cfg.max_results_per_scan = max(1, _coerce_int(cfg.max_results_per_scan, 32))

        return cfg


@dataclass
class LitecoinSession:
    connected: bool = False
    subscribed: bool = False
    authorized: bool = False
    extranonce1_hex: str = ""
    extranonce2_size: int = 0
    difficulty: float = 1.0


@dataclass
class LitecoinJob:
    job_id: str
    prevhash_hex: str
    coinb1_hex: str
    coinb2_hex: str
    merkle_branch_hex: list[str]
    version_hex: str
    nbits_hex: str
    ntime_hex: str
    clean_jobs: bool
    difficulty: float
    extranonce1_hex: str
    extranonce2_size: int
    received_at: float = field(default_factory=time.time)


@dataclass
class LitecoinPreparedWork:
    job_id: str
    header76: bytes
    share_target32_le: bytes
    share_target_int: int
    network_target_int: int
    extranonce2_hex: str
    ntime_hex: str
    difficulty: float


@dataclass
class LitecoinCandidateShare:
    job_id: str
    extranonce2_hex: str
    ntime_hex: str
    nonce_hex: str
    hash_hex: str
    backend: str = "native"


@dataclass
class SubmitResult:
    accepted: bool
    status: str = ""
    error: str = ""
    raw: Any = None
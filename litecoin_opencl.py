from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

try:
    import pyopencl as cl
except Exception:
    cl = None

from litecoin_models import LitecoinCandidateShare, LitecoinMinerConfig, LitecoinPreparedWork
from litecoin_native import LitecoinNativeBridge


@dataclass
class OpenCLDeviceInfo:
    platform_index: int
    device_index: int
    platform_name: str
    device_name: str


def _search_roots() -> list[str]:
    roots: list[str] = []

    try:
        roots.append(os.path.abspath(os.path.dirname(__file__)))
    except Exception:
        pass

    try:
        roots.append(os.path.abspath(os.getcwd()))
    except Exception:
        pass

    try:
        roots.append(os.path.abspath(os.path.dirname(sys.executable)))
    except Exception:
        pass

    try:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(os.path.abspath(meipass))
    except Exception:
        pass

    out: list[str] = []
    seen: set[str] = set()
    for root in roots:
        norm = os.path.normcase(os.path.abspath(root))
        if norm not in seen:
            seen.add(norm)
            out.append(os.path.abspath(root))
    return out


def _candidate_paths(path: str, default_name: str) -> list[str]:
    raw = (path or "").strip()
    roots = _search_roots()
    candidates: list[str] = []

    if raw:
        if os.path.isabs(raw):
            candidates.append(os.path.abspath(raw))
            basename = os.path.basename(raw)
            if basename:
                for root in roots:
                    candidates.append(os.path.abspath(os.path.join(root, basename)))
        else:
            for root in roots:
                candidates.append(os.path.abspath(os.path.join(root, raw)))
    else:
        for root in roots:
            candidates.append(os.path.abspath(os.path.join(root, default_name)))

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        norm = os.path.normcase(os.path.abspath(candidate))
        if norm not in seen:
            seen.add(norm)
            out.append(os.path.abspath(candidate))
    return out


def _resolve_existing_path(path: str, default_name: str) -> str:
    candidates = _candidate_paths(path, default_name)
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    tried = "\n  ".join(candidates)
    raise FileNotFoundError(
        f"Could not locate {default_name!r}. Tried:\n  {tried}"
    )


class OpenCLLitecoinScanner:
    name = "opencl"

    def __init__(
        self,
        config: LitecoinMinerConfig,
        on_log: Callable[[str], None],
        native: Optional[LitecoinNativeBridge] = None,
    ) -> None:
        self.config = config
        self.on_log = on_log
        self.native = native  # optional, not required by scanner itself

        self.ctx = None
        self.queue = None
        self.program = None
        self.device = None
        self.kernel = None
        self._lws: Optional[int] = None

        self._scratch_buf = None
        self._scratch_buf_nbytes = 0

    @staticmethod
    def list_devices() -> list[OpenCLDeviceInfo]:
        if cl is None:
            return []

        out: list[OpenCLDeviceInfo] = []
        for p_idx, platform in enumerate(cl.get_platforms()):
            for d_idx, device in enumerate(platform.get_devices()):
                out.append(
                    OpenCLDeviceInfo(
                        platform_index=p_idx,
                        device_index=d_idx,
                        platform_name=platform.name.strip(),
                        device_name=device.name.strip(),
                    )
                )
        return out

    def initialize(self) -> None:
        if cl is None:
            raise RuntimeError("pyopencl is not installed")

        kernel_path = _resolve_existing_path(
            getattr(self.config, "kernel_path", "") or "",
            "litecoin_scrypt_scan.cl",
        )
        self.on_log(f"[opencl] kernel_path={kernel_path}")

        with open(kernel_path, "r", encoding="utf-8") as fh:
            src = fh.read()

        platforms = cl.get_platforms()
        if not platforms:
            raise RuntimeError("No OpenCL platforms found")

        p_idx = int(self.config.platform_index)
        if p_idx < 0 or p_idx >= len(platforms):
            raise RuntimeError(f"Invalid platform_index={p_idx}; available={len(platforms)}")

        platform = platforms[p_idx]
        devices = platform.get_devices()
        if not devices:
            raise RuntimeError(f"No OpenCL devices found on platform {platform.name!r}")

        d_idx = int(self.config.device_index)
        if d_idx < 0 or d_idx >= len(devices):
            raise RuntimeError(f"Invalid device_index={d_idx}; available={len(devices)}")

        self.device = devices[d_idx]
        self.ctx = cl.Context(devices=[self.device])
        self.queue = cl.CommandQueue(self.ctx, self.device)

        build_options = str(getattr(self.config, "build_options", "") or "").strip()
        program = None
        try:
            program = cl.Program(self.ctx, src)
            self.program = program.build(options=build_options or None)
        except Exception as exc:
            build_log_parts: list[str] = []
            try:
                if program is not None and self.ctx is not None:
                    for dev in self.ctx.devices:
                        log_text = program.get_build_info(dev, cl.program_build_info.LOG)
                        if log_text and str(log_text).strip():
                            build_log_parts.append(f"[{dev.name.strip()}]\n{str(log_text).strip()}")
            except Exception:
                pass

            if build_log_parts:
                self.on_log("[opencl] build log:\n" + "\n\n".join(build_log_parts))
            raise RuntimeError(f"OpenCL build failed: {exc}") from exc

        kernel_name = str(getattr(self.config, "opencl_kernel_name", "") or "").strip()
        if not kernel_name:
            raise RuntimeError("config.opencl_kernel_name is empty")

        try:
            self.kernel = cl.Kernel(self.program, kernel_name)
        except Exception as exc:
            raise RuntimeError(
                f"Kernel {kernel_name!r} not found in {os.path.basename(kernel_path)!r}"
            ) from exc

        self._lws = self._choose_local_work_size(self.kernel, kernel_name)

        self.on_log(
            f"[opencl] initialized platform={platform.name.strip()} "
            f"device={self.device.name.strip()} kernel={kernel_name} lws={self._lws}"
        )

    def _choose_local_work_size(self, kernel, kernel_name: str) -> int:
        requested = int(getattr(self.config, "local_work_size", 1))

        kernel_max = 1
        preferred_multiple = 1

        try:
            kernel_max = int(
                kernel.get_work_group_info(
                    cl.kernel_work_group_info.WORK_GROUP_SIZE,
                    self.device,
                )
            )
        except Exception:
            pass

        try:
            preferred_multiple = int(
                kernel.get_work_group_info(
                    cl.kernel_work_group_info.PREFERRED_WORK_GROUP_SIZE_MULTIPLE,
                    self.device,
                )
            )
        except Exception:
            pass

        chosen = min(max(1, requested), max(1, kernel_max))
        if preferred_multiple > 1 and chosen >= preferred_multiple:
            chosen = max(preferred_multiple, (chosen // preferred_multiple) * preferred_multiple)

        chosen = max(1, min(chosen, max(1, kernel_max)))

        self.on_log(
            f"[opencl] workgroup kernel={kernel_name} requested={requested} "
            f"chosen={chosen} kernel_max={kernel_max} preferred_multiple={preferred_multiple}"
        )
        return chosen

    @staticmethod
    def _round_up(value: int, multiple: int) -> int:
        if multiple <= 0:
            return value
        return ((value + multiple - 1) // multiple) * multiple

    def scan(
        self,
        work: LitecoinPreparedWork,
        start_nonce: int,
        count: int,
        max_results: int | None = None,
    ) -> list[LitecoinCandidateShare]:
        if cl is None:
            raise RuntimeError("pyopencl is not installed")
        if self.ctx is None or self.queue is None or self.kernel is None:
            raise RuntimeError("OpenCL scanner has not been initialized")

        count = max(1, int(count))
        max_results = max(
            1,
            int(
                max_results
                if max_results is not None
                else getattr(self.config, "max_results_per_scan", 1)
            ),
        )

        mf = cl.mem_flags

        header76_arr = np.frombuffer(work.header76, dtype=np.uint8)
        target_arr = np.frombuffer(work.share_target32_le, dtype=np.uint8)

        out_count = np.zeros((1,), dtype=np.uint32)
        out_nonces = np.zeros((max_results,), dtype=np.uint32)
        out_hashes = np.zeros((max_results * 32,), dtype=np.uint8)

        header76_buf = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=header76_arr)
        target_buf = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=target_arr)
        out_count_buf = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=out_count)
        out_nonces_buf = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=out_nonces)
        out_hashes_buf = cl.Buffer(self.ctx, mf.READ_WRITE | mf.COPY_HOST_PTR, hostbuf=out_hashes)

        lws = int(self._lws or 1)
        gws = self._round_up(count, lws)

        scratch_words_per_item = 1024 * 32  # SCRYPT_V_WORDS
        required_words = gws * scratch_words_per_item
        required_bytes = required_words * 4

        if self._scratch_buf is None or self._scratch_buf_nbytes < required_bytes:
            self._scratch_buf = cl.Buffer(self.ctx, mf.READ_WRITE, required_bytes)
            self._scratch_buf_nbytes = required_bytes
            self.on_log(
                f"[opencl] allocated scratch buffer size={required_bytes} bytes "
                f"gws={gws} lws={lws}"
            )

        evt = self.kernel(
            self.queue,
            (gws,),
            (lws,),
            header76_buf,
            target_buf,
            np.uint32(int(start_nonce) & 0xFFFFFFFF),
            np.uint32(count),
            np.uint32(max_results),
            self._scratch_buf,
            out_count_buf,
            out_nonces_buf,
            out_hashes_buf,
        )
        evt.wait()

        cl.enqueue_copy(self.queue, out_count, out_count_buf).wait()
        found_count = min(int(out_count[0]), max_results)
        if found_count <= 0:
            return []

        cl.enqueue_copy(self.queue, out_nonces, out_nonces_buf).wait()
        cl.enqueue_copy(self.queue, out_hashes, out_hashes_buf).wait()

        results: list[LitecoinCandidateShare] = []
        for i in range(found_count):
            nonce = int(out_nonces[i])
            hash_le = bytes(out_hashes[i * 32:(i + 1) * 32].tolist())

            if len(hash_le) != 32:
                continue

            results.append(
                LitecoinCandidateShare(
                    job_id=work.job_id,
                    extranonce2_hex=work.extranonce2_hex,
                    ntime_hex=work.ntime_hex,
                    nonce_hex=f"{nonce:08x}",
                    hash_hex=hash_le[::-1].hex(),
                    backend="opencl",
                )
            )

        return results

    def close(self) -> None:
        self._scratch_buf = None
        self._scratch_buf_nbytes = 0
        self.kernel = None
        self.program = None
        self.queue = None
        self.ctx = None
        self.device = None
from __future__ import annotations

import time
from typing import Callable, Optional

from litecoin_models import LitecoinCandidateShare, LitecoinJob, LitecoinMinerConfig, LitecoinPreparedWork
from litecoin_native import LitecoinNativeBridge, NativeLitecoinScanner
from litecoin_pool import LitecoinStratumClient
from litecoin_utils import (
    build_coinbase,
    build_header76,
    build_merkle_root_from_coinbase,
    make_extranonce2,
    target_from_difficulty,
    target_from_nbits_hex,
)


class LitecoinMinerWorker:
    def __init__(
        self,
        config: LitecoinMinerConfig,
        on_log: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.on_log = on_log or (lambda msg: print(msg, flush=True))
        self.on_status = on_status or (lambda status: None)

        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _need_native_bridge(self) -> bool:
        backend = str(self.config.scan_backend or "native").strip().lower()
        if backend == "native":
            return True
        if backend == "opencl" and bool(self.config.verify_opencl_hits_on_cpu):
            return True
        return False

    def _create_scanner(self, native: Optional[LitecoinNativeBridge]):
        backend = str(self.config.scan_backend or "native").strip().lower()
        if backend == "opencl":
            from litecoin_opencl import OpenCLLitecoinScanner
            return OpenCLLitecoinScanner(
                config=self.config,
                on_log=self.on_log,
                native=native,
            )
        return NativeLitecoinScanner(
            config=self.config,
            on_log=self.on_log,
            native=native,
        )

    def _prepare_work(self, job: LitecoinJob, extranonce2_counter: int) -> LitecoinPreparedWork:
        extranonce2_hex = make_extranonce2(
            counter=extranonce2_counter,
            extranonce2_size=job.extranonce2_size,
        )

        coinbase = build_coinbase(
            coinb1_hex=job.coinb1_hex,
            extranonce1_hex=job.extranonce1_hex,
            extranonce2_hex=extranonce2_hex,
            coinb2_hex=job.coinb2_hex,
        )

        merkle_root = build_merkle_root_from_coinbase(
            coinbase_tx=coinbase,
            merkle_branch_hex=job.merkle_branch_hex,
        )

        header76 = build_header76(
            version_hex=job.version_hex,
            prevhash_hex=job.prevhash_hex,
            merkle_root=merkle_root,
            ntime_hex=job.ntime_hex,
            nbits_hex=job.nbits_hex,
        )

        share_target_int = target_from_difficulty(job.difficulty)
        network_target_int = target_from_nbits_hex(job.nbits_hex)

        prepared = LitecoinPreparedWork(
            job_id=job.job_id,
            header76=header76,
            share_target32_le=share_target_int.to_bytes(32, "little", signed=False),
            share_target_int=share_target_int,
            network_target_int=network_target_int,
            extranonce2_hex=extranonce2_hex,
            ntime_hex=job.ntime_hex,
            difficulty=job.difficulty,
        )

        self.on_log(
            f"[work] prepared job={prepared.job_id} diff={prepared.difficulty} "
            f"extranonce2={prepared.extranonce2_hex} "
            f"share_target=0x{prepared.share_target_int:064x} "
            f"network_target=0x{prepared.network_target_int:064x}"
        )
        return prepared

    def _sleep_stop_aware(self, seconds: float) -> None:
        end_at = time.monotonic() + max(0.0, float(seconds))
        while not self._stop and time.monotonic() < end_at:
            time.sleep(min(0.1, max(0.0, end_at - time.monotonic())))

    def _verify_opencl_candidate(
        self,
        native: Optional[LitecoinNativeBridge],
        prepared: LitecoinPreparedWork,
        candidate: LitecoinCandidateShare,
    ) -> Optional[LitecoinCandidateShare]:
        if native is None or not native.available:
            raise RuntimeError("OpenCL CPU verification is enabled but native bridge is unavailable")

        try:
            nonce = int(str(candidate.nonce_hex).strip(), 16) & 0xFFFFFFFF
        except Exception as exc:
            self.on_log(
                f"[verify] dropped gpu hit nonce={candidate.nonce_hex!r} "
                f"reason=invalid_nonce error={exc}"
            )
            return None

        header80 = prepared.header76 + nonce.to_bytes(4, "little", signed=False)
        exact_hash_le = native.scrypt_hash(header80)
        exact_hash_hex = exact_hash_le[::-1].hex()

        gpu_hash_hex = str(candidate.hash_hex or "").strip().lower()

        if not native.hash_meets_target(exact_hash_le, prepared.share_target32_le):
            self.on_log(
                f"[verify] dropped gpu hit nonce={candidate.nonce_hex} "
                f"gpu_hash=0x{gpu_hash_hex or 'unknown'} "
                f"cpu_hash=0x{exact_hash_hex} "
                f"reason=cpu_hash_above_share_target"
            )
            return None

        if gpu_hash_hex and gpu_hash_hex != exact_hash_hex:
            self.on_log(
                f"[verify] warning nonce={candidate.nonce_hex} "
                f"gpu_hash=0x{gpu_hash_hex} "
                f"cpu_hash=0x{exact_hash_hex} "
                f"note=using_cpu_hash"
            )

        return LitecoinCandidateShare(
            job_id=candidate.job_id,
            extranonce2_hex=candidate.extranonce2_hex,
            ntime_hex=candidate.ntime_hex,
            nonce_hex=f"{nonce:08x}",
            hash_hex=exact_hash_hex,
            backend="opencl",
        )

    def run(self) -> None:
        reconnect_delay = max(0.1, float(self.config.reconnect_delay_s))

        while not self._stop:
            pool: Optional[LitecoinStratumClient] = None
            native: Optional[LitecoinNativeBridge] = None
            scanner = None

            try:
                self.on_status("starting")

                if self._need_native_bridge():
                    native = LitecoinNativeBridge(self.config.native_dll_path, on_log=self.on_log)
                    if not native.available:
                        raise RuntimeError(native.load_error or "native bridge unavailable")
                else:
                    self.on_log("[worker] native bridge not required for current configuration")

                scanner = self._create_scanner(native)
                scanner.initialize()
                self.on_log(f"[worker] scanner={scanner.name}")

                if scanner.name == "opencl":
                    self.on_log(
                        f"[worker] opencl_cpu_verify={'on' if self.config.verify_opencl_hits_on_cpu else 'off'}"
                    )

                pool = LitecoinStratumClient(self.config, on_log=self.on_log)

                self.on_status("connecting")
                pool.connect_and_authorize()
                self.on_status("running")

                current_job: Optional[LitecoinJob] = None
                prepared: Optional[LitecoinPreparedWork] = None
                extranonce2_counter = 0
                start_nonce = 0

                last_rate_at = time.monotonic()
                scanned_since_rate = 0

                while not self._stop:
                    if not pool.alive:
                        raise ConnectionError(pool.reader_error or "pool reader stopped")

                    newest = pool.wait_for_job(timeout=self.config.idle_sleep_s)
                    if newest is not None and (
                        current_job is None or newest.received_at != current_job.received_at
                    ):
                        current_job = newest
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1
                        start_nonce = 0

                    if current_job is None:
                        latest = pool.get_latest_job()
                        if latest is not None:
                            current_job = latest
                            prepared = self._prepare_work(current_job, extranonce2_counter)
                            extranonce2_counter += 1
                            start_nonce = 0
                        else:
                            continue

                    latest = pool.get_latest_job()
                    if latest is not None and latest.received_at != current_job.received_at:
                        current_job = latest
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1
                        start_nonce = 0

                    if prepared is None:
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1
                        start_nonce = 0

                    window = min(
                        int(self.config.scan_window_nonces),
                        0x100000000 - start_nonce,
                    )
                    if window <= 0:
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1
                        start_nonce = 0
                        continue

                    self.on_log(
                        f"[scan] backend={scanner.name} job={prepared.job_id} "
                        f"start_nonce={start_nonce:08x} count={window}"
                    )

                    candidates = scanner.scan(
                        work=prepared,
                        start_nonce=start_nonce,
                        count=window,
                        max_results=self.config.max_results_per_scan,
                    )

                    scanned_since_rate += window

                    now = time.monotonic()
                    elapsed = now - last_rate_at
                    if elapsed >= float(self.config.log_hashrate_interval_s):
                        rate = scanned_since_rate / elapsed if elapsed > 0 else 0.0
                        self.on_log(
                            f"[hashrate] current={rate / 1000.0:.3f} kH/s "
                            f"window={elapsed:.3f}s scanned={scanned_since_rate} "
                            f"scanner={scanner.name}"
                        )
                        scanned_since_rate = 0
                        last_rate_at = now

                    if not pool.alive:
                        raise ConnectionError(pool.reader_error or "pool reader stopped")

                    for candidate in candidates:
                        if self._stop:
                            break

                        if not pool.alive:
                            raise ConnectionError(pool.reader_error or "pool reader stopped")

                        authoritative = candidate

                        if scanner.name == "opencl" and self.config.verify_opencl_hits_on_cpu:
                            verified = self._verify_opencl_candidate(native, prepared, candidate)
                            if verified is None:
                                continue
                            authoritative = verified

                        latest = pool.get_latest_job()
                        if latest is not None:
                            if (
                                str(latest.job_id) != str(authoritative.job_id)
                                or str(latest.ntime_hex).lower() != str(authoritative.ntime_hex).lower()
                            ):
                                self.on_log(
                                    f"[submit] dropping stale candidate nonce={authoritative.nonce_hex} "
                                    f"candidate_job={authoritative.job_id} latest_job={latest.job_id} "
                                    f"candidate_ntime={authoritative.ntime_hex} latest_ntime={latest.ntime_hex}"
                                )
                                continue

                        self.on_log(
                            f"[share] found job={authoritative.job_id} nonce={authoritative.nonce_hex} "
                            f"hash=0x{authoritative.hash_hex} backend={authoritative.backend}"
                        )

                        hash_le = bytes.fromhex(authoritative.hash_hex)[::-1]
                        if int.from_bytes(hash_le, "little", signed=False) <= prepared.network_target_int:
                            self.on_log(f"[share] block-candidate nonce={authoritative.nonce_hex}")

                        submit = pool.submit_share(
                            job_id=authoritative.job_id,
                            extranonce2_hex=authoritative.extranonce2_hex,
                            ntime_hex=authoritative.ntime_hex,
                            nonce_hex=authoritative.nonce_hex,
                        )

                        if submit.accepted:
                            self.on_log(f"[submit] accepted nonce={authoritative.nonce_hex}")
                        else:
                            self.on_log(
                                f"[submit] rejected nonce={authoritative.nonce_hex} "
                                f"status={submit.status} error={submit.error}"
                            )

                    start_nonce = (start_nonce + window) & 0xFFFFFFFF

                    if start_nonce == 0:
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1

            except Exception as exc:
                self.on_log(f"[worker] error: {exc}")
                self.on_status("reconnecting")
                if self._stop:
                    break
                self._sleep_stop_aware(reconnect_delay)

            finally:
                try:
                    if scanner is not None:
                        scanner.close()
                except Exception:
                    pass

                try:
                    if pool is not None:
                        pool.close()
                except Exception:
                    pass

                try:
                    if native is not None:
                        native.close()
                except Exception:
                    pass

        self.on_status("closed")
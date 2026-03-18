from __future__ import annotations

import time
from typing import Callable, Optional

from litecoin_models import (
    LitecoinJob,
    LitecoinMinerConfig,
    LitecoinPreparedWork,
)
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

    def _create_scanner(self, native: LitecoinNativeBridge):
        backend = self.config.scan_backend
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

    def run(self) -> None:
        while not self._stop:
            pool: Optional[LitecoinStratumClient] = None
            native: Optional[LitecoinNativeBridge] = None
            scanner = None

            try:
                self.on_status("starting")

                native = LitecoinNativeBridge(self.config.native_dll_path, on_log=self.on_log)
                if not native.available:
                    raise RuntimeError(native.load_error or "native bridge unavailable")

                scanner = self._create_scanner(native)
                scanner.initialize()
                self.on_log(f"[worker] scanner={scanner.name}")

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
                    newest = pool.wait_for_job(timeout=self.config.idle_sleep_s)
                    if newest is not None and (
                        current_job is None or newest.received_at != current_job.received_at
                    ):
                        current_job = newest
                        prepared = self._prepare_work(current_job, extranonce2_counter)
                        extranonce2_counter += 1
                        start_nonce = 0

                    if current_job is None or prepared is None:
                        continue

                    latest = pool.get_latest_job()
                    if latest is not None and latest.received_at != current_job.received_at:
                        current_job = latest
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

                    for candidate in candidates:
                        self.on_log(
                            f"[share] found job={candidate.job_id} nonce={candidate.nonce_hex} "
                            f"hash=0x{candidate.hash_hex} backend={candidate.backend}"
                        )

                        hash_le = bytes.fromhex(candidate.hash_hex)[::-1]
                        if int.from_bytes(hash_le, "little", signed=False) <= prepared.network_target_int:
                            self.on_log(f"[share] block-candidate nonce={candidate.nonce_hex}")

                        submit = pool.submit_share(
                            job_id=candidate.job_id,
                            extranonce2_hex=candidate.extranonce2_hex,
                            ntime_hex=candidate.ntime_hex,
                            nonce_hex=candidate.nonce_hex,
                        )

                        if submit.accepted:
                            self.on_log(f"[submit] accepted nonce={candidate.nonce_hex}")
                        else:
                            self.on_log(
                                f"[submit] rejected nonce={candidate.nonce_hex} "
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
                time.sleep(self.config.reconnect_delay_s)
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
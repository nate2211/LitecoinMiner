from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
from dataclasses import asdict
from typing import Any, Optional

from litecoin_models import LitecoinJob, LitecoinMinerConfig, LitecoinSession, SubmitResult


class LitecoinStratumClient:
    def __init__(self, config: LitecoinMinerConfig, on_log):
        self.config = config
        self.on_log = on_log

        self._sock: Optional[socket.socket] = None
        self._file = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_lock = threading.Lock()

        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._job_event = threading.Event()

        self.session = LitecoinSession()
        self.current_job: Optional[LitecoinJob] = None

    def _set_connected_state(self, connected: bool, authorized: Optional[bool] = None) -> None:
        with self._state_lock:
            self.session.connected = bool(connected)
            if authorized is not None:
                self.session.authorized = bool(authorized)

    def connect(self) -> None:
        self.close()

        raw_sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=self.config.socket_timeout_s,
        )
        raw_sock.settimeout(self.config.socket_timeout_s)

        if self.config.use_tls:
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw_sock, server_hostname=self.config.host)
        else:
            self._sock = raw_sock

        self._file = self._sock.makefile("r", encoding="utf-8", newline="\n")
        self._stop.clear()
        self._job_event.clear()

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="ltc-stratum-reader",
            daemon=True,
        )
        self._reader_thread.start()

        self._set_connected_state(True, False)
        self.on_log(
            f"[pool] connected host={self.config.host} port={self.config.port} tls={self.config.use_tls}"
        )

    def authorize(self) -> None:
        if self._sock is None:
            raise RuntimeError("socket is not connected")

        sub = self._rpc("mining.subscribe", [self.config.agent], timeout=self.config.rpc_timeout_s)
        self._handle_subscribe_result(sub)

        auth = self._rpc(
            "mining.authorize",
            [self.config.login, self.config.password],
            timeout=self.config.rpc_timeout_s,
        )
        accepted = bool(auth.get("result"))
        self._set_connected_state(True, accepted)

        if not accepted:
            raise RuntimeError(f"authorization failed: {auth}")

        self.on_log(f"[pool] authorized login={self.config.login}")

    def connect_and_authorize(self) -> None:
        self.connect()
        self.authorize()

    def reconnect(self) -> None:
        self.connect_and_authorize()

    def _rpc(self, method: str, params: list[Any], timeout: float) -> dict[str, Any]:
        req_id = self._next_request_id()
        slot: queue.Queue = queue.Queue(maxsize=1)

        with self._pending_lock:
            self._pending[req_id] = slot

        payload = {"id": req_id, "method": method, "params": params}
        self._send_json(payload)

        try:
            msg = slot.get(timeout=timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise TimeoutError(f"rpc timeout for {method}") from exc

        return msg

    def _next_request_id(self) -> int:
        with self._send_lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id

    def _send_json(self, payload: dict[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("socket is not connected")
        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(line)

    def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                line = self._file.readline() if self._file is not None else ""
                if not line:
                    break

                try:
                    msg = json.loads(line)
                except Exception:
                    self.on_log(f"[pool] invalid json: {line.strip()}")
                    continue

                msg_id = msg.get("id", None)
                if msg_id is not None:
                    with self._pending_lock:
                        slot = self._pending.pop(msg_id, None)
                    if slot is not None:
                        slot.put(msg)
                        continue

                method = str(msg.get("method") or "")
                params = msg.get("params") or []

                if method == "mining.set_difficulty":
                    self._handle_set_difficulty(params)
                elif method == "mining.notify":
                    self._handle_notify(params)
                elif method == "mining.set_extranonce":
                    self._handle_set_extranonce(params)
        except Exception as exc:
            self.on_log(f"[pool] reader error: {exc}")
        finally:
            self._set_connected_state(False, False)

    def _handle_subscribe_result(self, msg: dict[str, Any]) -> None:
        result = msg.get("result")
        if not isinstance(result, list) or len(result) < 3:
            raise RuntimeError(f"unexpected subscribe response: {msg}")

        extranonce1 = str(result[1] or "")
        extranonce2_size = int(result[2] or 4)

        with self._state_lock:
            self.session.subscribed = True
            self.session.extranonce1_hex = extranonce1
            self.session.extranonce2_size = extranonce2_size

        self.on_log(
            f"[pool] subscribe extranonce1={extranonce1} extranonce2_size={extranonce2_size}"
        )

    def _handle_set_difficulty(self, params: list[Any]) -> None:
        if not params:
            return
        try:
            diff = float(params[0])
        except Exception:
            return
        with self._state_lock:
            self.session.difficulty = diff
        self.on_log(f"[pool] set_difficulty diff={diff}")

    def _handle_set_extranonce(self, params: list[Any]) -> None:
        if len(params) < 2:
            return

        extranonce1 = str(params[0] or "")
        extranonce2_size = int(params[1] or 4)

        with self._state_lock:
            self.session.extranonce1_hex = extranonce1
            self.session.extranonce2_size = extranonce2_size

        self.on_log(
            f"[pool] set_extranonce extranonce1={extranonce1} extranonce2_size={extranonce2_size}"
        )

    def _handle_notify(self, params: list[Any]) -> None:
        if len(params) < 9:
            self.on_log(f"[pool] malformed mining.notify params={params!r}")
            return

        with self._state_lock:
            difficulty = float(self.session.difficulty)
            extranonce1_hex = self.session.extranonce1_hex
            extranonce2_size = self.session.extranonce2_size

        job = LitecoinJob(
            job_id=str(params[0]),
            prevhash_hex=str(params[1]),
            coinb1_hex=str(params[2]),
            coinb2_hex=str(params[3]),
            merkle_branch_hex=[str(x) for x in (params[4] or [])],
            version_hex=str(params[5]),
            nbits_hex=str(params[6]),
            ntime_hex=str(params[7]),
            clean_jobs=bool(params[8]),
            difficulty=difficulty,
            extranonce1_hex=extranonce1_hex,
            extranonce2_size=extranonce2_size,
        )

        with self._state_lock:
            self.current_job = job

        self._job_event.set()

        self.on_log(
            f"[pool] new_work job_id={job.job_id} diff={job.difficulty} "
            f"ntime={job.ntime_hex} clean={job.clean_jobs}"
        )

    def wait_for_job(self, timeout: Optional[float] = None) -> Optional[LitecoinJob]:
        with self._state_lock:
            if self.current_job is not None:
                return LitecoinJob(**asdict(self.current_job))

        fired = self._job_event.wait(timeout=timeout)
        if not fired:
            return None

        with self._state_lock:
            if self.current_job is None:
                return None
            return LitecoinJob(**asdict(self.current_job))

    def get_latest_job(self) -> Optional[LitecoinJob]:
        with self._state_lock:
            if self.current_job is None:
                return None
            return LitecoinJob(**asdict(self.current_job))

    def get_snapshot(self) -> tuple[LitecoinSession, Optional[LitecoinJob]]:
        with self._state_lock:
            session = LitecoinSession(
                connected=self.session.connected,
                subscribed=self.session.subscribed,
                authorized=self.session.authorized,
                extranonce1_hex=self.session.extranonce1_hex,
                extranonce2_size=self.session.extranonce2_size,
                difficulty=self.session.difficulty,
            )
            job = None
            if self.current_job is not None:
                job = LitecoinJob(**asdict(self.current_job))
            return session, job

    def submit_share(
        self,
        job_id: str,
        extranonce2_hex: str,
        ntime_hex: str,
        nonce_hex: str,
    ) -> SubmitResult:
        msg = self._rpc(
            "mining.submit",
            [self.config.login, job_id, extranonce2_hex, ntime_hex, nonce_hex],
            timeout=self.config.submit_timeout_s,
        )

        if msg.get("error"):
            return SubmitResult(
                accepted=False,
                status="error",
                error=str(msg.get("error")),
                raw=msg,
            )

        accepted = bool(msg.get("result"))
        return SubmitResult(
            accepted=accepted,
            status="accepted" if accepted else "rejected",
            raw=msg,
        )

    def close(self) -> None:
        self._stop.set()
        self._job_event.clear()

        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None

        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None

        with self._pending_lock:
            self._pending.clear()

        with self._state_lock:
            self.session.connected = False
            self.session.subscribed = False
            self.session.authorized = False
            self.current_job = None
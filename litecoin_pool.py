from __future__ import annotations

import json
import queue
import socket
import ssl
import threading
import time
from dataclasses import asdict
from typing import Any, Optional

from litecoin_models import LitecoinJob, LitecoinMinerConfig, LitecoinSession, SubmitResult


class LitecoinStratumClient:
    def __init__(self, config: LitecoinMinerConfig, on_log):
        self.config = config
        self.on_log = on_log

        self._sock: Optional[socket.socket] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_lock = threading.Lock()

        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._job_event = threading.Event()

        self._recv_buffer = bytearray()
        self._reader_error = ""
        self._last_recv_at = 0.0
        self._last_send_at = 0.0

        self.session = LitecoinSession()
        self.current_job: Optional[LitecoinJob] = None

    @property
    def alive(self) -> bool:
        with self._state_lock:
            connected = bool(self.session.connected)
        reader = self._reader_thread
        return connected and reader is not None and reader.is_alive() and not self._stop.is_set()

    @property
    def reader_error(self) -> str:
        with self._state_lock:
            return str(self._reader_error or "")

    @property
    def last_recv_at(self) -> float:
        with self._state_lock:
            return float(self._last_recv_at or 0.0)

    def _set_connected_state(self, connected: bool, authorized: Optional[bool] = None) -> None:
        with self._state_lock:
            self.session.connected = bool(connected)
            if authorized is not None:
                self.session.authorized = bool(authorized)

    def _clone_job(self, job: LitecoinJob) -> LitecoinJob:
        return LitecoinJob(**asdict(job))

    def _next_request_id(self) -> int:
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
            return req_id

    def _fail_all_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()

        for req_id, slot in pending:
            try:
                slot.put_nowait(
                    {
                        "id": req_id,
                        "result": None,
                        "error": reason,
                        "_transport_error": True,
                    }
                )
            except Exception:
                pass

    def _mark_reader_failed(self, reason: str) -> None:
        reason = str(reason or "connection closed").strip() or "connection closed"

        with self._state_lock:
            self._reader_error = reason
            self.session.connected = False
            self.session.authorized = False

        self._fail_all_pending(reason)
        self._job_event.set()

    def connect(self) -> None:
        self.close()

        raw_sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=self.config.socket_timeout_s,
        )

        try:
            raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        try:
            raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        if self.config.use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw_sock, server_hostname=self.config.host)
        else:
            sock = raw_sock

        # Do not leave a long blocking timeout here. The reader loop handles timeouts.
        sock.settimeout(1.0)

        self._sock = sock
        self._stop.clear()
        self._job_event.clear()

        with self._state_lock:
            self._recv_buffer.clear()
            self._reader_error = ""
            self._last_recv_at = time.time()
            self._last_send_at = 0.0
            self.session = LitecoinSession()
            self.current_job = None

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
        if self._sock is None or not self.alive:
            raise ConnectionError(self.reader_error or "socket is not connected")

        req_id = self._next_request_id()
        slot: queue.Queue = queue.Queue(maxsize=1)

        with self._pending_lock:
            self._pending[req_id] = slot

        try:
            payload = {"id": req_id, "method": method, "params": params}
            self._send_json(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._pending_lock:
                    self._pending.pop(req_id, None)
                raise TimeoutError(f"rpc timeout for {method}")

            try:
                msg = slot.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if not self.alive:
                    with self._pending_lock:
                        self._pending.pop(req_id, None)
                    raise ConnectionError(self.reader_error or f"connection lost during {method}")
                continue

            if msg.get("_transport_error"):
                raise ConnectionError(str(msg.get("error") or f"transport error during {method}"))

            return msg

    def _send_json(self, payload: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("socket is not connected")

        line = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")

        try:
            with self._send_lock:
                sock.sendall(line)
            with self._state_lock:
                self._last_send_at = time.time()
        except Exception as exc:
            self._mark_reader_failed(f"send failed: {exc}")
            raise ConnectionError(f"send failed: {exc}") from exc

    def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                sock = self._sock
                if sock is None:
                    break

                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except ssl.SSLWantReadError:
                    continue
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    self._mark_reader_failed(f"reader socket error: {exc}")
                    break
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._mark_reader_failed(f"reader error: {exc}")
                    break

                if not chunk:
                    if self._stop.is_set():
                        break
                    self._mark_reader_failed("pool closed connection")
                    break

                with self._state_lock:
                    self._last_recv_at = time.time()

                self._recv_buffer.extend(chunk)

                while True:
                    newline_index = self._recv_buffer.find(b"\n")
                    if newline_index < 0:
                        break

                    raw_line = bytes(self._recv_buffer[:newline_index]).strip()
                    del self._recv_buffer[: newline_index + 1]

                    if not raw_line:
                        continue

                    try:
                        msg = json.loads(raw_line.decode("utf-8", errors="replace"))
                    except Exception:
                        self.on_log(f"[pool] invalid json: {raw_line[:400]!r}")
                        continue

                    self._dispatch_message(msg)

        finally:
            self._set_connected_state(False, False)

    def _dispatch_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id", None)
        if msg_id is not None:
            try:
                lookup_id = int(msg_id)
            except Exception:
                lookup_id = msg_id

            with self._pending_lock:
                slot = self._pending.pop(lookup_id, None)

            if slot is not None:
                slot.put(msg)
                return

        method = str(msg.get("method") or "")
        params = msg.get("params") or []

        if method == "mining.set_difficulty":
            self._handle_set_difficulty(params)
        elif method == "mining.notify":
            self._handle_notify(params)
        elif method == "mining.set_extranonce":
            self._handle_set_extranonce(params)

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
        fired = self._job_event.wait(timeout=timeout)
        if not fired:
            return None

        self._job_event.clear()

        with self._state_lock:
            if self.current_job is None:
                return None
            return self._clone_job(self.current_job)

    def get_latest_job(self) -> Optional[LitecoinJob]:
        with self._state_lock:
            if self.current_job is None:
                return None
            return self._clone_job(self.current_job)

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
            job = self._clone_job(self.current_job) if self.current_job is not None else None
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
        self._job_event.set()
        self._fail_all_pending("client closed")

        sock = self._sock
        self._sock = None

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

        reader = self._reader_thread
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            try:
                reader.join(timeout=1.0)
            except Exception:
                pass
        self._reader_thread = None

        with self._state_lock:
            self._recv_buffer.clear()
            self.session.connected = False
            self.session.subscribed = False
            self.session.authorized = False
            self.current_job = None
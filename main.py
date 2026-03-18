from __future__ import annotations

import json
import signal
import sys
from pathlib import Path

from litecoin_models import LitecoinMinerConfig
from litecoin_worker import LitecoinMinerWorker


CONFIG_PATH = Path("litecoin_miner_config.json")


def load_config() -> LitecoinMinerConfig:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return LitecoinMinerConfig.from_mapping(raw)
        except Exception as exc:
            print(f"[main] failed to read config, using defaults: {exc}", flush=True)
    return LitecoinMinerConfig()


def main() -> int:
    cfg = load_config()

    worker = LitecoinMinerWorker(
        config=cfg,
        on_log=lambda msg: print(msg, flush=True),
        on_status=lambda status: print(f"[status] {status}", flush=True),
    )

    def _handle_stop(signum, frame):
        print("[main] stop requested", flush=True)
        worker.stop()

    try:
        signal.signal(signal.SIGINT, _handle_stop)
        signal.signal(signal.SIGTERM, _handle_stop)
    except Exception:
        pass

    try:
        print("[main] starting Litecoin miner", flush=True)
        print(
            f"[main] pool={cfg.host}:{cfg.port} "
            f"login={cfg.login} tls={cfg.use_tls} "
            f"dll={cfg.native_dll_path}",
            flush=True,
        )
        worker.run()
        return 0
    except KeyboardInterrupt:
        print("[main] interrupted", flush=True)
        worker.stop()
        return 0
    except Exception as exc:
        print(f"[main] fatal error: {exc}", flush=True)
        worker.stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
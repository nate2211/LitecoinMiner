from __future__ import annotations

import json
import re
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets

from litecoin_models import LitecoinMinerConfig
from litecoin_opencl import OpenCLLitecoinScanner
from litecoin_worker import LitecoinMinerWorker


CONFIG_PATH = Path("litecoin_miner_config.json")


def load_config() -> LitecoinMinerConfig:
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return LitecoinMinerConfig.from_mapping(raw)
        except Exception:
            pass
    return LitecoinMinerConfig()


def save_config(cfg: LitecoinMinerConfig) -> None:
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")


class LogEmitter(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    status = QtCore.pyqtSignal(str)
    finished_ok = QtCore.pyqtSignal()
    finished_error = QtCore.pyqtSignal(str)


class MinerThread(QtCore.QThread):
    def __init__(self, config: LitecoinMinerConfig, parent: Optional[QtCore.QObject] = None) -> None:
        super().__init__(parent)
        self.config = LitecoinMinerConfig.from_mapping(asdict(config))
        self.emitter = LogEmitter()
        self._worker: Optional[LitecoinMinerWorker] = None

    def run(self) -> None:
        try:
            self._worker = LitecoinMinerWorker(
                config=self.config,
                on_log=self.emitter.log.emit,
                on_status=self.emitter.status.emit,
            )
            self._worker.run()
            self.emitter.finished_ok.emit()
        except Exception as exc:
            tb = traceback.format_exc()
            self.emitter.log.emit(tb.rstrip())
            self.emitter.finished_error.emit(str(exc))
        finally:
            self._worker = None

    def stop(self) -> None:
        try:
            if self._worker is not None:
                self._worker.stop()
        except Exception:
            pass


class StatCard(QtWidgets.QFrame):
    def __init__(self, title: str, value: str = "-", parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)

        self.title_label = QtWidgets.QLabel(title)
        self.title_label.setObjectName("StatTitle")

        self.value_label = QtWidgets.QLabel(value)
        self.value_label.setObjectName("StatValue")
        self.value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class MiningGui(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Litecoin Miner")
        self.resize(1380, 860)

        self.cfg = load_config()
        self.worker_thread: Optional[MinerThread] = None
        self.started_at: Optional[float] = None

        self.accepted_count = 0
        self.rejected_count = 0
        self.found_count = 0
        self.last_job_id = "-"
        self.last_nonce = "-"
        self.last_hashrate = "-"
        self.last_status = "idle"

        self._build_ui()
        self._apply_theme()
        self._load_config_into_form()
        self._refresh_devices()

        self.uptime_timer = QtCore.QTimer(self)
        self.uptime_timer.setInterval(1000)
        self.uptime_timer.timeout.connect(self._tick_uptime)
        self.uptime_timer.start()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        header = self._build_header()
        root.addWidget(header)

        stats = self._build_stats_row()
        root.addWidget(stats)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 890])

        root.addWidget(splitter, 1)

    def _build_header(self) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setObjectName("HeaderBox")

        title = QtWidgets.QLabel("Litecoin Mining GUI")
        title.setObjectName("HeaderTitle")

        subtitle = QtWidgets.QLabel("PyQt5 control panel for native/OpenCL mining")
        subtitle.setObjectName("HeaderSubtitle")

        self.status_chip = QtWidgets.QLabel("idle")
        self.status_chip.setObjectName("StatusChip")
        self.status_chip.setAlignment(QtCore.Qt.AlignCenter)
        self.status_chip.setMinimumWidth(110)

        self.start_btn = QtWidgets.QPushButton("Start Mining")
        self.start_btn.clicked.connect(self.start_mining)

        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_mining)
        self.stop_btn.setEnabled(False)

        self.save_btn = QtWidgets.QPushButton("Save Config")
        self.save_btn.clicked.connect(self.save_form_config)

        self.reload_btn = QtWidgets.QPushButton("Reload Config")
        self.reload_btn.clicked.connect(self.reload_form_config)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(title)
        left.addWidget(subtitle)

        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(8)
        btns.addWidget(self.status_chip)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.save_btn)
        btns.addWidget(self.reload_btn)

        layout = QtWidgets.QHBoxLayout(box)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addLayout(left, 1)
        layout.addLayout(btns)

        return box

    def _build_stats_row(self) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        layout = QtWidgets.QGridLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.card_status = StatCard("Status", "idle")
        self.card_hashrate = StatCard("Hashrate", "-")
        self.card_found = StatCard("Found", "0")
        self.card_accepted = StatCard("Accepted", "0")
        self.card_rejected = StatCard("Rejected", "0")
        self.card_uptime = StatCard("Uptime", "00:00:00")
        self.card_job = StatCard("Last Job", "-")
        self.card_nonce = StatCard("Last Nonce", "-")

        cards = [
            self.card_status,
            self.card_hashrate,
            self.card_found,
            self.card_accepted,
            self.card_rejected,
            self.card_uptime,
            self.card_job,
            self.card_nonce,
        ]

        for i, card in enumerate(cards):
            layout.addWidget(card, 0, i)

        return row

    def _build_left_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QScrollArea()
        panel.setWidgetResizable(True)
        panel.setFrameShape(QtWidgets.QFrame.NoFrame)

        content = QtWidgets.QWidget()
        panel.setWidget(content)

        outer = QtWidgets.QVBoxLayout(content)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        outer.addWidget(self._make_connection_group())
        outer.addWidget(self._make_engine_group())
        outer.addWidget(self._make_opencl_group())
        outer.addWidget(self._make_runtime_group())
        outer.addStretch(1)

        return panel

    def _build_right_panel(self) -> QtWidgets.QWidget:
        wrap = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        top = QtWidgets.QFrame()
        top.setObjectName("PanelBox")
        top_layout = QtWidgets.QGridLayout(top)
        top_layout.setContentsMargins(12, 12, 12, 12)
        top_layout.setHorizontalSpacing(14)
        top_layout.setVerticalSpacing(8)

        self.last_job_label = self._selectable_value("-")
        self.last_diff_label = self._selectable_value("-")
        self.last_ntime_label = self._selectable_value("-")
        self.last_backend_label = self._selectable_value("-")
        self.last_target_label = self._selectable_value("-")

        top_layout.addWidget(QtWidgets.QLabel("Current Job"), 0, 0)
        top_layout.addWidget(self.last_job_label, 0, 1)
        top_layout.addWidget(QtWidgets.QLabel("Difficulty"), 1, 0)
        top_layout.addWidget(self.last_diff_label, 1, 1)
        top_layout.addWidget(QtWidgets.QLabel("nTime"), 2, 0)
        top_layout.addWidget(self.last_ntime_label, 2, 1)
        top_layout.addWidget(QtWidgets.QLabel("Backend"), 3, 0)
        top_layout.addWidget(self.last_backend_label, 3, 1)
        top_layout.addWidget(QtWidgets.QLabel("Target / Note"), 4, 0)
        top_layout.addWidget(self.last_target_label, 4, 1)

        log_box = QtWidgets.QFrame()
        log_box.setObjectName("PanelBox")
        log_layout = QtWidgets.QVBoxLayout(log_box)
        log_layout.setContentsMargins(12, 12, 12, 12)
        log_layout.setSpacing(8)

        log_header = QtWidgets.QHBoxLayout()
        log_header.addWidget(QtWidgets.QLabel("Console"))
        log_header.addStretch(1)

        self.clear_log_btn = QtWidgets.QPushButton("Clear")

        self.console = QtWidgets.QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.console.document().setMaximumBlockCount(5000)

        self.clear_log_btn.clicked.connect(self.console.clear)
        log_header.addWidget(self.clear_log_btn)

        log_layout.addLayout(log_header)
        log_layout.addWidget(self.console, 1)

        layout.addWidget(top)
        layout.addWidget(log_box, 1)

        return wrap

    def _make_connection_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Pool Connection")
        box.setObjectName("PanelBox")
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        self.host_edit = QtWidgets.QLineEdit()
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.login_edit = QtWidgets.QLineEdit()
        self.password_edit = QtWidgets.QLineEdit()
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        self.agent_edit = QtWidgets.QLineEdit()
        self.tls_check = QtWidgets.QCheckBox("Use TLS")

        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Login", self.login_edit)
        form.addRow("Password", self.password_edit)
        form.addRow("Agent", self.agent_edit)
        form.addRow("", self.tls_check)

        return box

    def _make_engine_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Miner Engine")
        box.setObjectName("PanelBox")
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        self.native_dll_edit = QtWidgets.QLineEdit()
        self.backend_combo = QtWidgets.QComboBox()
        self.backend_combo.addItems(["native", "opencl"])
        self.backend_combo.currentTextChanged.connect(self._backend_changed)

        self.scan_window_spin = QtWidgets.QSpinBox()
        self.scan_window_spin.setRange(1, 50_000_000)
        self.max_results_spin = QtWidgets.QSpinBox()
        self.max_results_spin.setRange(1, 10000)

        self.browse_dll_btn = QtWidgets.QPushButton("Browse DLL")
        self.browse_dll_btn.clicked.connect(self._browse_dll)

        dll_row = QtWidgets.QHBoxLayout()
        dll_row.addWidget(self.native_dll_edit, 1)
        dll_row.addWidget(self.browse_dll_btn)

        form.addRow("Native DLL", self._wrap_layout(dll_row))
        form.addRow("Backend", self.backend_combo)
        form.addRow("Scan Window", self.scan_window_spin)
        form.addRow("Max Results / Scan", self.max_results_spin)

        return box

    def _make_opencl_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("OpenCL")
        box.setObjectName("PanelBox")
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        self.platform_combo = QtWidgets.QComboBox()
        self.device_combo = QtWidgets.QComboBox()
        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh Devices")
        self.refresh_devices_btn.clicked.connect(self._refresh_devices)
        self.platform_combo.currentIndexChanged.connect(self._populate_device_combo)

        self.kernel_path_edit = QtWidgets.QLineEdit()
        self.kernel_browse_btn = QtWidgets.QPushButton("Browse Kernel")
        self.kernel_browse_btn.clicked.connect(self._browse_kernel)

        self.kernel_name_edit = QtWidgets.QLineEdit()
        self.local_work_size_spin = QtWidgets.QSpinBox()
        self.local_work_size_spin.setRange(1, 4096)
        self.build_options_edit = QtWidgets.QLineEdit()

        plat_row = QtWidgets.QHBoxLayout()
        plat_row.addWidget(self.platform_combo, 1)
        plat_row.addWidget(self.refresh_devices_btn)

        kernel_row = QtWidgets.QHBoxLayout()
        kernel_row.addWidget(self.kernel_path_edit, 1)
        kernel_row.addWidget(self.kernel_browse_btn)

        form.addRow("Platform", self._wrap_layout(plat_row))
        form.addRow("Device", self.device_combo)
        form.addRow("Kernel File", self._wrap_layout(kernel_row))
        form.addRow("Kernel Name", self.kernel_name_edit)
        form.addRow("Local Work Size", self.local_work_size_spin)
        form.addRow("Build Options", self.build_options_edit)

        self.opencl_group = box
        return box

    def _make_runtime_group(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Runtime")
        box.setObjectName("PanelBox")
        form = QtWidgets.QFormLayout(box)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        self.socket_timeout_spin = QtWidgets.QDoubleSpinBox()
        self.socket_timeout_spin.setRange(0.1, 600.0)
        self.socket_timeout_spin.setDecimals(2)

        self.rpc_timeout_spin = QtWidgets.QDoubleSpinBox()
        self.rpc_timeout_spin.setRange(0.1, 600.0)
        self.rpc_timeout_spin.setDecimals(2)

        self.submit_timeout_spin = QtWidgets.QDoubleSpinBox()
        self.submit_timeout_spin.setRange(0.1, 600.0)
        self.submit_timeout_spin.setDecimals(2)

        self.reconnect_delay_spin = QtWidgets.QDoubleSpinBox()
        self.reconnect_delay_spin.setRange(0.1, 600.0)
        self.reconnect_delay_spin.setDecimals(2)

        self.idle_sleep_spin = QtWidgets.QDoubleSpinBox()
        self.idle_sleep_spin.setRange(0.001, 10.0)
        self.idle_sleep_spin.setDecimals(3)

        self.log_hashrate_spin = QtWidgets.QDoubleSpinBox()
        self.log_hashrate_spin.setRange(1.0, 3600.0)
        self.log_hashrate_spin.setDecimals(2)

        form.addRow("Socket Timeout (s)", self.socket_timeout_spin)
        form.addRow("RPC Timeout (s)", self.rpc_timeout_spin)
        form.addRow("Submit Timeout (s)", self.submit_timeout_spin)
        form.addRow("Reconnect Delay (s)", self.reconnect_delay_spin)
        form.addRow("Idle Sleep (s)", self.idle_sleep_spin)
        form.addRow("Hashrate Log Interval (s)", self.log_hashrate_spin)

        return box

    def _selectable_value(self, text: str) -> QtWidgets.QLabel:
        lbl = QtWidgets.QLabel(text)
        lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        lbl.setWordWrap(True)
        return lbl

    def _wrap_layout(self, layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        w.setLayout(layout)
        return w

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #111418;
                color: #e6edf3;
                font-size: 13px;
            }
            QFrame#HeaderBox, QFrame#PanelBox, QGroupBox#PanelBox, QFrame#StatCard {
                background: #171b21;
                border: 1px solid #2a313c;
                border-radius: 10px;
            }
            QGroupBox#PanelBox {
                margin-top: 8px;
                padding-top: 8px;
                font-weight: 600;
            }
            QLabel#HeaderTitle {
                font-size: 24px;
                font-weight: 700;
            }
            QLabel#HeaderSubtitle {
                color: #9aa4b2;
                font-size: 12px;
            }
            QLabel#StatusChip {
                background: #2b3340;
                border: 1px solid #3a4657;
                border-radius: 14px;
                padding: 6px 12px;
                font-weight: 700;
            }
            QLabel#StatTitle {
                color: #97a3b6;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#StatValue {
                font-size: 18px;
                font-weight: 700;
            }
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #0f1318;
                border: 1px solid #313a47;
                border-radius: 8px;
                padding: 6px 8px;
                selection-background-color: #2f81f7;
            }
            QPushButton {
                background: #1f6feb;
                border: 1px solid #2f81f7;
                border-radius: 8px;
                padding: 8px 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #2f81f7;
            }
            QPushButton:disabled {
                background: #273140;
                color: #8b98ab;
                border-color: #334155;
            }
            QCheckBox {
                spacing: 8px;
            }
            QScrollArea {
                border: none;
            }
            """
        )

    # ---------- config ----------
    def _load_config_into_form(self) -> None:
        cfg = self.cfg
        self.host_edit.setText(cfg.host)
        self.port_spin.setValue(int(cfg.port))
        self.login_edit.setText(cfg.login)
        self.password_edit.setText(cfg.password)
        self.agent_edit.setText(cfg.agent)
        self.tls_check.setChecked(bool(cfg.use_tls))

        self.native_dll_edit.setText(cfg.native_dll_path)
        self.backend_combo.setCurrentText(cfg.scan_backend)
        self.scan_window_spin.setValue(int(cfg.scan_window_nonces))
        self.max_results_spin.setValue(int(cfg.max_results_per_scan))

        self.kernel_path_edit.setText(cfg.kernel_path)
        self.kernel_name_edit.setText(cfg.opencl_kernel_name)
        self.local_work_size_spin.setValue(int(cfg.local_work_size))
        self.build_options_edit.setText(cfg.build_options)

        self.socket_timeout_spin.setValue(float(cfg.socket_timeout_s))
        self.rpc_timeout_spin.setValue(float(cfg.rpc_timeout_s))
        self.submit_timeout_spin.setValue(float(cfg.submit_timeout_s))
        self.reconnect_delay_spin.setValue(float(cfg.reconnect_delay_s))
        self.idle_sleep_spin.setValue(float(cfg.idle_sleep_s))
        self.log_hashrate_spin.setValue(float(cfg.log_hashrate_interval_s))

        self._backend_changed(self.backend_combo.currentText())

    def _read_form_into_config(self) -> LitecoinMinerConfig:
        raw = {
            "host": self.host_edit.text().strip(),
            "port": int(self.port_spin.value()),
            "login": self.login_edit.text().strip(),
            "password": self.password_edit.text(),
            "agent": self.agent_edit.text().strip(),
            "use_tls": bool(self.tls_check.isChecked()),
            "native_dll_path": self.native_dll_edit.text().strip(),
            "scan_backend": self.backend_combo.currentText().strip().lower(),
            "scan_window_nonces": int(self.scan_window_spin.value()),
            "max_results_per_scan": int(self.max_results_spin.value()),
            "platform_index": max(0, int(self.platform_combo.currentData() or 0)),
            "device_index": max(0, int(self.device_combo.currentData() or 0)),
            "kernel_path": self.kernel_path_edit.text().strip(),
            "opencl_kernel_name": self.kernel_name_edit.text().strip(),
            "local_work_size": int(self.local_work_size_spin.value()),
            "build_options": self.build_options_edit.text(),
            "socket_timeout_s": float(self.socket_timeout_spin.value()),
            "rpc_timeout_s": float(self.rpc_timeout_spin.value()),
            "submit_timeout_s": float(self.submit_timeout_spin.value()),
            "reconnect_delay_s": float(self.reconnect_delay_spin.value()),
            "idle_sleep_s": float(self.idle_sleep_spin.value()),
            "log_hashrate_interval_s": float(self.log_hashrate_spin.value()),
        }
        return LitecoinMinerConfig.from_mapping(raw)

    def save_form_config(self) -> None:
        try:
            self.cfg = self._read_form_into_config()
            save_config(self.cfg)
            self._append_log("[gui] config saved")
        except Exception as exc:
            self._append_log(f"[gui] failed to save config: {exc}")

    def reload_form_config(self) -> None:
        self.cfg = load_config()
        self._load_config_into_form()
        self._refresh_devices()
        self._append_log("[gui] config reloaded")

    # ---------- device handling ----------
    def _refresh_devices(self) -> None:
        current_platform = self.platform_combo.currentData()
        current_device = self.device_combo.currentData()

        self.platform_combo.blockSignals(True)
        self.device_combo.blockSignals(True)

        self.platform_combo.clear()
        self.device_combo.clear()

        try:
            devices = OpenCLLitecoinScanner.list_devices()
        except Exception as exc:
            devices = []
            self._append_log(f"[gui] OpenCL device enumeration failed: {exc}")

        grouped: dict[int, tuple[str, list[tuple[int, str]]]] = {}
        for item in devices:
            plat_name, dev_list = grouped.setdefault(item.platform_index, (item.platform_name, []))
            dev_list.append((item.device_index, item.device_name))

        for platform_index, (platform_name, _) in grouped.items():
            self.platform_combo.addItem(f"{platform_index}: {platform_name}", platform_index)

        if self.platform_combo.count() == 0:
            self.platform_combo.addItem("No OpenCL devices found", 0)
            self.device_combo.addItem("No devices", 0)
        else:
            target_platform = (
                current_platform if current_platform is not None else getattr(self.cfg, "platform_index", 0)
            )
            index = self.platform_combo.findData(target_platform)
            self.platform_combo.setCurrentIndex(index if index >= 0 else 0)

        self.platform_combo.blockSignals(False)
        self.device_combo.blockSignals(False)

        self._populate_device_combo()

    def _populate_device_combo(self) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        try:
            devices = OpenCLLitecoinScanner.list_devices()
        except Exception:
            devices = []

        platform_index = int(self.platform_combo.currentData() or 0)
        matching = [d for d in devices if d.platform_index == platform_index]

        if not matching:
            self.device_combo.addItem("No devices", 0)
            self.device_combo.blockSignals(False)
            return

        for item in matching:
            self.device_combo.addItem(f"{item.device_index}: {item.device_name}", item.device_index)

        target_device = getattr(self.cfg, "device_index", 0)
        dindex = self.device_combo.findData(target_device)
        self.device_combo.setCurrentIndex(dindex if dindex >= 0 else 0)
        self.device_combo.blockSignals(False)

    def _backend_changed(self, backend: str) -> None:
        use_opencl = str(backend).strip().lower() == "opencl"
        self.opencl_group.setEnabled(use_opencl)

    def _browse_dll(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Native DLL",
            self.native_dll_edit.text().strip() or str(Path.cwd()),
            "DLL Files (*.dll);;All Files (*)",
        )
        if path:
            self.native_dll_edit.setText(path)

    def _browse_kernel(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select OpenCL Kernel",
            self.kernel_path_edit.text().strip() or str(Path.cwd()),
            "OpenCL Files (*.cl);;All Files (*)",
        )
        if path:
            self.kernel_path_edit.setText(path)

    # ---------- worker ----------
    def start_mining(self) -> None:
        if self.worker_thread is not None and self.worker_thread.isRunning():
            return

        try:
            self.cfg = self._read_form_into_config()
            save_config(self.cfg)
        except Exception as exc:
            self._append_log(f"[gui] invalid config: {exc}")
            return

        self.accepted_count = 0
        self.rejected_count = 0
        self.found_count = 0
        self.last_job_id = "-"
        self.last_nonce = "-"
        self.last_hashrate = "-"
        self.started_at = time.time()

        self.card_accepted.set_value("0")
        self.card_rejected.set_value("0")
        self.card_found.set_value("0")
        self.card_hashrate.set_value("-")
        self.card_job.set_value("-")
        self.card_nonce.set_value("-")
        self.last_job_label.setText("-")
        self.last_diff_label.setText("-")
        self.last_ntime_label.setText("-")
        self.last_backend_label.setText(self.cfg.scan_backend)
        self.last_target_label.setText("-")

        self.worker_thread = MinerThread(self.cfg, self)
        self.worker_thread.emitter.log.connect(self._handle_log)
        self.worker_thread.emitter.status.connect(self._handle_status)
        self.worker_thread.emitter.finished_ok.connect(self._handle_finished_ok)
        self.worker_thread.emitter.finished_error.connect(self._handle_finished_error)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_status("starting")
        self._append_log("[gui] starting miner...")
        self.worker_thread.start()

    def stop_mining(self) -> None:
        if self.worker_thread is None:
            return
        self._append_log("[gui] stop requested")
        self._set_status("stopping")
        self.stop_btn.setEnabled(False)
        self.worker_thread.stop()

    def _handle_finished_ok(self) -> None:
        self._append_log("[gui] miner stopped")
        self._set_status("stopped")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker_thread = None

    def _handle_finished_error(self, error: str) -> None:
        self._append_log(f"[gui] miner exited with error: {error}")
        self._set_status("error")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker_thread = None

    def _handle_status(self, status: str) -> None:
        self._set_status(status)
        self._append_log(f"[status] {status}")

    # ---------- log parsing ----------
    def _handle_log(self, msg: str) -> None:
        self._append_log(msg)
        self._parse_log_for_stats(msg)

    def _append_log(self, msg: str) -> None:
        self.console.appendPlainText(str(msg))
        bar = self.console.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_status(self, status: str) -> None:
        text = str(status or "").strip() or "idle"
        self.last_status = text
        self.status_chip.setText(text)
        self.card_status.set_value(text)

        color = "#2b3340"
        border = "#3a4657"
        if text == "running":
            color = "#143d2b"
            border = "#238636"
        elif text in {"starting", "connecting", "reconnecting", "stopping"}:
            color = "#4a3216"
            border = "#d29922"
        elif text == "error":
            color = "#4b1d1d"
            border = "#da3633"

        self.status_chip.setStyleSheet(
            f"background:{color}; border:1px solid {border}; border-radius:14px; padding:6px 12px; font-weight:700;"
        )

    def _parse_log_for_stats(self, msg: str) -> None:
        text = str(msg)

        if text.startswith("[hashrate]"):
            m = re.search(r"current=([^\s]+(?:\s+[kMGT]?H/s))", text)
            if m:
                self.last_hashrate = m.group(1)
                self.card_hashrate.set_value(self.last_hashrate)

        if "[submit] accepted" in text:
            self.accepted_count += 1
            self.card_accepted.set_value(str(self.accepted_count))

        if "[submit] rejected" in text:
            self.rejected_count += 1
            self.card_rejected.set_value(str(self.rejected_count))

        if text.startswith("[share] found"):
            self.found_count += 1
            self.card_found.set_value(str(self.found_count))

            nonce_match = re.search(r"nonce=([0-9a-fA-F]+)", text)
            if nonce_match:
                self.last_nonce = nonce_match.group(1)
                self.card_nonce.set_value(self.last_nonce)

        if text.startswith("[pool] new_work"):
            job_match = re.search(r"job_id=([^\s]+)", text)
            diff_match = re.search(r"diff=([^\s]+)", text)
            ntime_match = re.search(r"ntime=([^\s]+)", text)

            if job_match:
                self.last_job_id = job_match.group(1)
                self.card_job.set_value(self.last_job_id)
                self.last_job_label.setText(self.last_job_id)
            if diff_match:
                self.last_diff_label.setText(diff_match.group(1))
            if ntime_match:
                self.last_ntime_label.setText(ntime_match.group(1))

        if text.startswith("[worker] scanner="):
            scanner = text.split("=", 1)[1].strip()
            self.last_backend_label.setText(scanner)

        if "[share] block-candidate" in text:
            self.last_target_label.setText("block candidate found")

    def _tick_uptime(self) -> None:
        if self.started_at is None:
            self.card_uptime.set_value("00:00:00")
            return
        elapsed = max(0, int(time.time() - self.started_at))
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        self.card_uptime.set_value(f"{h:02d}:{m:02d}:{s:02d}")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        try:
            self.save_form_config()
        except Exception:
            pass

        if self.worker_thread is not None and self.worker_thread.isRunning():
            self.worker_thread.stop()
            self.worker_thread.wait(4000)

        super().closeEvent(event)


def main() -> int:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Litecoin Miner")
    app.setOrganizationName("nate2211")

    win = MiningGui()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
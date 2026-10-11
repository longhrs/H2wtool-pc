from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDockWidget, QLabel, QMainWindow, QMessageBox, QSystemTrayIcon, QTabWidget,
)

from .. import __version__, protocol
from ..core.adb import Adb, AdbAutoReverse
from ..core.config_schema import PresetStore, ToolSchema
from ..core.hub import Event, Hub
from ..core.netutil import local_ipv4_addresses
from ..core.settings import Settings
from .bridge import EventBridge
from .config_panel import ConfigPanel
from .devices_panel import DevicesPanel
from .dialogs import SerialDialog, SettingsDialog
from .log_panel import LogPanel
from .results_panel import ResultsPanel


class MainWindow(QMainWindow):
    def __init__(self, hub: Hub, settings: Settings, data_dir: Path, schemas: dict[str, ToolSchema]):
        super().__init__()
        self.hub = hub
        self.settings = settings
        self.data_dir = data_dir
        self.schemas = schemas
        self.adb = Adb(settings.adb_path)
        self.adb_auto: AdbAutoReverse | None = None
        self.setWindowTitle(f"H2wTool PC  v{__version__}")
        self.resize(1360, 820)

        self.bridge = EventBridge()
        self.bridge.hub_event.connect(self._on_hub_event)
        self.bridge.adb_event.connect(self._on_adb_event)
        hub.add_listener(self.bridge.on_hub_event)

        # 左侧设备
        self.devices = DevicesPanel(hub)
        dock = QDockWidget("设备", self)
        dock.setObjectName("devices")
        dock.setWidget(self.devices)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # 中间标签页
        self.tabs = QTabWidget()
        self.results = ResultsPanel(hub.store, {t: s.title for t, s in schemas.items()})
        self.tabs.addTab(self.results, "测试结果")
        presets = PresetStore(data_dir / "presets")
        self.config_panels: dict[str, ConfigPanel] = {}
        for tool, schema in schemas.items():
            p = ConfigPanel(schema, hub, presets, self.devices.target_keys, self.log)
            self.config_panels[tool] = p
            self.tabs.addTab(p, f"{schema.title} 设置")
        self.logs = LogPanel()
        self.tabs.addTab(self.logs, "日志")
        self.setCentralWidget(self.tabs)
        self.resizeDocks([dock], [520], Qt.Horizontal)

        self._build_toolbar()
        self.status_server = QLabel()
        self.status_ips = QLabel()
        self.status_adb = QLabel()
        for w in (self.status_server, self.status_ips, self.status_adb):
            self.statusBar().addPermanentWidget(w)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

        self.tray = QSystemTrayIcon(self.windowIcon(), self) if QSystemTrayIcon.isSystemTrayAvailable() else None

        if settings.auto_start_server:
            self.start_server()
        self._apply_adb_auto()
        self._update_status()

    # ---------------------------------------------------------------- UI
    def _build_toolbar(self) -> None:
        tb = self.addToolBar("main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        self.act_server = QAction("启动服务", self)
        self.act_server.triggered.connect(self.toggle_server)
        tb.addAction(self.act_server)
        act_serial = QAction("打开串口", self)
        act_serial.triggered.connect(self.open_serial)
        tb.addAction(act_serial)
        act_adb = QAction("adb 端口映射", self)
        act_adb.setToolTip("立即扫描 USB 设备并执行 adb reverse")
        act_adb.triggered.connect(self.adb_reverse_now)
        tb.addAction(act_adb)
        tb.addSeparator()
        act_settings = QAction("设置", self)
        act_settings.triggered.connect(self.open_settings)
        tb.addAction(act_settings)
        act_dir = QAction("打开数据目录", self)
        act_dir.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir))))
        tb.addAction(act_dir)
        act_help = QAction("连接说明", self)
        act_help.triggered.connect(self.show_help)
        tb.addAction(act_help)

    def log(self, level: str, text: str, source: str = "") -> None:
        self.logs.append(level, text, source)
        if level == "error":
            self.statusBar().showMessage(text, 8000)

    def _update_status(self) -> None:
        if self.hub.server_running:
            self.status_server.setText(f"● 监听 {self.settings.bind_host}:{self.hub.tcp_port}")
            self.status_server.setStyleSheet("color: #2e7d32;")
            self.act_server.setText("停止服务")
        else:
            self.status_server.setText("○ 服务未启动")
            self.status_server.setStyleSheet("color: #c62828;")
            self.act_server.setText("启动服务")
        ips = local_ipv4_addresses()
        self.status_ips.setText("本机 IP: " + (", ".join(ips) if ips else "未知"))
        self.status_adb.setText("adb: " + ("已找到" if self.adb.path else "未找到"))

    def _tick(self) -> None:
        # 刷新“最后活动”列
        self.devices.refresh_all()

    # ---------------------------------------------------------------- 服务
    def start_server(self) -> None:
        s = self.settings
        self.hub.heartbeat_timeout_s = s.heartbeat_timeout_s
        try:
            self.hub.start_server(s.bind_host, s.tcp_port, s.discovery_enabled, s.discovery_port)
        except OSError as e:
            self.log("error", f"启动服务失败 {s.bind_host}:{s.tcp_port}: {e}")
            QMessageBox.critical(self, "启动服务失败", f"无法监听 {s.bind_host}:{s.tcp_port}\n{e}")
        self._update_status()

    def toggle_server(self) -> None:
        if self.hub.server_running:
            self.hub.stop_server()
            self._update_status()
        else:
            self.start_server()

    def open_serial(self) -> None:
        dlg = SerialDialog(self.settings.serial_baudrate, self)
        if dlg.exec() != QDialog.Accepted:
            return
        port, baud = dlg.selected()
        if not port:
            return
        try:
            if dlg.selected_mux():
                self.hub.open_serial_mux(port, baud)
            else:
                self.hub.open_serial(port, baud)
        except Exception as e:
            QMessageBox.critical(self, "打开串口失败", f"{port}: {e}")

    def _apply_adb_auto(self) -> None:
        if self.adb_auto:
            self.adb_auto.stop()
            self.adb_auto = None
        if self.settings.adb_auto_reverse:
            self.adb_auto = AdbAutoReverse(self.adb, lambda: self.hub.tcp_port or self.settings.tcp_port,
                                           self.bridge.on_adb_event)
            self.adb_auto.start()

    def adb_reverse_now(self) -> None:
        port = self.hub.tcp_port or self.settings.tcp_port
        try:
            devices = self.adb.devices()
        except Exception as e:
            QMessageBox.warning(self, "adb", str(e))
            return
        self.devices.set_adb_devices(devices)
        ok = 0
        for d in devices:
            if d.state != "device":
                self.log("warn", f"adb 设备 {d.serial} 状态为 {d.state}，请在手机上允许 USB 调试")
                continue
            try:
                self.adb.reverse(d.serial, port)
                ok += 1
                self.log("info", f"adb reverse tcp:{port} 已建立: {d.serial} {d.model}")
            except Exception as e:
                self.log("error", f"adb reverse 失败 {d.serial}: {e}")
        self.statusBar().showMessage(f"adb: 发现 {len(devices)} 台设备，成功映射 {ok} 台", 5000)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.result_settings(self.settings)
        server_changed = (new.bind_host, new.tcp_port, new.discovery_enabled, new.discovery_port) != (
            self.settings.bind_host, self.settings.tcp_port, self.settings.discovery_enabled,
            self.settings.discovery_port)
        self.settings = new
        new.save(self.data_dir / "settings.json")
        self.hub.heartbeat_timeout_s = new.heartbeat_timeout_s
        self.adb.custom_path = new.adb_path
        if server_changed and self.hub.server_running:
            self.start_server()
        self._apply_adb_auto()
        self._update_status()

    def show_help(self) -> None:
        port = self.hub.tcp_port or self.settings.tcp_port
        ips = ", ".join(local_ipv4_addresses()) or "<PC IP>"
        QMessageBox.information(self, "连接说明", (
            f"Android 端支持三种方式连接本工具 (协议 v{protocol.PROTOCOL_VERSION}，JSON Lines)：\n\n"
            f"1. 网络 (WiFi / 网卡)：连接 {ips} 端口 {port}；\n"
            f"   或向 UDP 端口 {self.settings.discovery_port} 广播 \"{protocol.DISCOVERY_MAGIC}\" 自动发现 PC。\n\n"
            f"2. USB (adb)：手机开启 USB 调试并连接 PC，本工具会自动执行\n"
            f"   adb reverse tcp:{port} tcp:{port}，手机端连接 127.0.0.1:{port} 即可。\n\n"
            f"3. USB 串口 (CDC/gadget serial)：点击“打开串口”选择端口。\n\n"
            f"协议详情见 docs/PROTOCOL.md。"
        ))

    # ---------------------------------------------------------------- 事件
    def _on_adb_event(self, level: str, text: str, devices) -> None:
        if level == "devices":
            self.devices.set_adb_devices(devices)
        else:
            self.log(level, text, "adb")
        self.status_adb.setText("adb: " + ("已找到" if self.adb.path else "未找到"))

    def _on_hub_event(self, ev: Event) -> None:
        k, d, dev = ev.kind, ev.data, ev.device
        src = dev.display_id if dev else ""
        if k in ("info", "error"):
            self.log(k, d["text"])
            if k == "info":
                self._update_status()
        elif k in ("device_added", "device_updated"):
            self.devices.upsert(dev)
        elif k == "device_removed":
            self.devices.remove(dev)
        elif k == "result":
            row = d["row"]
            self.results.add_result(row)
            level = "error" if row["result"] != "PASS" else "result"
            self.log(level, f"{row['tool']} 结果 {row['result']} test_id={row['test_id']}", src)
            if row["result"] != "PASS" and self.tray and self.isMinimized():
                self.tray.showMessage("测试失败", f"{src} {row['tool']} {row['result']}")
        elif k == "status":
            m = d["msg"]
            if m.get("state") == d.get("prev_state"):
                return  # 进度更新只刷新设备列表，状态变化才写日志
            text = f"{d['tool']} 状态 {m.get('state')}"
            if m.get("progress") is not None:
                text += f" {m.get('progress')}%"
            if m.get("message"):
                text += f" {m.get('message')}"
            self.log("info", text, src)
        elif k == "log":
            self.log(d["level"], f"{('[' + d['tool'] + '] ') if d['tool'] else ''}{d['text']}", src)
        elif k == "config_report":
            self.log("info", f"收到 {d['tool']} 当前参数: {json.dumps(d['config'], ensure_ascii=False)}", src)
            panel = self.config_panels.get(d["tool"])
            if panel:
                panel.on_config_report(dev.key, d["config"])
        elif k == "ack":
            req = d["request"]
            desc = req.get("desc", "请求")
            if d["ok"]:
                self.log("info", f"{desc}: 设备已确认", src)
            else:
                self.log("error", f"{desc}: 设备拒绝 - {d.get('error')}", src)
        elif k == "file":
            self.log("info", f"收到文件 {d['path']} ({d['size']} 字节)", src)

    def closeEvent(self, event) -> None:
        if self.adb_auto:
            self.adb_auto.stop()
        self.hub.remove_listener(self.bridge.on_hub_event)
        self.hub.shutdown()
        super().closeEvent(event)


from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QStackedWidget, QWidget,
)

from ..core.settings import Settings
from ..transport import list_serial_ports, serial_available


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("高级设置")
        s = settings
        self.host = QLineEdit(s.bind_host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(s.tcp_port)
        self.auto_start = QCheckBox("启动时自动开启服务")
        self.auto_start.setChecked(s.auto_start_server)
        self.discovery = QCheckBox("启用 UDP 局域网发现")
        self.discovery.setChecked(s.discovery_enabled)
        self.discovery_port = QSpinBox()
        self.discovery_port.setRange(1, 65535)
        self.discovery_port.setValue(s.discovery_port)
        self.hb = QSpinBox()
        self.hb.setRange(0, 3600)
        self.hb.setValue(s.heartbeat_timeout_s)
        self.hb.setSuffix(" 秒 (0=不检测)")
        self.adb_path = QLineEdit(s.adb_path)
        self.adb_path.setPlaceholderText("留空则从 PATH / ANDROID_HOME 查找")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_adb)
        adb_row = QWidget()
        h = QHBoxLayout(adb_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.adb_path, 1)
        h.addWidget(browse)
        self.adb_auto = QCheckBox("自动对 USB 设备执行 adb reverse")
        self.adb_auto.setChecked(s.adb_auto_reverse)
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(["9600", "57600", "115200", "230400", "460800", "921600", "1500000", "3000000"])
        self.baud.setCurrentText(str(s.serial_baudrate))

        form = QFormLayout(self)
        form.addRow("监听地址", self.host)
        form.addRow("TCP 端口", self.port)
        form.addRow("", self.auto_start)
        form.addRow("", self.discovery)
        form.addRow("UDP 发现端口", self.discovery_port)
        form.addRow("心跳超时", self.hb)
        form.addRow("adb 路径", adb_row)
        form.addRow("", self.adb_auto)
        form.addRow("串口默认波特率", self.baud)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _browse_adb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 adb 可执行文件")
        if path:
            self.adb_path.setText(path)

    def result_settings(self, base: Settings) -> Settings:
        s = Settings(**vars(base))
        s.bind_host = self.host.text().strip() or "0.0.0.0"
        s.tcp_port = self.port.value()
        s.auto_start_server = self.auto_start.isChecked()
        s.discovery_enabled = self.discovery.isChecked()
        s.discovery_port = self.discovery_port.value()
        s.heartbeat_timeout_s = self.hb.value()
        s.adb_path = self.adb_path.text().strip()
        s.adb_auto_reverse = self.adb_auto.isChecked()
        try:
            s.serial_baudrate = int(self.baud.currentText())
        except ValueError:
            pass
        return s


class SerialDialog(QDialog):
    def __init__(self, baudrate: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("打开 USB 串口")
        self.port = QComboBox()
        self.port.setEditable(True)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.port, 1)
        h.addWidget(refresh)
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(["9600", "57600", "115200", "230400", "460800", "921600", "1500000", "3000000"])
        self.baud.setCurrentText(str(baudrate))
        form = QFormLayout(self)
        if not serial_available():
            form.addRow("提示", QLabel("未安装 pyserial，无法使用串口 (pip install pyserial)"))
        form.addRow("串口", row)
        form.addRow("波特率", self.baud)
        self.mux = QCheckBox("SerialMux 汇聚器 (按源端口拆成多台设备)")
        self.mux.setToolTip("串口另一端是 SerialMux 汇聚器的硬件串口或 USB 虚拟串口输出 (A5 5A 帧格式) 时勾选；\n"
                            "网络 / adb 方式请用工具栏“连接汇聚器”")
        form.addRow("", self.mux)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self._refresh()

    def _refresh(self) -> None:
        self.port.clear()
        for dev, desc in list_serial_ports():
            self.port.addItem(f"{dev}  {desc}".strip(), dev)

    def selected_mux(self) -> bool:
        return self.mux.isChecked()

    def selected(self) -> tuple[str, int]:
        text = self.port.currentText().strip()
        port = self.port.currentData() or (text.split()[0] if text else "")
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            baud = 115200
        return port, baud


class MuxDialog(QDialog):
    """连接 SerialMux 汇聚器 (longhrs/uart)：网络 (两种方向)、USB adb forward、串口 / USB 虚拟串口。"""

    def __init__(self, last: dict, baudrate: int, adb_devices: list, local_ips: list[str],
                 auto_connect: bool, parent=None):
        super().__init__(parent)
        from ..core.mux import MODES
        from ..transport import DEFAULT_MUX_TCP_PORT

        self.setWindowTitle("连接汇聚器 (SerialMux)")
        self.setMinimumWidth(520)
        last = dict(last or {})
        self.mode = QComboBox()
        for key, text in MODES.items():
            self.mode.addItem(text, key)
        self.stack = QStackedWidget()

        def port_box(value) -> QSpinBox:
            sb = QSpinBox()
            sb.setRange(1, 65535)
            sb.setValue(int(value or DEFAULT_MUX_TCP_PORT))
            return sb

        def page(rows, hint: str) -> QWidget:
            w = QWidget()
            f = QFormLayout(w)
            f.setContentsMargins(0, 0, 0, 0)
            for label, widget in rows:
                f.addRow(label, widget)
            tip = QLabel(hint)
            tip.setWordWrap(True)
            tip.setStyleSheet("color: #666;")
            f.addRow(tip)
            return w

        # 网络：PC 连接汇聚器
        self.tcp_host = QLineEdit(last.get("host", "") if last.get("mode") == "tcp" else "")
        self.tcp_host.setPlaceholderText("汇聚器界面上显示的 IP，例如 192.168.1.20")
        self.tcp_port = port_box(last.get("port") if last.get("mode") == "tcp" else None)
        self.stack.addWidget(page(
            [("汇聚器 IP", self.tcp_host), ("端口", self.tcp_port)],
            "汇聚器 App 里“转发到”勾选网络，模式选 TCP 服务端 (默认端口 9000)。断线后每 3 秒自动重连。"))

        # 网络：等待汇聚器连接
        self.listen_port = port_box(last.get("port") if last.get("mode") == "listen" else None)
        ips = ", ".join(local_ips) or "本机 IP"
        self.stack.addWidget(page(
            [("监听端口", self.listen_port)],
            f"汇聚器 App 里网络模式选 TCP 客户端，PC 地址填 {ips}，端口填这里的端口。"
            "可同时接多台汇聚器 (按 IP 区分)。首次使用 Windows 防火墙可能会弹窗，请选择允许。"))

        # USB adb forward
        self.adb_serial = QComboBox()
        self.adb_serial.setEditable(True)
        self.adb_serial.addItem("(只连了一台 adb 设备时留空)", "")
        for d in adb_devices:
            self.adb_serial.addItem(f"{d.serial}  {d.model}  [{d.state}]".strip(), d.serial)
        if last.get("mode") == "adb" and last.get("adb_serial"):
            i = self.adb_serial.findData(last["adb_serial"])
            if i >= 0:
                self.adb_serial.setCurrentIndex(i)
            else:
                self.adb_serial.setEditText(last["adb_serial"])
        self.adb_port = port_box(last.get("port") if last.get("mode") == "adb" else None)
        self.adb_local = port_box(last.get("local_port") if last.get("mode") == "adb" else None)
        self.stack.addWidget(page(
            [("adb 设备", self.adb_serial), ("汇聚器端口", self.adb_port), ("PC 本地端口", self.adb_local)],
            "板子没有 USB 虚拟串口 (gadget) 时用这种方式：汇聚器开启网络 TCP 服务端，用 USB 线连 PC，"
            "本工具自动执行 adb forward 并连接 127.0.0.1。同时接多台板子时每台用不同的本地端口。"))

        # 串口 / USB 虚拟串口
        self.serial_port = QComboBox()
        self.serial_port.setEditable(True)
        for dev, desc in list_serial_ports():
            self.serial_port.addItem(f"{dev}  {desc}".strip(), dev)
        if last.get("mode") == "serial" and last.get("serial_port"):
            i = self.serial_port.findData(last["serial_port"])
            if i >= 0:
                self.serial_port.setCurrentIndex(i)
        self.serial_baud = QComboBox()
        self.serial_baud.setEditable(True)
        self.serial_baud.addItems(["115200", "230400", "460800", "921600", "1500000", "3000000"])
        self.serial_baud.setCurrentText(str(last.get("baud") or baudrate))
        rows = [("串口", self.serial_port), ("波特率", self.serial_baud)]
        if not serial_available():
            rows.insert(0, ("提示", QLabel("未安装 pyserial，无法使用串口 (pip install pyserial)")))
        self.stack.addWidget(page(
            rows,
            "汇聚器的 USB 虚拟串口 (/dev/ttyGS0) 在 Windows 上显示为“USB 串行设备 (COMx)”，波特率无所谓；"
            "硬件串口 (/dev/ttyS32) 的波特率要和汇聚器设置一致。"))

        self.mode.currentIndexChanged.connect(self.stack.setCurrentIndex)
        i = self.mode.findData(last.get("mode", "tcp"))
        self.mode.setCurrentIndex(max(i, 0))
        self.stack.setCurrentIndex(max(i, 0))

        self.auto = QCheckBox("下次启动本工具时自动用这个方式连接")
        self.auto.setChecked(auto_connect)

        form = QFormLayout(self)
        form.addRow("连接方式", self.mode)
        form.addRow(self.stack)
        form.addRow("", self.auto)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def config(self) -> dict:
        mode = self.mode.currentData()
        if mode == "tcp":
            from ..core.mux import parse_host_port
            text = self.tcp_host.text().strip()
            host, port = parse_host_port(text, self.tcp_port.value()) if text else ("", self.tcp_port.value())
            return {"mode": "tcp", "host": host, "port": port}
        if mode == "listen":
            return {"mode": "listen", "port": self.listen_port.value()}
        if mode == "adb":
            text = self.adb_serial.currentText().strip()
            serial = self.adb_serial.currentData()
            if serial is None or self.adb_serial.itemText(self.adb_serial.currentIndex()) != text:
                serial = "" if text.startswith("(") else text.split()[0] if text else ""
            return {"mode": "adb", "adb_serial": serial, "port": self.adb_port.value(),
                    "local_port": self.adb_local.value()}
        text = self.serial_port.currentText().strip()
        sp = self.serial_port.currentData() or (text.split()[0] if text else "")
        try:
            baud = int(self.serial_baud.currentText())
        except ValueError:
            baud = 115200
        return {"mode": "serial", "serial_port": sp, "baud": baud}

    def auto_connect(self) -> bool:
        return self.auto.isChecked()

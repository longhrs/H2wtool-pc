from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QWidget,
)

from ..core.settings import Settings
from ..transport import list_serial_ports, serial_available


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
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
        self.mux = QCheckBox("SerialMux 汇聚口 (按源端口拆成多台设备)")
        self.mux.setToolTip("串口另一端是 SerialMux App 的输出口 (A5 5A 帧格式) 时勾选")
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

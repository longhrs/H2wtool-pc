"""已连接设备列表 + adb USB 设备列表。"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHeaderView, QMenu, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.hub import Device, Hub

TRANSPORT_NAMES = {"network": "网络", "usb-adb": "USB (adb)", "usb-serial": "USB 串口", "usb-serial-mux": "串口汇聚"}
STATE_NAMES = {"idle": "空闲", "running": "运行中", "done": "完成", "error": "错误", "stopped": "已停止",
               "paused": "暂停"}

COLUMNS = ["设备ID", "型号", "Android", "连接方式", "地址", "测试状态", "最后活动"]


def status_text(dev: Device) -> str:
    parts = []
    for tool, st in dev.status.items():
        state = STATE_NAMES.get(str(st.get("state")), str(st.get("state", "")))
        prog = st.get("progress")
        s = f"{tool}: {state}"
        if prog is not None and str(st.get("state")) == "running":
            s += f" {prog}%"
        parts.append(s)
    return "; ".join(parts) or "-"


class DevicesPanel(QWidget):
    selection_changed = Signal()

    def __init__(self, hub: Hub, parent=None):
        super().__init__(parent)
        self.hub = hub

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.doubleClicked.connect(lambda _: self._show_details())

        self.adb_table = QTableWidget(0, 3)
        self.adb_table.setHorizontalHeaderLabels(["序列号", "状态", "型号"])
        self.adb_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.adb_table.verticalHeader().setVisible(False)
        self.adb_table.horizontalHeader().setStretchLastSection(True)
        self.adb_table.setMaximumHeight(130)

        g1 = QGroupBox("已连接设备 (可多选，作为参数/命令下发目标)")
        l1 = QVBoxLayout(g1)
        l1.addWidget(self.table)
        g2 = QGroupBox("USB adb 设备")
        l2 = QVBoxLayout(g2)
        l2.addWidget(self.adb_table)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(g1, 1)
        lay.addWidget(g2)

    # ---------------------------------------------------------------- 行管理
    def _row_of(self, key: str) -> int:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.UserRole) == key:
                return r
        return -1

    def upsert(self, dev: Device) -> None:
        r = self._row_of(dev.key)
        if r < 0:
            r = self.table.rowCount()
            self.table.insertRow(r)
        values = [dev.display_id, dev.model, dev.android_version, TRANSPORT_NAMES.get(dev.transport, dev.transport),
                  dev.peer, status_text(dev), time.strftime("%H:%M:%S", time.localtime(dev.last_seen))]
        for c, v in enumerate(values):
            it = self.table.item(r, c)
            if it is None:
                it = QTableWidgetItem()
                self.table.setItem(r, c, it)
            it.setText(v)
            if c == 0:
                it.setData(Qt.UserRole, dev.key)

    def remove(self, dev: Device) -> None:
        r = self._row_of(dev.key)
        if r >= 0:
            self.table.removeRow(r)

    def refresh_all(self) -> None:
        for d in self.hub.devices():
            self.upsert(d)

    def set_adb_devices(self, devices) -> None:
        self.adb_table.setRowCount(len(devices))
        for r, d in enumerate(devices):
            for c, v in enumerate([d.serial, d.state, d.model]):
                self.adb_table.setItem(r, c, QTableWidgetItem(v))

    # ---------------------------------------------------------------- 选择
    def selected_keys(self) -> list[str]:
        keys = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            if it:
                keys.append(it.data(Qt.UserRole))
        return keys

    def target_keys(self, all_devices: bool = False) -> list[str]:
        """返回下发目标：all_devices=True 时为全部已握手设备；否则为选中设备；
        未选中且只连接了一台时默认该设备。"""
        greeted = [d.key for d in self.hub.devices() if d.greeted]
        if all_devices:
            return greeted
        sel = self.selected_keys()
        if sel:
            return sel
        if len(greeted) == 1:
            return greeted
        return []

    # ---------------------------------------------------------------- 菜单
    def _context_menu(self, pos) -> None:
        keys = self.selected_keys()
        if not keys:
            return
        m = QMenu(self)
        m.addAction("查看设备信息", self._show_details)
        m.addAction("断开连接", lambda: [self.hub.disconnect(k) for k in keys])
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _show_details(self) -> None:
        keys = self.selected_keys()
        if not keys:
            return
        dev = self.hub.device(keys[0])
        if not dev:
            return
        info = {
            "device_id": dev.device_id, "model": dev.model, "android_version": dev.android_version,
            "app_version": dev.app_version, "tools": dev.tools, "transport": dev.transport, "peer": dev.peer,
            "connected_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(dev.connected_at)),
            "hello": dev.info, "status": dev.status, "configs": dev.configs,
        }
        box = QMessageBox(self)
        box.setWindowTitle(f"设备信息 - {dev.display_id}")
        box.setText(f"{dev.display_id}  {dev.model}")
        box.setDetailedText(json.dumps(info, ensure_ascii=False, indent=2))
        box.exec()

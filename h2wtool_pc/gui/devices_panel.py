"""已连接设备列表 + adb USB 设备列表。"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHeaderView, QMenu, QMessageBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.hub import Device, Hub

TRANSPORT_NAMES = {"network": "网络", "usb-adb": "USB (adb)", "usb-serial": "USB 串口", "usb-serial-mux": "汇聚器-串口",
                   "network-mux": "汇聚器-网络", "usb-adb-mux": "汇聚器-USB(adb)"}
MUX_COLUMNS = ["汇聚器", "方式", "状态", "路数", "帧数", "丢帧", "坏帧"]
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

        self.mux_table = QTableWidget(0, len(MUX_COLUMNS))
        self.mux_table.setHorizontalHeaderLabels(MUX_COLUMNS)
        self.mux_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.mux_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.mux_table.verticalHeader().setVisible(False)
        self.mux_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.mux_table.horizontalHeader().setStretchLastSection(True)
        self.mux_table.setMaximumHeight(130)
        self.mux_table.setToolTip("丢帧：汇聚器序号不连续 (输出队列满被丢弃)；坏帧：校验失败 (线路干扰)")
        self.mux_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.mux_table.customContextMenuRequested.connect(self._mux_menu)
        self._mux_rows: list = []

        g1 = QGroupBox("已连接设备 (可多选，作为参数/命令下发目标)")
        l1 = QVBoxLayout(g1)
        l1.addWidget(self.table)
        g2 = QGroupBox("USB adb 设备")
        l2 = QVBoxLayout(g2)
        l2.addWidget(self.adb_table)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.mux_group = QGroupBox("SerialMux 汇聚器 (每一路作为一台设备出现在上方列表)")
        l3 = QVBoxLayout(self.mux_group)
        l3.addWidget(self.mux_table)
        self.mux_group.setVisible(False)

        lay.addWidget(g1, 1)
        lay.addWidget(self.mux_group)
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
        self.refresh_mux()

    def refresh_mux(self) -> None:
        sources = self.hub.mux_sources()
        self._mux_rows = sources
        self.mux_group.setVisible(bool(sources))
        self.mux_table.setRowCount(len(sources))
        for r, m in enumerate(sources):
            values = [m.name, m.kind, m.state, str(m.channel_count), str(m.frames), str(m.lost_frames),
                      str(m.bad_frames)]
            for c, v in enumerate(values):
                it = self.mux_table.item(r, c)
                if it is None:
                    it = QTableWidgetItem()
                    self.mux_table.setItem(r, c, it)
                it.setText(v)
                if c == 2:
                    it.setForeground(QBrush(QColor("#2e7d32" if m.connected else "#c62828")))
                if c == 5:
                    it.setForeground(QBrush(QColor("#c62828" if m.lost_frames else "#000000")))

    def _mux_menu(self, pos) -> None:
        rows = {i.row() for i in self.mux_table.selectionModel().selectedRows()}
        sources = [self._mux_rows[r] for r in sorted(rows) if r < len(self._mux_rows)]
        if not sources:
            return
        m = QMenu(self)
        m.addAction("断开/关闭", lambda: ([self.hub.close_mux(s) for s in sources], self.refresh_mux()))
        m.exec(self.mux_table.viewport().mapToGlobal(pos))

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

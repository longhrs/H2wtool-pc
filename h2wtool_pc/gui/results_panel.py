"""测试结果页：16 台设备看板 + 结果列表 (过滤、详情、导出 CSV)。"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.hub import Device
from ..core.result_store import RESULT_VALUES, ResultStore
from .devices_panel import status_text

COLUMNS = ["ID", "接收时间", "设备ID", "型号", "Flash 型号", "固件版本", "连接", "工具", "测试ID", "结果", "耗时(s)", "摘要"]
RESULT_COL = COLUMNS.index("结果")
BOARD_ROWS, BOARD_COLS = 4, 4  # 看板一页 16 台设备
RESULT_COLORS = {"PASS": QColor("#2e7d32"), "FAIL": QColor("#c62828"), "ABORT": QColor("#ef6c00"),
                 "ERROR": QColor("#6a1b9a")}


def summary_text(summary) -> str:
    if isinstance(summary, dict):
        return ", ".join(f"{k}={v}" for k, v in summary.items())
    return str(summary)


class DeviceTile(QFrame):
    """看板里的一格：一台设备的 Flash 信息、当前状态和最近一次结果。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tile")
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumSize(220, 120)
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: bold;")
        self.online = QLabel()
        self.flash = QLabel()
        self.fw = QLabel()
        self.state = QLabel()
        self.result = QLabel()
        self.result.setAlignment(Qt.AlignCenter)
        self.result.setMinimumHeight(28)
        self.summary = QLabel()
        self.summary.setStyleSheet("color: gray;")
        for w in (self.title, self.flash, self.fw, self.state, self.summary):
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(self.online)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        lay.addLayout(head)
        for w in (self.flash, self.fw, self.state, self.result, self.summary):
            lay.addWidget(w)
        self.show_slot(None)

    def show_slot(self, slot: dict | None) -> None:
        if slot is None:
            self.setStyleSheet("#tile { border: 1px dashed #bbb; }")
            for w in (self.title, self.online, self.flash, self.fw, self.state, self.summary):
                w.setText("")
            self.result.setText("空闲位")
            self.result.setStyleSheet("color: #bbb;")
            return
        self.setStyleSheet("#tile { border: 1px solid #999; }")
        info = slot["info"]
        self.title.setText(f"{slot['device_id']}  {slot['model']}".strip())
        self.online.setText("● 在线" if slot["online"] else "○ 离线")
        self.online.setStyleSheet("color: #2e7d32;" if slot["online"] else "color: gray;")
        self.flash.setText(f"Flash 型号: {info.get('flash_model') or '未上报'}")
        self.fw.setText(f"固件版本: {info.get('flash_fw_version') or '未上报'}")
        self.state.setText(f"状态: {slot['status']}")
        row = slot.get("result")
        if row:
            color = RESULT_COLORS.get(row["result"], QColor("#555"))
            t = time.strftime("%H:%M:%S", time.localtime(row["received_at"]))
            self.result.setText(f"{row['result']}   {row['tool']}  {t}")
            self.result.setStyleSheet(f"background: {color.name()}; color: white; font-weight: bold; border-radius: 3px;")
            self.summary.setText(summary_text(row["summary"])[:80])
        else:
            self.result.setText("等待结果")
            self.result.setStyleSheet("color: gray;")
            self.summary.setText("")


class DeviceBoard(QWidget):
    """测试结果看板：每页 16 格，每台设备一格，按出现顺序排列。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._order: list[str] = []
        self._slots: dict[str, dict] = {}
        self._page = 0
        grid = QGridLayout()
        grid.setSpacing(6)
        self.tiles = []
        for i in range(BOARD_ROWS * BOARD_COLS):
            t = DeviceTile()
            grid.addWidget(t, i // BOARD_COLS, i % BOARD_COLS)
            self.tiles.append(t)
        self.page_label = QLabel()
        self.b_prev = QPushButton("上一页")
        self.b_prev.clicked.connect(lambda: self._go(-1))
        self.b_next = QPushButton("下一页")
        self.b_next.clicked.connect(lambda: self._go(1))
        b_clear = QPushButton("清除离线设备")
        b_clear.setToolTip("看板上只保留当前在线的设备，结果记录不受影响")
        b_clear.clicked.connect(self.clear_offline)
        nav = QHBoxLayout()
        title = QLabel("设备看板")
        title.setStyleSheet("font-weight: bold;")
        nav.addWidget(title)
        nav.addWidget(self.page_label)
        nav.addStretch(1)
        for w in (self.b_prev, self.b_next, b_clear):
            nav.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(nav)
        lay.addLayout(grid, 1)
        self._render()

    def _slot(self, device_id: str) -> dict:
        if device_id not in self._slots:
            self._order.append(device_id)
            self._slots[device_id] = {"device_id": device_id, "model": "", "info": {}, "status": "-",
                                      "online": False, "result": None}
        return self._slots[device_id]

    def upsert_device(self, dev: Device) -> None:
        if not dev.greeted and not dev.passive:
            return  # 握手前 device_id 还没确定
        s = self._slot(dev.device_id or dev.peer)
        s.update(model=dev.model, info=dict(dev.info), status=status_text(dev), online=True)
        self._render()

    def device_offline(self, dev: Device) -> None:
        s = self._slots.get(dev.device_id or dev.peer)
        if s:
            s["online"] = False
            self._render()

    def on_result(self, row: dict) -> None:
        s = self._slot(row["device_id"])
        s["result"] = row
        if not s["model"]:
            s["model"] = row.get("device_model", "")
        for k in ("flash_model", "flash_fw_version"):
            if row.get(k) and not s["info"].get(k):
                s["info"][k] = row[k]
        self._render()

    def clear_offline(self) -> None:
        self._order = [d for d in self._order if self._slots[d]["online"]]
        self._slots = {d: self._slots[d] for d in self._order}
        self._render()

    def _pages(self) -> int:
        return max(1, -(-len(self._order) // len(self.tiles)))

    def _go(self, delta: int) -> None:
        self._page = min(max(self._page + delta, 0), self._pages() - 1)
        self._render()

    def _render(self) -> None:
        self._page = min(self._page, self._pages() - 1)
        start = self._page * len(self.tiles)
        ids = self._order[start:start + len(self.tiles)]
        for i, t in enumerate(self.tiles):
            t.show_slot(self._slots[ids[i]] if i < len(ids) else None)
        online = sum(1 for s in self._slots.values() if s["online"])
        self.page_label.setText(f"在线 {online} 台 / 共 {len(self._order)} 台   第 {self._page + 1}/{self._pages()} 页")
        self.b_prev.setEnabled(self._page > 0)
        self.b_next.setEnabled(self._page < self._pages() - 1)


class ResultsPanel(QWidget):
    def __init__(self, store: ResultStore, tools: dict[str, str], parent=None):
        super().__init__(parent)
        self.store = store

        self.tool_combo = QComboBox()
        self.tool_combo.addItem("全部工具", "")
        for tool, title in tools.items():
            self.tool_combo.addItem(title, tool)
        self.result_combo = QComboBox()
        self.result_combo.addItem("全部结果", "")
        for r in RESULT_VALUES:
            self.result_combo.addItem(r, r)
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(150)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 设备/型号/Flash/测试ID/摘要")
        self.search.returnPressed.connect(self.reload)
        for c in (self.tool_combo, self.result_combo, self.device_combo):
            c.currentIndexChanged.connect(self.reload)

        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.reload)
        btn_export = QPushButton("导出 CSV")
        btn_export.clicked.connect(self.export_csv)
        btn_delete = QPushButton("删除选中")
        btn_delete.clicked.connect(self.delete_selected)
        btn_clear = QPushButton("清空全部")
        btn_clear.clicked.connect(self.clear_all)

        top = QHBoxLayout()
        for w in (self.tool_combo, self.result_combo, self.device_combo, self.search):
            top.addWidget(w)
        top.addWidget(btn_refresh)
        top.addStretch(1)
        for w in (btn_export, btn_delete, btn_clear):
            top.addWidget(w)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_detail)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选择一条结果查看完整数据")

        self.board = DeviceBoard()

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.board)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 1)

        self.stats_label = QLabel()

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        lay.addWidget(self.stats_label)

        self._reload_devices()
        self.reload()

    # ---------------------------------------------------------------- 数据
    def _filters(self) -> dict:
        return {
            "tool": self.tool_combo.currentData() or None,
            "result": self.result_combo.currentData() or None,
            "device_id": self.device_combo.currentData() or None,
            "text": self.search.text().strip() or None,
        }

    def _reload_devices(self) -> None:
        cur = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("全部设备", "")
        for d in self.store.device_ids():
            self.device_combo.addItem(d, d)
        idx = self.device_combo.findData(cur) if cur else 0
        self.device_combo.setCurrentIndex(max(idx, 0))
        self.device_combo.blockSignals(False)

    def reload(self) -> None:
        rows = self.store.query(**self._filters())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._fill_row(r, row)
        self.table.setSortingEnabled(True)
        self._update_stats(len(rows))

    def _fill_row(self, r: int, row: dict) -> None:
        values = [
            row["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["received_at"])),
            row["device_id"], row["device_model"], row.get("flash_model", ""), row.get("flash_fw_version", ""),
            row["transport"], row["tool"], row["test_id"],
            row["result"], "" if row["duration_s"] is None else round(row["duration_s"], 1),
            summary_text(row["summary"]),
        ]
        for c, v in enumerate(values):
            it = QTableWidgetItem()
            if isinstance(v, (int, float)):
                it.setData(Qt.DisplayRole, v)  # 数字列按数值排序
            else:
                it.setText(str(v))
            if c == 0:
                it.setData(Qt.UserRole, row["id"])
            if c == RESULT_COL:
                color = RESULT_COLORS.get(row["result"])
                if color:
                    it.setForeground(color)
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
            self.table.setItem(r, c, it)

    def _matches(self, row: dict) -> bool:
        f = self._filters()
        if f["tool"] and row["tool"] != f["tool"]:
            return False
        if f["result"] and row["result"] != f["result"]:
            return False
        if f["device_id"] and row["device_id"] != f["device_id"]:
            return False
        if f["text"]:
            hay = " ".join([row["device_id"], row["device_model"], row["test_id"], row.get("flash_model", ""),
                            row.get("flash_fw_version", ""), json.dumps(row["summary"], ensure_ascii=False)])
            if f["text"] not in hay:
                return False
        return True

    def add_result(self, row: dict) -> None:
        self.board.on_result(row)
        if self.device_combo.findData(row["device_id"]) < 0:
            self._reload_devices()
        if not self._matches(row):
            self._update_stats(self.table.rowCount())
            return
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.insertRow(0)
        self._fill_row(0, row)
        self.table.setSortingEnabled(sorting)
        self._update_stats(self.table.rowCount())

    def _update_stats(self, shown: int) -> None:
        st = self.store.stats()
        total = sum(st.values())
        parts = [f"{k}: {st.get(k, 0)}" for k in RESULT_VALUES]
        self.stats_label.setText(f"当前显示 {shown} 条 | 数据库共 {total} 条 | " + "  ".join(parts))

    def _selected_ids(self) -> list[int]:
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            if it:
                ids.append(int(it.data(Qt.UserRole)))
        return ids

    def _show_detail(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self.detail.clear()
            return
        row = self.store.get(ids[0])
        if row:
            self.detail.setPlainText(json.dumps(row["raw"], ensure_ascii=False, indent=2))

    # ---------------------------------------------------------------- 操作
    def export_csv(self) -> None:
        rows = self.store.query(**self._filters(), limit=1_000_000)
        if not rows:
            QMessageBox.information(self, "导出", "没有可导出的结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"h2w_results_{time.strftime('%Y%m%d_%H%M%S')}.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            ResultStore.export_csv(path, rows)
        except OSError as e:
            QMessageBox.critical(self, "导出失败", str(e))
            return
        QMessageBox.information(self, "导出", f"已导出 {len(rows)} 条到\n{path}")

    def delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "删除", f"删除选中的 {len(ids)} 条结果？") != QMessageBox.Yes:
            return
        self.store.delete(ids)
        self.reload()

    def clear_all(self) -> None:
        if QMessageBox.question(self, "清空", "确定清空数据库中的全部测试结果？此操作不可恢复。") != QMessageBox.Yes:
            return
        self.store.clear()
        self._reload_devices()
        self.reload()

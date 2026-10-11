"""测试结果列表：过滤、详情、导出 CSV。"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..core.result_store import RESULT_VALUES, ResultStore

COLUMNS = ["ID", "接收时间", "设备ID", "型号", "连接", "工具", "测试ID", "结果", "耗时(s)", "摘要"]
RESULT_COLORS = {"PASS": QColor("#2e7d32"), "FAIL": QColor("#c62828"), "ABORT": QColor("#ef6c00"),
                 "ERROR": QColor("#6a1b9a")}


def summary_text(summary) -> str:
    if isinstance(summary, dict):
        return ", ".join(f"{k}={v}" for k, v in summary.items())
    return str(summary)


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
        self.search.setPlaceholderText("搜索 设备/型号/测试ID/摘要")
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

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)

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
            row["device_id"], row["device_model"], row["transport"], row["tool"], row["test_id"],
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
            if c == 7:
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
            hay = " ".join([row["device_id"], row["device_model"], row["test_id"], json.dumps(row["summary"],
                                                                                            ensure_ascii=False)])
            if f["text"] not in hay:
                return False
        return True

    def add_result(self, row: dict) -> None:
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

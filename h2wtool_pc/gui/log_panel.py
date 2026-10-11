from __future__ import annotations

import html
import time

from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

LEVEL_COLORS = {"error": "#c62828", "warn": "#ef6c00", "warning": "#ef6c00", "result": "#1565c0",
                "debug": "#808080"}


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(10000)
        self.errors_only = QCheckBox("仅显示错误")
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        b_clear = QPushButton("清空")
        b_clear.clicked.connect(self.view.clear)
        b_save = QPushButton("保存日志")
        b_save.clicked.connect(self.save)
        top = QHBoxLayout()
        top.addWidget(self.errors_only)
        top.addWidget(self.autoscroll)
        top.addStretch(1)
        top.addWidget(b_clear)
        top.addWidget(b_save)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)

    def append(self, level: str, text: str, source: str = "") -> None:
        level = (level or "info").lower()
        if self.errors_only.isChecked() and level not in ("error", "warn", "warning"):
            return
        color = LEVEL_COLORS.get(level)
        src = f"[{html.escape(source)}] " if source else ""
        line = f"{time.strftime('%H:%M:%S')} [{level}] {src}{html.escape(text)}"
        if color:
            line = f'<span style="color:{color}">{line}</span>'
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.view.appendHtml(line)
        if self.autoscroll.isChecked() and at_bottom:
            sb.setValue(sb.maximum())

    def save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", f"h2w_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                                              "Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText())

"""根据 schema 动态生成的参数设置页，可保存预设并下发到设备。"""

from __future__ import annotations

import json
import time
from typing import Callable

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from ..core.config_schema import Field, PresetStore, ToolSchema
from ..core.hub import Hub

INT_LIMIT = 2**31 - 1


class MultiSelect(QWidget):
    def __init__(self, f: Field, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        btns = QHBoxLayout()
        b_all = QPushButton("全选")
        b_none = QPushButton("全不选")
        b_all.clicked.connect(lambda: self.set_value(f.option_values))
        b_none.clicked.connect(lambda: self.set_value([]))
        btns.addWidget(b_all)
        btns.addWidget(b_none)
        btns.addStretch(1)
        lay.addLayout(btns)
        grid = QGridLayout()
        self.boxes: list[tuple[object, QCheckBox]] = []
        for i, opt in enumerate(f.options):
            cb = QCheckBox(str(opt.get("label", opt["value"])))
            grid.addWidget(cb, i // 3, i % 3)
            self.boxes.append((opt["value"], cb))
        lay.addLayout(grid)

    def value(self) -> list:
        return [v for v, cb in self.boxes if cb.isChecked()]

    def set_value(self, values) -> None:
        sv = {str(x) for x in values or []}
        for v, cb in self.boxes:
            cb.setChecked(str(v) in sv)


class ConfigForm(QWidget):
    """schema -> 表单控件"""

    def __init__(self, schema: ToolSchema, parent=None):
        super().__init__(parent)
        self.schema = schema
        self._getters: dict[str, Callable] = {}
        self._setters: dict[str, Callable] = {}
        lay = QVBoxLayout(self)
        if schema.description:
            desc = QLabel(schema.description)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: gray;")
            lay.addWidget(desc)
        for group, fields in schema.groups():
            box = QGroupBox(group)
            if len(fields) == 1 and fields[0].type == "multi":
                inner = QVBoxLayout(box)
                inner.addWidget(self._make_widget(fields[0]))
            else:
                form = QFormLayout(box)
                for f in fields:
                    w = self._make_widget(f)
                    label = QLabel(f.label)
                    if f.help:
                        label.setToolTip(f.help)
                        w.setToolTip(f.help)
                        label.setText(f"{f.label} ⓘ")
                    form.addRow(label, w)
            lay.addWidget(box)
        lay.addStretch(1)
        self.set_config(schema.defaults())

    def _make_widget(self, f: Field) -> QWidget:
        if f.type == "int":
            w = QSpinBox()
            w.setRange(int(f.min) if f.min is not None else -INT_LIMIT, int(f.max) if f.max is not None else INT_LIMIT)
            w.setMinimumWidth(140)
            self._getters[f.key] = w.value
            self._setters[f.key] = lambda v, w=w: w.setValue(int(v))
        elif f.type == "float":
            w = QDoubleSpinBox()
            w.setDecimals(3)
            w.setRange(f.min if f.min is not None else -1e12, f.max if f.max is not None else 1e12)
            w.setMinimumWidth(140)
            self._getters[f.key] = w.value
            self._setters[f.key] = lambda v, w=w: w.setValue(float(v))
        elif f.type == "bool":
            w = QCheckBox()
            self._getters[f.key] = w.isChecked
            self._setters[f.key] = lambda v, w=w: w.setChecked(bool(v))
        elif f.type == "string":
            w = QLineEdit()
            self._getters[f.key] = w.text
            self._setters[f.key] = lambda v, w=w: w.setText(str(v))
        elif f.type == "enum":
            w = QComboBox()
            for opt in f.options:
                w.addItem(str(opt.get("label", opt["value"])), opt["value"])

            def set_enum(v, w=w):
                for i in range(w.count()):
                    if str(w.itemData(i)) == str(v):
                        w.setCurrentIndex(i)
                        return

            self._getters[f.key] = w.currentData
            self._setters[f.key] = set_enum
        else:  # multi
            w = MultiSelect(f)
            self._getters[f.key] = w.value
            self._setters[f.key] = w.set_value
        return w

    def config(self) -> dict:
        return {k: g() for k, g in self._getters.items()}

    def set_config(self, cfg: dict) -> list[str]:
        """应用配置，返回无法应用的字段错误列表。"""
        cleaned, errors = self.schema.validate(cfg, fill_defaults=False)
        for k, v in cleaned.items():
            if k in self._setters:
                self._setters[k](v)
        return list(errors.values())


class ConfigPanel(QWidget):
    def __init__(self, schema: ToolSchema, hub: Hub, presets: PresetStore,
                 target_keys: Callable[[bool], list[str]], log: Callable[[str, str], None], parent=None):
        super().__init__(parent)
        self.schema = schema
        self.tool = schema.tool
        self.hub = hub
        self.presets = presets
        self.target_keys = target_keys
        self.log = log
        self._awaiting_report: dict[str, float] = {}  # 设备 key -> 请求时间

        self.form = ConfigForm(schema)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)

        # 预设
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        b_load = QPushButton("加载预设")
        b_load.clicked.connect(self.load_preset)
        b_save = QPushButton("保存为预设")
        b_save.clicked.connect(self.save_preset)
        b_del = QPushButton("删除预设")
        b_del.clicked.connect(self.delete_preset)
        b_default = QPushButton("恢复默认")
        b_default.clicked.connect(lambda: self.form.set_config(self.schema.defaults()))
        b_import = QPushButton("导入 JSON")
        b_import.clicked.connect(self.import_json)
        b_export = QPushButton("导出 JSON")
        b_export.clicked.connect(self.export_json)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("预设:"))
        for w in (self.preset_combo, b_load, b_save, b_del):
            row1.addWidget(w)
        row1.addStretch(1)
        for w in (b_default, b_import, b_export):
            row1.addWidget(w)

        # 设备操作
        b_read = QPushButton("从设备读取")
        b_read.clicked.connect(self.read_from_device)
        b_push = QPushButton("下发到选中设备")
        b_push.clicked.connect(lambda: self.push(False))
        b_push_all = QPushButton("下发到全部设备")
        b_push_all.clicked.connect(lambda: self.push(True))
        b_start = QPushButton("下发并开始测试")
        b_start.setStyleSheet("font-weight: bold;")
        b_start.clicked.connect(self.start_test)
        b_stop = QPushButton("停止测试")
        b_stop.clicked.connect(self.stop_test)
        row2 = QHBoxLayout()
        for w in (b_read, b_push, b_push_all):
            row2.addWidget(w)
        row2.addStretch(1)
        for w in (b_start, b_stop):
            row2.addWidget(w)

        lay = QVBoxLayout(self)
        lay.addLayout(row1)
        lay.addWidget(scroll, 1)
        lay.addLayout(row2)
        self.reload_presets()

    # ---------------------------------------------------------------- 预设 / 文件
    def reload_presets(self) -> None:
        self.preset_combo.clear()
        self.preset_combo.addItems(self.presets.list(self.tool))

    def load_preset(self) -> None:
        name = self.preset_combo.currentText()
        if not name:
            return
        try:
            errors = self.form.set_config(self.presets.load(self.tool, name))
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "加载失败", str(e))
            return
        if errors:
            QMessageBox.warning(self, "部分参数无效", "\n".join(errors))

    def save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "保存预设", "预设名称:", text=self.preset_combo.currentText())
        if not ok or not name.strip():
            return
        self.presets.save(self.tool, name.strip(), self.form.config())
        self.reload_presets()
        self.preset_combo.setCurrentText(name.strip())

    def delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name and QMessageBox.question(self, "删除预设", f"删除预设 “{name}”？") == QMessageBox.Yes:
            self.presets.delete(self.tool, name)
            self.reload_presets()

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入参数", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.critical(self, "导入失败", str(e))
            return
        if isinstance(data, dict) and "config" in data and isinstance(data["config"], dict):
            data = data["config"]
        if not isinstance(data, dict):
            QMessageBox.critical(self, "导入失败", "文件内容必须是 JSON 对象")
            return
        errors = self.form.set_config(data)
        if errors:
            QMessageBox.warning(self, "部分参数无效", "\n".join(errors))

    def export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出参数", f"{self.tool}_config.json", "JSON (*.json)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tool": self.tool, "config": self.form.config()}, f, ensure_ascii=False, indent=2)

    # ---------------------------------------------------------------- 设备
    def _targets(self, all_devices: bool) -> list[str]:
        keys = self.target_keys(all_devices)
        if not keys:
            QMessageBox.information(self, "没有目标设备",
                                    "请先在左侧选择设备 (需要设备已连接并完成握手)。")
        return keys

    def _validated(self) -> dict | None:
        cfg, errors = self.schema.validate(self.form.config())
        if errors:
            QMessageBox.warning(self, "参数错误", "\n".join(errors.values()))
            return None
        return cfg

    def _each(self, keys: list[str], fn: Callable[[str], str], what: str) -> None:
        for k in keys:
            dev = self.hub.device(k)
            name = dev.display_id if dev else k
            try:
                fn(k)
                self.log("info", f"{what} -> {name}")
            except Exception as e:
                self.log("error", f"{what} -> {name} 失败: {e}")

    def push(self, all_devices: bool) -> None:
        cfg = self._validated()
        if cfg is None:
            return
        keys = self._targets(all_devices)
        self._each(keys, lambda k: self.hub.send_config(k, self.tool, cfg), f"下发 {self.tool} 参数")

    def read_from_device(self) -> None:
        keys = self._targets(False)
        if len(keys) > 1:
            QMessageBox.information(self, "读取参数", "请只选择一台设备")
            return
        for k in keys:
            self._awaiting_report[k] = time.time()
        self._each(keys, lambda k: self.hub.request_config(k, self.tool), f"读取 {self.tool} 参数")

    def start_test(self) -> None:
        cfg = self._validated()
        if cfg is None:
            return
        keys = self._targets(False)
        if not keys:
            return

        def go(k: str) -> str:
            self.hub.send_config(k, self.tool, cfg)
            return self.hub.send_command(k, self.tool, "start")

        self._each(keys, go, f"开始 {self.tool} 测试")

    def stop_test(self) -> None:
        keys = self._targets(False)
        self._each(keys, lambda k: self.hub.send_command(k, self.tool, "stop"), f"停止 {self.tool} 测试")

    def on_config_report(self, device_key: str, config: dict) -> None:
        requested = self._awaiting_report.pop(device_key, None)
        if requested is None or time.time() - requested > 30:
            return  # 不是本页主动请求的，忽略 (日志里会记录)
        errors = self.form.set_config(config)
        if errors:
            QMessageBox.warning(self, "设备参数部分无效", "\n".join(errors))

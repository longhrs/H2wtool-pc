from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ..core.config_schema import SchemaError, load_schemas
from ..core.hub import Hub
from ..core.result_store import ResultStore
from ..core.settings import Settings
from .main_window import MainWindow


def run_gui(data_dir: Path, settings: Settings, args) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("H2wTool PC")
    app.setStyle("Fusion")

    if args.host:
        settings.bind_host = args.host
    if args.port is not None:
        settings.tcp_port = args.port

    try:
        schemas = load_schemas(data_dir / "schemas")
    except SchemaError as e:
        QMessageBox.critical(None, "参数定义错误", f"加载 schema 失败，将只使用内置定义:\n{e}")
        schemas = load_schemas(None)

    store = ResultStore(data_dir / "results.db")
    hub = Hub(data_dir, store, schemas)
    win = MainWindow(hub, settings, data_dir, schemas)
    for port in getattr(args, "serial", []) or []:
        try:
            hub.open_serial(port, settings.serial_baudrate)
        except Exception as e:
            win.log("error", f"打开串口 {port} 失败: {e}")
    win.show()
    code = app.exec()
    store.close()
    return code

"""入口：
    python -m h2wtool_pc               启动图形界面
    python -m h2wtool_pc --headless    无界面模式，只接收并保存测试结果
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .core.settings import Settings, default_data_dir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="h2wtool_pc", description="H2wTool PC 端")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--data-dir", type=Path, default=None, help="数据目录 (结果数据库/日志/预设)")
    p.add_argument("--headless", action="store_true", help="无界面模式")
    p.add_argument("--host", default=None, help="监听地址 (默认取设置)")
    p.add_argument("--port", type=int, default=None, help="TCP 端口 (默认取设置)")
    p.add_argument("--no-adb", action="store_true", help="无界面模式下不自动执行 adb reverse")
    p.add_argument("--serial", action="append", default=[], help="无界面模式下打开串口，可多次指定")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run_headless(args, data_dir: Path, settings: Settings) -> int:
    from .core.adb import Adb, AdbAutoReverse
    from .core.config_schema import load_schemas
    from .core.hub import Event, Hub
    from .core.result_store import ResultStore

    store = ResultStore(data_dir / "results.db")
    hub = Hub(data_dir, store, load_schemas(data_dir / "schemas"))
    hub.heartbeat_timeout_s = settings.heartbeat_timeout_s

    def on_event(ev: Event) -> None:
        dev = ev.device.display_id if ev.device else "-"
        if ev.kind in ("info", "error"):
            print(f"[{ev.kind}] {ev.data['text']}", flush=True)
        elif ev.kind == "result":
            r = ev.data["row"]
            print(f"[result] {dev} {r['tool']} {r['result']} test_id={r['test_id']} "
                  f"duration={r['duration_s']} summary={r['summary']}", flush=True)
        elif ev.kind == "status":
            m = ev.data["msg"]
            if m.get("state") == ev.data.get("prev_state"):
                return  # 只打印状态变化，不打印每次进度
            print(f"[status] {dev} {ev.data['tool']} {m.get('state')} {m.get('progress', '')}% "
                  f"{m.get('message', '')}", flush=True)
        elif ev.kind == "log":
            print(f"[log] {dev} [{ev.data['level']}] {ev.data['text']}", flush=True)
        elif ev.kind == "file":
            print(f"[file] {dev} -> {ev.data['path']} ({ev.data['size']} bytes)", flush=True)

    hub.add_listener(on_event)
    hub.start_server(args.host or settings.bind_host, args.port if args.port is not None else settings.tcp_port,
                     settings.discovery_enabled, settings.discovery_port)
    for port in args.serial:
        try:
            hub.open_serial(port, settings.serial_baudrate)
        except Exception as e:
            print(f"[error] 打开串口 {port} 失败: {e}", flush=True)

    auto = None
    if settings.adb_auto_reverse and not args.no_adb:
        def on_adb(level, text, _devices):
            if level != "devices":
                print(f"[adb] {text}", flush=True)
        auto = AdbAutoReverse(Adb(settings.adb_path), lambda: hub.tcp_port, on_adb)
        auto.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    print(f"数据目录: {data_dir}  (Ctrl+C 退出)", flush=True)
    while not stop.wait(0.5):
        pass
    if auto:
        auto.stop()
    hub.shutdown()
    store.close()
    return 0


def force_utf8_stdio() -> None:
    """Windows 下输出被重定向到文件/管道时默认是 ANSI 编码，打印中文会崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data_dir: Path = args.data_dir or default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings.load(data_dir / "settings.json")
    if args.headless:
        return run_headless(args, data_dir, settings)
    try:
        from .gui.app import run_gui
    except ImportError as e:
        print(f"无法加载图形界面 ({e})。请执行 pip install PySide6，或使用 --headless", file=sys.stderr)
        return 1
    return run_gui(data_dir, settings, args)


if __name__ == "__main__":
    sys.exit(main())

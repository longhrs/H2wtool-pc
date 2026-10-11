"""核心调度：管理连接的 Android 设备，处理收到的消息，向设备下发参数/命令。

Hub 与 GUI 无关，可以在无界面模式下运行。事件通过监听器回调发出
(回调在传输层线程里执行，GUI 需要自行切换到主线程)。
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__, protocol
from ..protocol import MsgType
from ..transport import DiscoveryResponder, Link, SerialLink, SerialMuxReader, TcpServer
from .config_schema import ToolSchema, safe_filename
from .result_store import ResultStore

log = logging.getLogger(__name__)


@dataclass
class Device:
    link: Link
    device_id: str = ""
    model: str = ""
    android_version: str = ""
    app_version: str = ""
    tools: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    status: dict[str, dict] = field(default_factory=dict)  # tool -> 最近一次 status 消息
    configs: dict[str, dict] = field(default_factory=dict)  # tool -> 设备上报的当前配置
    greeted: bool = False

    @property
    def key(self) -> str:
        return self.link.link_id

    @property
    def transport(self) -> str:
        return self.link.transport

    @property
    def peer(self) -> str:
        return self.link.peer

    @property
    def passive(self) -> bool:
        """只读设备：接在控制台串口上、没有握手 (或握手声明 passive) 的 stub，例如 bootloader 里的 memtest。
        PC 不向它回发任何数据 —— 往 bootloader 控制台写字节可能打断自动启动或被当成命令。"""
        return self.link.console_text and (not self.greeted or bool(self.info.get("passive")))

    @property
    def display_id(self) -> str:
        return self.device_id or f"(未握手 {self.peer})"


@dataclass
class Event:
    kind: str  # device_added / device_updated / device_removed / result / status / log / config_report / ack / file / error / info
    device: Device | None = None
    data: dict = field(default_factory=dict)


Listener = Callable[[Event], None]


class Hub:
    def __init__(self, data_dir: Path, store: ResultStore, schemas: dict[str, ToolSchema] | None = None):
        self.data_dir = data_dir
        self.store = store
        self.schemas = schemas or {}
        self._devices: dict[str, Device] = {}
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._pending: dict[str, dict] = {}  # 已发送消息 id -> 请求信息，用于匹配 ack
        self.tcp_server: TcpServer | None = None
        self.discovery: DiscoveryResponder | None = None
        self.heartbeat_timeout_s = 30
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None
        self._mux_readers: list[SerialMuxReader] = []

    # ------------------------------------------------------------------ 监听
    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, kind: str, device: Device | None = None, **data: Any) -> None:
        ev = Event(kind, device, data)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:
                log.exception("事件监听器异常")

    def info(self, text: str) -> None:
        self._emit("info", text=text)

    def error(self, text: str) -> None:
        self._emit("error", text=text)

    # ------------------------------------------------------------------ 服务启停
    def start_server(self, host: str = "0.0.0.0", port: int = protocol.DEFAULT_TCP_PORT,
                     discovery: bool = True, discovery_port: int = protocol.DEFAULT_DISCOVERY_PORT) -> None:
        self.stop_server()
        self.tcp_server = TcpServer(self, host, port)
        self.tcp_server.start()
        self.info(f"TCP 服务已启动 {host}:{self.tcp_server.port}")
        if discovery:
            self.discovery = DiscoveryResponder(lambda: self.tcp_port, port=discovery_port)
            try:
                self.discovery.start()
                self.info(f"UDP 发现服务已启动 端口 {self.discovery.port}")
            except OSError as e:
                self.discovery = None
                self.error(f"UDP 发现服务启动失败: {e}")
        self._start_watchdog()

    def stop_server(self) -> None:
        if self.tcp_server:
            self.tcp_server.stop()
            self.tcp_server = None
            self.info("TCP 服务已停止")
        if self.discovery:
            self.discovery.stop()
            self.discovery = None

    @property
    def tcp_port(self) -> int:
        return self.tcp_server.port if self.tcp_server else 0

    @property
    def server_running(self) -> bool:
        return bool(self.tcp_server and self.tcp_server.running)

    def open_serial(self, port: str, baudrate: int = 115200) -> SerialLink:
        link = SerialLink(self, port, baudrate)
        link.start()
        self._start_watchdog()
        return link

    def open_serial_mux(self, port: str, baudrate: int = 115200) -> SerialMuxReader:
        """打开 SerialMux 汇聚口，每个源端口在收到第一帧时作为一台设备出现。"""
        reader = SerialMuxReader(self, port, baudrate)
        reader.start()
        self._mux_readers.append(reader)
        self.info(f"已打开汇聚串口 {port}")
        return reader

    def shutdown(self) -> None:
        self._watchdog_stop.set()
        self.stop_server()
        for r in self._mux_readers:
            r.close()
        self._mux_readers.clear()
        for d in self.devices():
            d.link.close()

    def _start_watchdog(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog = threading.Thread(target=self._watchdog_loop, name="hub-watchdog", daemon=True)
        self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(2.0):
            timeout = self.heartbeat_timeout_s
            if timeout <= 0:
                continue
            now = time.time()
            for d in self.devices():
                if d.link.idle_timeout and now - d.last_seen > timeout:
                    self.error(f"{d.display_id} 超过 {timeout}s 无数据，断开连接")
                    d.link.close()
            # 清理太久没有回应的请求
            with self._lock:
                for mid in [k for k, v in self._pending.items() if now - v["sent_at"] > 300]:
                    del self._pending[mid]

    # ------------------------------------------------------------------ 设备
    def devices(self) -> list[Device]:
        with self._lock:
            return list(self._devices.values())

    def device(self, key: str) -> Device | None:
        with self._lock:
            return self._devices.get(key)

    def disconnect(self, key: str) -> None:
        d = self.device(key)
        if d:
            d.link.close()

    # ------------------------------------------------------------------ LinkHandler
    def on_link_opened(self, link: Link) -> None:
        dev = Device(link=link)
        with self._lock:
            self._devices[link.link_id] = dev
        self.info(f"新连接 [{link.transport}] {link.peer}")
        self._emit("device_added", dev)

    def on_link_closed(self, link: Link) -> None:
        with self._lock:
            dev = self._devices.pop(link.link_id, None)
        if dev:
            self.info(f"连接断开 {dev.display_id} [{link.transport}] {link.peer}")
            self._emit("device_removed", dev)

    def on_link_error(self, link: Link, error: str) -> None:
        dev = self.device(link.link_id)
        self.error(f"{dev.display_id if dev else link.peer} 协议错误: {error}")

    def on_link_message(self, link: Link, msg: dict) -> None:
        dev = self.device(link.link_id)
        if dev is None:
            return
        dev.last_seen = time.time()
        handler = getattr(self, f"_on_{msg['type']}", None)
        if handler is None:
            self._emit("log", dev, level="warn", tool="", text=f"未知消息类型 {msg['type']}")
            return
        try:
            handler(dev, msg)
        except Exception as e:
            log.exception("处理消息失败")
            self.error(f"处理 {dev.display_id} 的 {msg['type']} 消息失败: {e}")
            self._reply(dev, protocol.make_ack(msg.get("id"), ok=False, error=str(e)))

    # ------------------------------------------------------------------ 消息处理
    def _reply(self, dev: Device, msg: dict) -> None:
        if dev.passive:
            return
        try:
            dev.link.send(msg)
        except Exception as e:
            self.error(f"发送到 {dev.display_id} 失败: {e}")

    def _on_hello(self, dev: Device, msg: dict) -> None:
        dev.device_id = str(msg.get("device_id") or dev.peer)
        dev.model = str(msg.get("model") or "")
        dev.android_version = str(msg.get("android_version") or "")
        dev.app_version = str(msg.get("app_version") or "")
        dev.tools = [str(t) for t in msg.get("tools") or []]
        dev.info = {k: v for k, v in msg.items() if k not in ("type", "id", "ts")}
        dev.greeted = True
        self._reply(dev, protocol.make_message(
            MsgType.WELCOME, ref=msg.get("id"), server="H2wTool-PC", server_version=__version__,
            protocol=protocol.PROTOCOL_VERSION, server_time=protocol.now_ms(),
            heartbeat_interval_s=max(1, self.heartbeat_timeout_s // 3) if self.heartbeat_timeout_s else 10,
        ))
        self.info(f"设备握手 {dev.device_id} {dev.model} Android {dev.android_version} [{dev.transport}]")
        self._emit("device_updated", dev)

    def _on_heartbeat(self, dev: Device, msg: dict) -> None:
        self._emit("device_updated", dev)

    def _on_status(self, dev: Device, msg: dict) -> None:
        tool = str(msg.get("tool") or "")
        prev_state = dev.status.get(tool, {}).get("state")
        dev.status[tool] = msg
        self._emit("status", dev, tool=tool, msg=msg, prev_state=prev_state)
        self._emit("device_updated", dev)

    def _on_log(self, dev: Device, msg: dict) -> None:
        tool = str(msg.get("tool") or "")
        level = str(msg.get("level") or "info")
        text = str(msg.get("text") or "")
        self._append_device_log(dev, tool, level, text)
        self._emit("log", dev, level=level, tool=tool, text=text)

    def _on_result(self, dev: Device, msg: dict) -> None:
        if not msg.get("tool"):
            raise ValueError("result 缺少 tool")
        row_id, inserted = self.store.add_from_message(
            msg, device_id=dev.device_id or dev.peer, device_model=dev.model, transport=dev.transport)
        self._reply(dev, protocol.make_ack(msg.get("id"), ok=True))
        if inserted:
            row = self.store.get(row_id)
            self._append_device_log(dev, str(msg["tool"]), "result",
                                    f"{row['result']} test_id={row['test_id']} summary={row['summary']}")
            self._emit("result", dev, row=row)
        else:
            self.info(f"{dev.display_id} 重复上报结果 test_id={msg.get('test_id')}，已忽略")

    def _on_config_report(self, dev: Device, msg: dict) -> None:
        tool = str(msg.get("tool") or "")
        cfg = msg.get("config") or {}
        dev.configs[tool] = cfg
        self._emit("config_report", dev, tool=tool, config=cfg, ref=msg.get("ref"))

    def _on_file(self, dev: Device, msg: dict) -> None:
        name = safe_filename(str(msg.get("name") or "file.bin"))
        try:
            data = base64.b64decode(msg.get("data_b64") or "", validate=True)
        except (ValueError, TypeError) as e:
            raise ValueError(f"文件 base64 解码失败: {e}") from None
        d = self.data_dir / "files" / safe_filename(dev.device_id or dev.peer)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"
        path.write_bytes(data)
        self._reply(dev, protocol.make_ack(msg.get("id"), ok=True))
        self._emit("file", dev, path=str(path), size=len(data), tool=str(msg.get("tool") or ""))

    def _on_ack(self, dev: Device, msg: dict) -> None:
        with self._lock:
            req = self._pending.pop(str(msg.get("ref")), None)
        self._emit("ack", dev, ok=bool(msg.get("ok", True)), error=msg.get("error"), request=req or {})

    def _append_device_log(self, dev: Device, tool: str, level: str, text: str) -> None:
        try:
            d = self.data_dir / "logs" / safe_filename(dev.device_id or dev.peer)
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{time.strftime('%Y%m%d')}.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} [{level}] [{tool}] {text}\n")
        except OSError as e:
            log.warning("写设备日志失败: %s", e)

    # ------------------------------------------------------------------ 下发
    def _send_request(self, key: str, msg: dict, desc: str, **info: Any) -> str:
        dev = self.device(key)
        if dev is None:
            raise KeyError(f"设备 {key} 不存在")
        if dev.passive:
            raise ValueError(f"{dev.display_id} 是只读的串口 stub，不能下发参数或命令")
        with self._lock:
            self._pending[msg["id"]] = {"type": msg["type"], "desc": desc, "sent_at": time.time(), **info}
        try:
            dev.link.send(msg)
        except Exception:
            with self._lock:
                self._pending.pop(msg["id"], None)
            raise
        return msg["id"]

    def send_config(self, key: str, tool: str, config: dict) -> str:
        schema = self.schemas.get(tool)
        if schema:
            config, errors = schema.validate(config)
            if errors:
                raise ValueError("; ".join(errors.values()))
        msg = protocol.make_message(MsgType.CONFIG_SET, tool=tool, config=config)
        return self._send_request(key, msg, f"下发 {tool} 参数", tool=tool)

    def request_config(self, key: str, tool: str) -> str:
        msg = protocol.make_message(MsgType.CONFIG_GET, tool=tool)
        return self._send_request(key, msg, f"读取 {tool} 参数", tool=tool)

    def send_command(self, key: str, tool: str, action: str, **params: Any) -> str:
        msg = protocol.make_message(MsgType.COMMAND, tool=tool, action=action, **params)
        return self._send_request(key, msg, f"{tool} {action}", tool=tool, action=action)

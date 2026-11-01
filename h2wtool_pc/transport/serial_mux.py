"""SerialMux 汇聚器：一路输出上承载最多 32 路被测板的 UART 输出。

SerialMux (longhrs/uart 仓库里的 Android App) 把 /dev/ttyS0..31 收到的字节按帧打包，
带上来源端口号后同时转发到最多三种输出，三种输出里的字节流完全相同：

- 硬件串口 /dev/ttyS32          -> PC 上是一个 COM 口
- USB 虚拟串口 /dev/ttyGS0 (ACM) -> PC 上也是一个 COM 口 ("USB 串行设备")
- 网络 TCP：板子当服务端 (默认端口 9000，PC 连过去)，或当客户端 (板子连 PC 的端口)；
  没有 USB gadget 时还可以 `adb forward tcp:9000 tcp:9000` 后连 127.0.0.1:9000

每帧格式：

    A5 5A | 源端口号 (1) | 序号 (1, 每个源端口各自递增) | 长度 (2, 大端) | payload (n) | XOR 校验 (1)

校验为从源端口号到 payload 末尾所有字节的 XOR。PC 端按源端口号拆开，每一路当成一台
独立的只读串口设备 (各自切行、各自出现在设备列表/结果看板里)。汇聚器是单向的，PC 不回发任何数据。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import sys
import threading
import time
from typing import Callable

from .. import protocol
from .base import Link, LinkHandler
from .serial_link import serial

log = logging.getLogger(__name__)

SYNC = b"\xa5\x5a"
HEADER_SIZE = 6
DEFAULT_MUX_TCP_PORT = 9000
# SerialMux 默认单帧最多 1024 字节；长度字段明显不合理时当成假同步字，免得为一个坏帧头干等 64KB
MAX_PAYLOAD = 8192
RECONNECT_INTERVAL_S = 3.0


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return host == "localhost"


class MuxFrameDecoder:
    """把汇聚流解成 (源端口号, 序号, payload)。校验失败时跳过一个字节重新找帧。"""

    def __init__(self, max_payload: int = MAX_PAYLOAD) -> None:
        self._buf = bytearray()
        self._max = max_payload
        self.bad_frames = 0

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self._buf.extend(data)
        frames: list[tuple[int, int, bytes]] = []
        buf = self._buf
        while True:
            idx = buf.find(SYNC)
            if idx < 0:
                # 只保留可能是半个同步字的最后一个 0xA5
                keep = 1 if buf[-1:] == SYNC[:1] else 0
                del buf[: len(buf) - keep]
                break
            if idx:
                del buf[:idx]
            if len(buf) < HEADER_SIZE:
                break
            length = (buf[4] << 8) | buf[5]
            if length > self._max:
                self.bad_frames += 1
                del buf[:1]
                continue
            end = HEADER_SIZE + length
            if len(buf) < end + 1:
                break
            calc = 0
            for b in buf[2:end]:
                calc ^= b
            if calc != buf[end]:
                self.bad_frames += 1
                del buf[:1]  # 不是真帧头，往后继续找
                continue
            frames.append((buf[2], buf[3], bytes(buf[HEADER_SIZE:end])))
            del buf[: end + 1]
        return frames


class MuxChannelLink(Link):
    """汇聚流里的一路 (一个源端口)，对 Hub 来说就是一台只读串口设备。"""

    console_text = True
    idle_timeout = False

    def __init__(self, handler: LinkHandler, peer: str, transport: str,
                 on_close: Callable[["MuxChannelLink"], None]):
        super().__init__(handler, peer)
        self.transport = transport
        self._on_close = on_close

    def feed(self, data: bytes) -> None:
        self._feed(data)

    def _write_bytes(self, data: bytes) -> None:
        pass  # 汇聚器单向，丢弃 PC 的回复

    def _close_impl(self) -> None:
        self._on_close(self)


class MuxStream:
    """一条汇聚字节流 (一个 COM 口 / 一条 TCP 连接)：解帧、统计丢帧、按源端口分发到 MuxChannelLink。

    `name` 是设备地址的前缀，各路设备显示为 `<name>#ch<源端口>`。name 必须在重连后保持不变
    (串口名、PC 主动连接的 host:port、板子主动连接时的板子 IP)，结果看板才能把重连后的同一路对上。
    """

    kind = "汇聚器"

    def __init__(self, handler: LinkHandler, name: str, transport: str = "usb-serial-mux"):
        self.handler = handler
        self.name = name
        self.transport = transport
        self._decoder = MuxFrameDecoder()
        self._channels: dict[int, MuxChannelLink] = {}
        self._last_seq: dict[int, int] = {}
        self._lock = threading.Lock()
        self._closed = False
        self.state = "未连接"
        self.connected = False
        self.frames = 0
        self.lost_frames = 0
        self.rx_bytes = 0
        self.last_rx = 0.0

    # ---------------------------------------------------------------- 状态
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def bad_frames(self) -> int:
        return self._decoder.bad_frames

    @property
    def channel_count(self) -> int:
        with self._lock:
            return len(self._channels)

    def channels(self) -> list[int]:
        with self._lock:
            return sorted(self._channels)

    def _set_state(self, state: str, connected: bool) -> None:
        self.state = state
        self.connected = connected

    # ---------------------------------------------------------------- 数据
    def dispatch(self, data: bytes) -> None:
        self.rx_bytes += len(data)
        self.last_rx = time.time()
        for src, seq, payload in self._decoder.feed(data):
            self.frames += 1
            ch = self._channel(src)
            prev = self._last_seq.get(src)
            self._last_seq[src] = seq
            if prev is not None:
                gap = (seq - prev - 1) & 0xFF
                if gap:
                    self.lost_frames += gap
                    self.handler.on_link_message(ch, {
                        "type": protocol.MsgType.LOG, "tool": "serialmux", "level": "warn",
                        "text": f"汇聚器丢失 {gap} 帧 (序号 {prev} -> {seq})，这一路的输出可能不完整"})
            ch.feed(payload)

    def reset_stream(self) -> None:
        """换了一条新连接：丢掉半帧数据，序号重新开始计。"""
        self._decoder.reset()
        self._last_seq.clear()

    def _channel(self, src: int) -> MuxChannelLink:
        with self._lock:
            ch = self._channels.get(src)
            if ch is None or ch.closed:
                ch = MuxChannelLink(self.handler, f"{self.name}#ch{src}", self.transport, self._forget)
                self._channels[src] = ch
                new = True
            else:
                new = False
        if new:
            self.handler.on_link_opened(ch)
        return ch

    def _forget(self, ch: MuxChannelLink) -> None:
        with self._lock:
            for k, v in list(self._channels.items()):
                if v is ch:
                    del self._channels[k]
                    self._last_seq.pop(k, None)

    def close_channels(self) -> None:
        with self._lock:
            channels = list(self._channels.values())
        for ch in channels:
            ch.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._set_state("已关闭", False)
        self._close_impl()
        self.close_channels()

    def _close_impl(self) -> None:
        pass

    def start(self) -> None:
        pass


class SerialMuxReader(MuxStream):
    """汇聚器的串口输出：硬件串口 (/dev/ttyS32) 或 USB 虚拟串口 (/dev/ttyGS0)，PC 上都是 COM 口。"""

    kind = "串口"

    def __init__(self, handler: LinkHandler, port: str, baudrate: int = 115200, ser=None):
        super().__init__(handler, port, "usb-serial-mux")
        if ser is None:
            if serial is None:
                raise RuntimeError("未安装 pyserial，无法使用串口 (pip install pyserial)")
            ser = serial.Serial(port, baudrate=baudrate, timeout=0.2)
        self.port = port
        self._serial = ser
        self._set_state(f"已打开 {baudrate}", True)
        self._thread = threading.Thread(target=self._read_loop, name=f"serialmux-{port}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                data = self._serial.read(self._serial.in_waiting or 1)
                if data:
                    self.dispatch(data)
        except Exception as e:
            if not self.closed:
                log.info("汇聚串口 %s 读错误: %s", self.port, e)
        finally:
            self.close()

    def _close_impl(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass


class TcpMuxClient(MuxStream):
    """PC 主动连接汇聚器 (板子为 TCP 服务端，默认端口 9000)。断线后每 3 秒自动重连。

    host 为 127.0.0.1 时就是通过 USB 线 + `adb forward` 连接；`prepare` 在每次连接前调用
    (用于重新建立 adb forward，拔插 USB 后自动恢复)。
    """

    kind = "网络 (PC 连汇聚器)"

    def __init__(self, handler: LinkHandler, host: str, port: int = DEFAULT_MUX_TCP_PORT,
                 prepare: Callable[[], None] | None = None, kind: str = ""):
        loop = _is_loopback(host)
        super().__init__(handler, f"{host}:{port}", "usb-adb-mux" if loop else "network-mux")
        if kind:
            self.kind = kind
        self.host = host
        self.port = port
        self._prepare = prepare
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"muxtcp-{host}:{port}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        last_err = None
        while not self._stop.is_set():
            try:
                if self._prepare:
                    self._prepare()
                self._set_state("连接中…", False)
                sock = socket.create_connection((self.host, self.port), timeout=5)
                sock.settimeout(None)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception as e:
                err = str(e)
                self._set_state(f"连接失败，{RECONNECT_INTERVAL_S:.0f} 秒后重试: {err}", False)
                if err != last_err:
                    log.info("连接汇聚器 %s 失败: %s", self.name, err)
                    last_err = err
                self._stop.wait(RECONNECT_INTERVAL_S)
                continue
            last_err = None
            self._sock = sock
            self.reset_stream()
            self._set_state("已连接", True)
            self.handler_info(f"已连接汇聚器 {self.name}")
            got_data = False
            try:
                while not self._stop.is_set():
                    data = sock.recv(65536)
                    if not data:
                        break
                    got_data = True
                    self.dispatch(data)
            except OSError as e:
                if not self._stop.is_set():
                    log.info("汇聚器 %s 读错误: %s", self.name, e)
            finally:
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            if self._stop.is_set():
                break
            # 断线：这一路的设备下线 (结果看板标记离线)，重连后收到数据再上线
            self.close_channels()
            self._set_state(f"连接断开，{RECONNECT_INTERVAL_S:.0f} 秒后重连", False)
            if got_data:
                self.handler_info(f"汇聚器 {self.name} 连接断开，自动重连中")
            self._stop.wait(RECONNECT_INTERVAL_S)

    def handler_info(self, text: str) -> None:
        fn = getattr(self.handler, "info", None)
        if callable(fn):
            fn(text)

    def _close_impl(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class TcpMuxServer:
    """PC 等待汇聚器连进来 (板子为 TCP 客户端，填 PC 的 IP 和这里的端口)。

    可以同时接多台汇聚器；每台按来源 IP 区分 (`<IP>#ch<n>`)，同一 IP 重连时复用原来的设备名，
    旧连接直接关掉 (板子重启后旧 socket 可能还没超时)。
    """

    kind = "网络 (汇聚器连 PC)"

    def __init__(self, handler: LinkHandler, host: str = "0.0.0.0", port: int = DEFAULT_MUX_TCP_PORT):
        self.handler = handler
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._streams: dict[str, MuxStream] = {}
        self._conns: dict[str, socket.socket] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None

    # 让 UI 可以和 MuxStream 一样展示
    @property
    def name(self) -> str:
        return f"监听 {self.host}:{self.port}"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._conns)

    @property
    def state(self) -> str:
        if self._closed:
            return "已关闭"
        with self._lock:
            peers = list(self._conns)
        return f"已连接 {', '.join(peers)}" if peers else "等待汇聚器连接…"

    def streams(self) -> list[MuxStream]:
        with self._lock:
            return list(self._streams.values())

    def _sum(self, attr: str) -> int:
        return sum(getattr(s, attr) for s in self.streams())

    frames = property(lambda self: self._sum("frames"))
    lost_frames = property(lambda self: self._sum("lost_frames"))
    bad_frames = property(lambda self: self._sum("bad_frames"))
    channel_count = property(lambda self: self._sum("channel_count"))
    rx_bytes = property(lambda self: self._sum("rx_bytes"))

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(8)
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, name=f"muxsrv-{self.port}", daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        sock = self._sock
        while not self._closed and sock:
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn, addr[0]), name=f"muxsrv-{addr[0]}",
                             daemon=True).start()

    def _serve(self, conn: socket.socket, ip: str) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        with self._lock:
            old = self._conns.get(ip)
            self._conns[ip] = conn
            stream = self._streams.get(ip)
            if stream is None:
                stream = MuxStream(self.handler, ip, "usb-adb-mux" if _is_loopback(ip) else "network-mux")
                stream.kind = self.kind
                self._streams[ip] = stream
        if old is not None:
            try:
                old.close()  # 让旧连接的读线程退出
            except OSError:
                pass
        stream.reset_stream()
        stream._set_state("已连接", True)
        fn = getattr(self.handler, "info", None)
        if callable(fn):
            fn(f"汇聚器 {ip} 已连入 (端口 {self.port})")
        try:
            while not self._closed:
                data = conn.recv(65536)
                if not data:
                    break
                with self._lock:
                    if self._conns.get(ip) is not conn:
                        break  # 已被同 IP 的新连接取代
                stream.dispatch(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                superseded = self._conns.get(ip) is not conn
                if not superseded:
                    del self._conns[ip]
            if not superseded:
                stream._set_state("连接断开，等待重连", False)
                stream.close_channels()
                if callable(fn) and not self._closed:
                    fn(f"汇聚器 {ip} 连接断开")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns.values())
            streams = list(self._streams.values())
            self._conns.clear()
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        for s in streams:
            s.close()

"""SerialMux 汇聚串口：一个物理串口上承载最多 32 路被测板的 UART 输出。

SerialMux (longhrs/uart 仓库里的 Android App) 把 /dev/ttyS0..31 收到的字节按帧打包，
带上来源端口号后从一个输出口转发出来。每帧格式：

    A5 5A | 源端口号 (1) | 序号 (1) | 长度 (2, 大端) | payload (n) | XOR 校验 (1)

校验为从源端口号到 payload 末尾所有字节的 XOR。PC 端按源端口号拆开，每一路当成一台
独立的串口设备 (各自切行、各自出现在设备列表里)。汇聚口是单向的，PC 不向被测板回发任何数据。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .base import Link, LinkHandler
from .serial_link import serial

log = logging.getLogger(__name__)

SYNC = b"\xa5\x5a"
HEADER_SIZE = 6


class MuxFrameDecoder:
    """把汇聚口的字节流解成 (源端口号, payload)。校验失败时跳过同步字重新找帧。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.bad_frames = 0

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        frames: list[tuple[int, bytes]] = []
        buf = self._buf
        while True:
            idx = buf.find(SYNC)
            if idx < 0:
                # 只保留可能是半个同步字的最后一个字节
                del buf[: max(0, len(buf) - 1)]
                break
            if idx:
                del buf[:idx]
            if len(buf) < HEADER_SIZE:
                break
            length = (buf[4] << 8) | buf[5]
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
            frames.append((buf[2], bytes(buf[HEADER_SIZE:end])))
            del buf[: end + 1]
        return frames


class MuxChannelLink(Link):
    """汇聚口里的一路 (一个源端口)，对 Hub 来说就是一台只读串口设备。"""

    transport = "usb-serial-mux"
    console_text = True
    idle_timeout = False

    def __init__(self, handler: LinkHandler, peer: str, on_close: Callable[["MuxChannelLink"], None]):
        super().__init__(handler, peer)
        self._on_close = on_close

    def feed(self, data: bytes) -> None:
        self._feed(data)

    def _write_bytes(self, data: bytes) -> None:
        pass  # 汇聚口单向，丢弃 PC 的回复

    def _close_impl(self) -> None:
        self._on_close(self)


class SerialMuxReader:
    """打开汇聚串口，按源端口号把数据分发给各自的 MuxChannelLink (收到第一帧时创建)。"""

    def __init__(self, handler: LinkHandler, port: str, baudrate: int = 115200, ser=None):
        if ser is None:
            if serial is None:
                raise RuntimeError("未安装 pyserial，无法使用串口 (pip install pyserial)")
            ser = serial.Serial(port, baudrate=baudrate, timeout=0.2)
        self.handler = handler
        self.port = port
        self._serial = ser
        self._decoder = MuxFrameDecoder()
        self._channels: dict[int, MuxChannelLink] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._read_loop, name=f"serialmux-{port}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def dispatch(self, data: bytes) -> None:
        for src, payload in self._decoder.feed(data):
            self._channel(src).feed(payload)

    def _channel(self, src: int) -> MuxChannelLink:
        with self._lock:
            ch = self._channels.get(src)
            if ch is None or ch.closed:
                ch = MuxChannelLink(self.handler, f"{self.port}#ch{src}", self._forget)
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

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                data = self._serial.read(self._serial.in_waiting or 1)
                if data:
                    self.dispatch(data)
        except Exception as e:
            if not self._closed:
                log.info("汇聚串口 %s 读错误: %s", self.port, e)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._serial.close()
        except Exception:
            pass
        with self._lock:
            channels = list(self._channels.values())
        for ch in channels:
            ch.close()

"""TCP 服务端。

- Android 通过 WiFi / 以太网直接连接 PC 的 IP:端口 -> 连接方式记为 "network"
- Android 通过 USB 线，PC 执行 `adb reverse tcp:端口 tcp:端口` 后，
  Android 连接自己的 127.0.0.1:端口，流量由 adb 转发到 PC -> 对 PC 来说连接
  来自本机回环地址，连接方式记为 "usb-adb"
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading

from .base import Link, LinkHandler

log = logging.getLogger(__name__)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


class TcpLink(Link):
    def __init__(self, handler: LinkHandler, sock: socket.socket, addr):
        host, port = addr[0], addr[1]
        super().__init__(handler, f"{host}:{port}")
        self.transport = "usb-adb" if _is_loopback(host) else "network"
        self._sock = sock
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._thread = threading.Thread(target=self._read_loop, name=f"tcp-{self.link_id}", daemon=True)

    def start(self) -> None:
        self.handler.on_link_opened(self)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                data = self._sock.recv(65536)
                if not data:
                    break
                self._feed(data)
        except OSError as e:
            if not self.closed:
                log.info("连接 %s 读错误: %s", self.peer, e)
        finally:
            self.close()

    def _write_bytes(self, data: bytes) -> None:
        self._sock.sendall(data)

    def _close_impl(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


class TcpServer:
    def __init__(self, handler: LinkHandler, host: str = "0.0.0.0", port: int = 9527):
        self.handler = handler
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(16)
        self.port = sock.getsockname()[1]  # 端口为 0 时取实际分配的端口
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="tcp-accept", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self) -> None:
        sock = self._sock
        while self._running and sock:
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            TcpLink(self.handler, conn, addr).start()
        self._running = False

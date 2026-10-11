"""UDP 局域网发现：Android 广播 "H2W_DISCOVER"，PC 回复自身 TCP 端口。"""

from __future__ import annotations

import json
import logging
import socket
import threading

from .. import protocol

log = logging.getLogger(__name__)


class DiscoveryResponder:
    def __init__(self, tcp_port_getter, name: str = "H2wTool-PC", port: int = protocol.DEFAULT_DISCOVERY_PORT):
        self._tcp_port_getter = tcp_port_getter
        self.name = name
        self.port = port
        self._sock: socket.socket | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        self.port = sock.getsockname()[1]
        self._sock = sock
        self._running = True
        threading.Thread(target=self._loop, name="udp-discovery", daemon=True).start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _loop(self) -> None:
        sock = self._sock
        while self._running and sock:
            try:
                data, addr = sock.recvfrom(2048)
            except OSError:
                break
            if not data.strip().startswith(protocol.DISCOVERY_MAGIC.encode()):
                continue
            reply = {
                "type": "discover_reply",
                "name": self.name,
                "tcp_port": self._tcp_port_getter(),
                "protocol": protocol.PROTOCOL_VERSION,
            }
            try:
                sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except OSError as e:
                log.debug("发现回复失败 %s: %s", addr, e)
        self._running = False

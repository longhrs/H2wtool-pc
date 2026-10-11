"""传输链路抽象。每条物理连接 (TCP socket / 串口) 对应一个 Link。"""

from __future__ import annotations

import itertools
import threading
from typing import Protocol

from .. import protocol

_link_counter = itertools.count(1)


class LinkHandler(Protocol):
    def on_link_opened(self, link: "Link") -> None: ...
    def on_link_message(self, link: "Link", msg: dict) -> None: ...
    def on_link_error(self, link: "Link", error: str) -> None: ...
    def on_link_closed(self, link: "Link") -> None: ...


class Link:
    """一个双向消息链路。子类实现 _write_bytes / _close_impl，并在读线程里调用 _feed。"""

    transport = "unknown"

    def __init__(self, handler: LinkHandler, peer: str):
        self.link_id = f"L{next(_link_counter)}"
        self.handler = handler
        self.peer = peer
        self._decoder = protocol.LineDecoder()
        self._send_lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, msg: dict) -> None:
        data = protocol.encode(msg)
        with self._send_lock:
            if self._closed:
                raise ConnectionError("链路已关闭")
            self._write_bytes(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._close_impl()
        finally:
            self.handler.on_link_closed(self)

    def _feed(self, data: bytes) -> None:
        try:
            lines = self._decoder.feed(data)
        except protocol.ProtocolError as e:
            self.handler.on_link_error(self, str(e))
            return
        for line in lines:
            try:
                msg = protocol.decode_line(line)
            except protocol.ProtocolError as e:
                self.handler.on_link_error(self, f"{e}: {line[:200]!r}")
                continue
            self.handler.on_link_message(self, msg)

    def _write_bytes(self, data: bytes) -> None:
        raise NotImplementedError

    def _close_impl(self) -> None:
        raise NotImplementedError

"""USB 串口 (CDC-ACM / USB gadget serial / USB 转串口) 链路，依赖 pyserial。"""

from __future__ import annotations

import logging
import threading

from .base import Link, LinkHandler

log = logging.getLogger(__name__)

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except ImportError:  # pragma: no cover - 可选依赖
    serial = None
    list_ports = None


def serial_available() -> bool:
    return serial is not None


def list_serial_ports() -> list[tuple[str, str]]:
    """返回 [(设备名, 描述), ...]"""
    if list_ports is None:
        return []
    return [(p.device, p.description or "") for p in list_ports.comports()]


class SerialLink(Link):
    transport = "usb-serial"

    def __init__(self, handler: LinkHandler, port: str, baudrate: int = 115200):
        if serial is None:
            raise RuntimeError("未安装 pyserial，无法使用串口 (pip install pyserial)")
        super().__init__(handler, port)
        self._serial = serial.Serial(port, baudrate=baudrate, timeout=0.2)
        self._thread = threading.Thread(target=self._read_loop, name=f"serial-{port}", daemon=True)

    def start(self) -> None:
        self.handler.on_link_opened(self)
        self._thread.start()

    def _read_loop(self) -> None:
        try:
            while not self.closed:
                data = self._serial.read(self._serial.in_waiting or 1)
                if data:
                    self._feed(data)
        except Exception as e:  # serial.SerialException 等
            if not self.closed:
                log.info("串口 %s 读错误: %s", self.peer, e)
        finally:
            self.close()

    def _write_bytes(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def _close_impl(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass

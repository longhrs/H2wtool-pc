"""把后台线程里的事件安全地转发到 Qt 主线程。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..core.hub import Event


class EventBridge(QObject):
    hub_event = Signal(object)  # Event
    adb_event = Signal(str, str, object)  # level, text, devices

    def on_hub_event(self, ev: Event) -> None:
        self.hub_event.emit(ev)

    def on_adb_event(self, level: str, text: str, devices) -> None:
        self.adb_event.emit(level, text, devices)

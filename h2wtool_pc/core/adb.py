"""adb 辅助：列出 USB 连接的 Android 设备并建立 `adb reverse` 端口映射。

建立映射后，Android App 连接 127.0.0.1:<端口> 即可通过 USB 线与 PC 通信，
无需网络。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

log = logging.getLogger(__name__)

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW，避免弹黑框


@dataclass
class AdbDevice:
    serial: str
    state: str  # device / unauthorized / offline ...
    model: str = ""


def find_adb(custom_path: str = "") -> str | None:
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    found = shutil.which("adb")
    if found:
        return found
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            cand = os.path.join(root, "platform-tools", "adb.exe" if sys.platform == "win32" else "adb")
            if os.path.isfile(cand):
                return cand
    return None


class Adb:
    def __init__(self, path: str = ""):
        self.custom_path = path

    @property
    def path(self) -> str | None:
        return find_adb(self.custom_path)

    def _run(self, *args: str, timeout: float = 10) -> str:
        exe = self.path
        if not exe:
            raise FileNotFoundError("未找到 adb，请安装 Android platform-tools 或在设置中指定 adb 路径")
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip() or f"adb 返回码 {p.returncode}")
        return p.stdout

    def devices(self) -> list[AdbDevice]:
        return parse_devices(self._run("devices", "-l"))

    def reverse(self, serial: str, port: int) -> None:
        self._run("-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}")

    def reverse_list(self, serial: str) -> str:
        return self._run("-s", serial, "reverse", "--list")


def parse_devices(output: str) -> list[AdbDevice]:
    devs = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        devs.append(AdbDevice(serial=parts[0], state=parts[1], model=model))
    return devs


class AdbAutoReverse:
    """后台线程：定期扫描 adb 设备，对新出现的设备自动执行 adb reverse。"""

    def __init__(self, adb: Adb, port_getter, on_event, interval: float = 3.0):
        self.adb = adb
        self._port_getter = port_getter
        self._on_event = on_event  # fn(level, text, devices)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mapped: dict[str, int] = {}  # serial -> port
        self._last_error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="adb-auto-reverse", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def scan_once(self) -> list[AdbDevice]:
        port = self._port_getter()
        devices = self.adb.devices()
        online = {d.serial for d in devices if d.state == "device"}
        for serial in list(self._mapped):
            if serial not in online:
                del self._mapped[serial]
        for d in devices:
            if d.state != "device":
                continue
            if self._mapped.get(d.serial) == port:
                continue
            try:
                self.adb.reverse(d.serial, port)
                self._mapped[d.serial] = port
                self._on_event("info", f"adb reverse 已建立: {d.serial} {d.model} tcp:{port}", devices)
            except Exception as e:
                self._on_event("error", f"adb reverse 失败 {d.serial}: {e}", devices)
        return devices

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                devices = self.scan_once()
                self._last_error = ""
                self._on_event("devices", "", devices)
            except Exception as e:
                msg = str(e)
                if msg != self._last_error:  # 同样的错误只报一次
                    self._on_event("error", f"adb: {msg}", [])
                    self._last_error = msg
            self._stop.wait(self.interval)

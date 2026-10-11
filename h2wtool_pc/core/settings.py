"""应用设置与数据目录。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .. import protocol


def default_data_dir() -> Path:
    env = os.environ.get("H2WTOOL_DATA_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "H2wToolPC"
    return Path.home() / ".h2wtool_pc"


@dataclass
class Settings:
    bind_host: str = "0.0.0.0"
    tcp_port: int = protocol.DEFAULT_TCP_PORT
    discovery_enabled: bool = True
    discovery_port: int = protocol.DEFAULT_DISCOVERY_PORT
    adb_path: str = ""
    adb_auto_reverse: bool = True
    heartbeat_timeout_s: int = 30  # 超过该时间无任何数据则断开，0 = 不检测
    serial_baudrate: int = 115200
    auto_start_server: bool = True

    @classmethod
    def load(cls, path: Path) -> "Settings":
        s = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return s
        valid = {f.name: f.type for f in fields(cls)}
        for k, v in data.items():
            if k in valid and isinstance(v, type(getattr(s, k))):
                setattr(s, k, v)
        return s

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

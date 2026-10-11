"""H2W PC <-> Android 通信协议 (v1)。

传输层无关：TCP (网卡 / adb reverse 的 USB) 和串口 (USB CDC) 都使用同一套
"JSON Lines" 帧格式 —— 每条消息是一个 UTF-8 编码的 JSON 对象，以 '\\n' 结尾。

详细说明见 docs/PROTOCOL.md。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

PROTOCOL_VERSION = 1
DEFAULT_TCP_PORT = 9527
DEFAULT_DISCOVERY_PORT = 9528
DISCOVERY_MAGIC = "H2W_DISCOVER"

# 单条消息最大字节数（包括 base64 文件内容）
MAX_LINE_BYTES = 16 * 1024 * 1024

# 与控制台文本混在同一串口上的协议消息 (bootloader 等不能独占串口的 stub) 以此前缀开头，
# 例如 `@H2W {"type":"result",...}`。前缀前允许有时间戳等杂字符。
TAG_PREFIX = b"@H2W "


class MsgType:
    # Android -> PC
    HELLO = "hello"
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    RESULT = "result"
    LOG = "log"
    CONFIG_REPORT = "config_report"
    FILE = "file"
    # PC -> Android
    WELCOME = "welcome"
    CONFIG_SET = "config_set"
    CONFIG_GET = "config_get"
    COMMAND = "command"
    # 双向
    ACK = "ack"


class ProtocolError(Exception):
    pass


def new_msg_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> int:
    return int(time.time() * 1000)


def make_message(msg_type: str, **fields: Any) -> dict:
    msg = {"type": msg_type, "id": new_msg_id(), "ts": now_ms()}
    msg.update(fields)
    return msg


def make_ack(ref: str | None, ok: bool = True, error: str | None = None) -> dict:
    fields: dict[str, Any] = {"ref": ref, "ok": ok}
    if error:
        fields["error"] = error
    return make_message(MsgType.ACK, **fields)


def encode(msg: dict) -> bytes:
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return data + b"\n"


def decode_line(line: bytes) -> dict:
    text = line.decode("utf-8", errors="replace").strip()
    if not text:
        raise ProtocolError("空消息")
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON 解析失败: {e}") from None
    if not isinstance(msg, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    if not isinstance(msg.get("type"), str):
        raise ProtocolError("消息缺少 type 字段")
    return msg


def split_tagged(line: bytes) -> bytes | None:
    """行里带 TAG_PREFIX 时返回前缀之后的 JSON 部分，否则返回 None。"""
    idx = line.find(TAG_PREFIX)
    return None if idx < 0 else line[idx + len(TAG_PREFIX):]


def console_log_message(line: bytes) -> dict:
    """把一行控制台文本包装成 log 消息 (tool=console)。"""
    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
    return {"type": MsgType.LOG, "tool": "console", "level": "debug", "text": text}


class LineDecoder:
    """把字节流切成行。超过 MAX_LINE_BYTES 仍没有换行则抛 ProtocolError。"""

    def __init__(self, max_line: int = MAX_LINE_BYTES):
        self._buf = bytearray()
        self._max = max_line

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        lines = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if line.strip():
                lines.append(line)
        if len(self._buf) > self._max:
            self._buf.clear()
            raise ProtocolError(f"单条消息超过 {self._max} 字节")
        return lines

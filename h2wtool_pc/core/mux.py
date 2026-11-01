"""SerialMux 汇聚器的连接方式 (GUI 对话框、启动自动连接、命令行共用)。

连接参数用一个 dict 表示 (会存进 settings.json 的 mux_last)：

    {"mode": "tcp",    "host": "192.168.1.20", "port": 9000}            PC 连板子 (板子 = TCP 服务端)
    {"mode": "listen", "port": 9000}                                    板子连 PC (板子 = TCP 客户端)
    {"mode": "adb",    "adb_serial": "", "port": 9000, "local_port": 9000}  USB 线 + adb forward
    {"mode": "serial", "serial_port": "COM5", "baud": 115200}           硬件串口 / USB 虚拟串口
"""

from __future__ import annotations

from typing import Callable

from ..transport import DEFAULT_MUX_TCP_PORT
from .adb import Adb

MODES = {
    "tcp": "网络：PC 连接汇聚器 (汇聚器设为 TCP 服务端)",
    "listen": "网络：等待汇聚器连接 PC (汇聚器设为 TCP 客户端)",
    "adb": "USB 线：adb forward (汇聚器设为 TCP 服务端，无需 USB gadget)",
    "serial": "串口：硬件串口或 USB 虚拟串口 (COM 口)",
}


def parse_host_port(text: str, default_port: int = DEFAULT_MUX_TCP_PORT) -> tuple[str, int]:
    text = text.strip()
    if text.startswith("["):  # [IPv6]:port
        host, _, rest = text[1:].partition("]")
        return host, int(rest.lstrip(":") or default_port)
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host.strip(), int(port)
    return text, default_port


def describe(cfg: dict) -> str:
    mode = cfg.get("mode")
    if mode == "tcp":
        return f"网络 {cfg.get('host')}:{cfg.get('port', DEFAULT_MUX_TCP_PORT)}"
    if mode == "listen":
        return f"监听端口 {cfg.get('port', DEFAULT_MUX_TCP_PORT)}"
    if mode == "adb":
        return f"USB adb forward {cfg.get('adb_serial') or '(唯一设备)'} 端口 {cfg.get('port', DEFAULT_MUX_TCP_PORT)}"
    if mode == "serial":
        return f"串口 {cfg.get('serial_port')}"
    return str(cfg)


def open_mux_source(hub, cfg: dict, adb: Adb | None = None,
                    on_adb_error: Callable[[str], None] | None = None):
    """按 cfg 打开一个汇聚器来源，返回 hub 里的 reader 对象。参数不合法时抛 ValueError。"""
    mode = cfg.get("mode")
    port = int(cfg.get("port") or DEFAULT_MUX_TCP_PORT)
    if mode == "tcp":
        host = str(cfg.get("host") or "").strip()
        if not host:
            raise ValueError("请填写汇聚器的 IP 地址")
        return hub.connect_mux_tcp(host, port)
    if mode == "listen":
        return hub.listen_mux_tcp(str(cfg.get("bind_host") or "0.0.0.0"), port)
    if mode == "adb":
        adb = adb or Adb()
        serial = str(cfg.get("adb_serial") or "")
        local = int(cfg.get("local_port") or port)
        state = {"err": None}

        def prepare() -> None:
            # 每次 (重) 连接前都建一次映射：拔插 USB / 重启板子后 forward 会消失
            try:
                adb.forward(serial, local, port)
                state["err"] = None
            except Exception as e:
                msg = f"adb forward 失败: {e}"
                if on_adb_error and msg != state["err"]:
                    on_adb_error(msg)
                state["err"] = msg
                raise

        reader = hub.connect_mux_tcp("127.0.0.1", local, prepare=prepare)
        reader.kind = f"USB adb ({serial})" if serial else "USB (adb forward)"
        return reader
    if mode == "serial":
        sp = str(cfg.get("serial_port") or "").strip()
        if not sp:
            raise ValueError("请选择串口")
        return hub.open_serial_mux(sp, int(cfg.get("baud") or 115200))
    raise ValueError(f"未知的汇聚器连接方式 {mode!r}")


def mux_configs_from_args(args, settings) -> list[dict]:
    """命令行里的汇聚器参数 -> core.mux 的连接参数列表。"""
    cfgs = [{"mode": "serial", "serial_port": p, "baud": settings.serial_baudrate}
            for p in getattr(args, "serial_mux", []) or []]
    for text in getattr(args, "mux_tcp", []) or []:
        host, port = parse_host_port(text)
        cfgs.append({"mode": "tcp", "host": host, "port": port})
    cfgs += [{"mode": "listen", "port": p} for p in getattr(args, "mux_listen", []) or []]
    used = set()
    for i, serial in enumerate(getattr(args, "mux_adb", []) or []):
        local = 9000 + i  # 多台设备各占一个本地端口
        while local in used:
            local += 1
        used.add(local)
        cfgs.append({"mode": "adb", "adb_serial": serial or "", "port": 9000, "local_port": local})
    return cfgs


def open_mux_args(hub, args, settings, report_error) -> None:
    adb = Adb(settings.adb_path)
    for cfg in mux_configs_from_args(args, settings):
        try:
            open_mux_source(hub, cfg, adb, report_error)
        except Exception as e:
            report_error(f"打开汇聚器 {describe(cfg)} 失败: {e}")

#!/usr/bin/env python3
"""Android 端模拟器：按 docs/PROTOCOL.md 与 PC 工具通信，用于联调/演示。

    python tools/device_simulator.py --host 127.0.0.1 --port 9527
    python tools/device_simulator.py --discover            # 通过 UDP 广播找到 PC
    python tools/device_simulator.py --auto memtest --count 3 --fail-rate 0.3

收到 config_set 会保存参数并 ack；收到 config_get 会回 config_report；
收到 command start 会模拟进度并上报 result；command stop 会中止。
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from h2wtool_pc import protocol  # noqa: E402
from h2wtool_pc.__main__ import force_utf8_stdio  # noqa: E402
from h2wtool_pc.core.config_schema import load_schemas  # noqa: E402
from h2wtool_pc.protocol import MsgType  # noqa: E402


def discover(port: int, timeout: float = 3.0) -> tuple[str, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    s.sendto(protocol.DISCOVERY_MAGIC.encode(), ("255.255.255.255", port))
    try:
        data, addr = s.recvfrom(2048)
    finally:
        s.close()
    reply = json.loads(data)
    return addr[0], int(reply["tcp_port"])


class SimDevice:
    def __init__(self, sock: socket.socket, device_id: str, model: str, fail_rate: float, speed: float):
        self.sock = sock
        self.device_id = device_id
        self.model = model
        self.fail_rate = fail_rate
        self.speed = speed
        self.configs = {t: s.defaults() for t, s in load_schemas().items()}
        self.lock = threading.Lock()
        self.running: dict[str, threading.Event] = {}
        self.alive = True
        self.done = threading.Event()

    def send(self, msg_type: str, **fields) -> dict:
        msg = protocol.make_message(msg_type, **fields)
        with self.lock:
            self.sock.sendall(protocol.encode(msg))
        return msg

    def log(self, text: str) -> None:
        print(f"[{self.device_id}] {text}", flush=True)

    # ------------------------------------------------------------
    def run(self, auto_tool: str | None, count: int) -> None:
        self.send(MsgType.HELLO, device_id=self.device_id, model=self.model, android_version="14",
                  app_version="sim-1.0", tools=list(self.configs), serial_no=self.device_id,
                  battery=87)
        threading.Thread(target=self._heartbeat, daemon=True).start()
        if auto_tool:
            threading.Thread(target=self._auto, args=(auto_tool, count), daemon=True).start()
        dec = protocol.LineDecoder()
        try:
            while self.alive:
                data = self.sock.recv(65536)
                if not data:
                    break
                for line in dec.feed(data):
                    self.handle(protocol.decode_line(line))
        except OSError:
            pass
        self.alive = False
        self.log("连接已断开")

    def _heartbeat(self) -> None:
        while self.alive:
            time.sleep(5)
            try:
                self.send(MsgType.HEARTBEAT)
            except OSError:
                return

    def _auto(self, tool: str, count: int) -> None:
        time.sleep(0.5)
        for _ in range(count):
            self.run_test(tool)
        self.done.set()

    def handle(self, msg: dict) -> None:
        t = msg["type"]
        if t == MsgType.WELCOME:
            self.log(f"握手成功: {msg.get('server')} {msg.get('server_version')}")
        elif t == MsgType.CONFIG_SET:
            tool = msg.get("tool")
            if tool not in self.configs:
                self.send(MsgType.ACK, ref=msg["id"], ok=False, error=f"不支持的工具 {tool}")
                return
            self.configs[tool].update(msg.get("config") or {})
            self.log(f"收到 {tool} 参数: {msg.get('config')}")
            self.send(MsgType.ACK, ref=msg["id"], ok=True)
        elif t == MsgType.CONFIG_GET:
            tool = msg.get("tool")
            self.send(MsgType.ACK, ref=msg["id"], ok=tool in self.configs)
            if tool in self.configs:
                self.send(MsgType.CONFIG_REPORT, ref=msg["id"], tool=tool, config=self.configs[tool])
        elif t == MsgType.COMMAND:
            tool, action = msg.get("tool"), msg.get("action")
            if action == "start":
                if tool in self.running:
                    self.send(MsgType.ACK, ref=msg["id"], ok=False, error="测试已在运行")
                    return
                self.send(MsgType.ACK, ref=msg["id"], ok=True)
                threading.Thread(target=self.run_test, args=(tool,), daemon=True).start()
            elif action == "stop":
                ev = self.running.get(tool)
                if ev:
                    ev.set()
                self.send(MsgType.ACK, ref=msg["id"], ok=True)
            else:
                self.send(MsgType.ACK, ref=msg["id"], ok=False, error=f"未知命令 {action}")
        elif t == MsgType.ACK:
            pass

    def run_test(self, tool: str) -> None:
        stop = threading.Event()
        self.running[tool] = stop
        cfg = dict(self.configs.get(tool, {}))
        test_id = uuid.uuid4().hex[:16]
        start = protocol.now_ms()
        self.send(MsgType.LOG, tool=tool, level="info", text=f"开始测试 test_id={test_id} 参数={cfg}")
        steps = 10
        aborted = False
        for i in range(steps + 1):
            if stop.is_set():
                aborted = True
                break
            self.send(MsgType.STATUS, tool=tool, state="running", progress=i * 100 // steps,
                      message=f"第 {i}/{steps} 步")
            time.sleep(self.speed)
        fail = (not aborted) and random.random() < self.fail_rate
        end = protocol.now_ms()
        if tool == "memtest":
            summary = {"size_mb": cfg.get("size_mb"), "loops": cfg.get("loops"), "errors": 3 if fail else 0}
            details = {"failed_tests": ["walking_ones"] if fail else [],
                       "first_error_addr": "0x7f3a2c1000" if fail else None}
        else:
            summary = {"tested_mb": cfg.get("test_size_mb") or 8192, "write_mbps": round(random.uniform(80, 200), 1),
                       "read_mbps": round(random.uniform(150, 300), 1), "bad_mb": 2 if fail else 0}
            details = {"mode": cfg.get("mode"), "first_bad_offset": 123456789 if fail else None}
        result = "ABORT" if aborted else ("FAIL" if fail else "PASS")
        if fail:
            self.send(MsgType.LOG, tool=tool, level="error", text="检测到数据校验错误")
        self.send(MsgType.STATUS, tool=tool, state="stopped" if aborted else "done", progress=100)
        self.send(MsgType.RESULT, tool=tool, test_id=test_id, result=result, start_time=start, end_time=end,
                  duration_s=round((end - start) / 1000, 2), summary=summary, details=details, config=cfg)
        self.log(f"{tool} 结果 {result}")
        self.running.pop(tool, None)


def main() -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description="H2wTool Android 端模拟器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=protocol.DEFAULT_TCP_PORT)
    ap.add_argument("--discover", action="store_true", help="通过 UDP 广播发现 PC")
    ap.add_argument("--device-id", default=None)
    ap.add_argument("--model", default="SIM-Phone")
    ap.add_argument("--auto", choices=["memtest", "h2wflashtool"], help="连接后自动执行测试")
    ap.add_argument("--count", type=int, default=1, help="--auto 时执行次数")
    ap.add_argument("--fail-rate", type=float, default=0.2)
    ap.add_argument("--speed", type=float, default=0.3, help="每个进度步骤的秒数")
    ap.add_argument("--exit-after-auto", action="store_true", help="--auto 完成后退出")
    args = ap.parse_args()

    host, port = args.host, args.port
    if args.discover:
        host, port = discover(protocol.DEFAULT_DISCOVERY_PORT)
        print(f"发现 PC: {host}:{port}")
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(None)
    dev = SimDevice(sock, args.device_id or f"SIM{uuid.uuid4().hex[:6].upper()}", args.model,
                    args.fail_rate, args.speed)
    if args.exit_after_auto and args.auto:
        runner = threading.Thread(target=dev.run, args=(args.auto, args.count), daemon=True)
        runner.start()
        dev.done.wait()
        time.sleep(0.5)  # 等最后的 ack 到达
        dev.alive = False
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        # 等接收线程结束再退出，否则它还在打印时解释器关闭会触发 Fatal Python error (Windows)
        runner.join(timeout=5)
        return 0
    try:
        dev.run(args.auto, args.count)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

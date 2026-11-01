#!/usr/bin/env python3
"""SerialMux 汇聚器模拟器：不接真板子也能试 PC 工具的“连接汇聚器”。

模拟 longhrs/uart 的 SerialMux App：N 路 UART (默认 4 路) 各自跑一遍 memtest 风格的输出
(控制台文本 + `@H2W` 结果行)，打成 A5 5A 帧后从 TCP 发出去。

    python tools/serialmux_simulator.py --server 9000          # 汇聚器当 TCP 服务端，PC 选“PC 连接汇聚器” 127.0.0.1:9000
    python tools/serialmux_simulator.py --client 127.0.0.1:9000 # 汇聚器当 TCP 客户端，PC 选“等待汇聚器连接”端口 9000
    python tools/serialmux_simulator.py --server 9000 --ports 16 --fail 3,7 --loop
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time


def frame(port: int, seq: int, payload: bytes) -> bytes:
    body = bytes([port, seq & 0xFF, len(payload) >> 8, len(payload) & 0xFF]) + payload
    x = 0
    for b in body:
        x ^= b
    return b"\xa5\x5a" + body + bytes([x])


def h2w(msg: dict) -> bytes:
    return b"@H2W " + json.dumps(msg, ensure_ascii=False).encode() + b"\r\n"


def board_script(port: int, fail: bool) -> list[bytes]:
    tests = ["Random value", "XOR comparison", "Solid bits", "Bit flip", "Walking ones"]
    out = [f"\r\nU-Boot / LK boot, uart port {port}\r\nDDR mem test start\r\n".encode(),
           h2w({"type": "hello", "model": f"Board-{port:02d}", "tools": ["memtest"], "passive": True,
                "ddr_size_mb": 4096})]
    for i, t in enumerate(tests, 1):
        out.append(f"test {i}/{len(tests)} {t} ... ok\r\n".encode())
        out.append(h2w({"type": "status", "tool": "memtest", "state": "running",
                        "progress": i * 100 // len(tests), "message": f"{i}/{len(tests)} {t}"}))
    if fail:
        out.append(b"memtest Bit flip failed: 0x82000000 act:0x1 exp:0x0\r\n")
    out.append(h2w({"type": "result", "tool": "memtest", "test_id": f"sim-{port}-{int(time.time())}",
                    "result": "FAIL" if fail else "PASS", "duration_s": 5,
                    "summary": {"size_mb": 3072, "tests": len(tests), "tests_failed": int(fail)}}))
    return out


def run(sock: socket.socket, ports: int, fails: set[int], loop: bool) -> None:
    seq = [0] * ports
    while True:
        scripts = {p: board_script(p, p in fails) for p in range(ports)}
        while any(scripts.values()):
            for p in range(ports):
                if not scripts[p]:
                    continue
                data = scripts[p].pop(0)
                for i in range(0, len(data), 64):  # 模拟按帧间隔切开的输出
                    sock.sendall(frame(p, seq[p], data[i:i + 64]))
                    seq[p] = (seq[p] + 1) & 0xFF
            time.sleep(random.uniform(0.2, 0.6))
        print(f"一轮完成：{ports} 路，失败 {sorted(fails) or '无'}", flush=True)
        if not loop:
            time.sleep(1)
            return
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--server", type=int, metavar="PORT", help="当 TCP 服务端，等 PC 连进来")
    g.add_argument("--client", metavar="HOST:PORT", help="当 TCP 客户端，连接 PC")
    ap.add_argument("--ports", type=int, default=4, help="模拟几路 UART (1-32)")
    ap.add_argument("--fail", default="", help="哪些路报 FAIL，例如 1,5")
    ap.add_argument("--loop", action="store_true", help="一直循环测试")
    a = ap.parse_args()
    fails = {int(x) for x in a.fail.split(",") if x.strip()}
    ports = max(1, min(32, a.ports))
    if a.server:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", a.server))
        srv.listen(1)
        print(f"汇聚器模拟器监听 :{a.server}，等待 PC 连接…", flush=True)
        sock, addr = srv.accept()
        print(f"PC 已连接 {addr[0]}", flush=True)
    else:
        host, _, port = a.client.rpartition(":")
        sock = socket.create_connection((host, int(port)), timeout=10)
        print(f"已连接 PC {a.client}", flush=True)
    try:
        run(sock, ports, fails, a.loop)
    except (BrokenPipeError, ConnectionError) as e:
        print(f"连接断开: {e}", flush=True)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

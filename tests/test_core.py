import json
import socket
import time

import pytest

from h2wtool_pc import protocol
from h2wtool_pc.core.adb import parse_devices
from h2wtool_pc.core.config_schema import PresetStore, ToolSchema, load_schemas
from h2wtool_pc.core.hub import Hub
from h2wtool_pc.core.result_store import ResultStore, normalize_result
from h2wtool_pc.core.settings import Settings
from h2wtool_pc.protocol import MsgType


# ---------------------------------------------------------------- protocol
def test_encode_decode_roundtrip():
    msg = protocol.make_message(MsgType.RESULT, tool="memtest", text="中文\n换行")
    data = protocol.encode(msg)
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    dec = protocol.LineDecoder()
    lines = dec.feed(data[:5]) + dec.feed(data[5:])
    assert len(lines) == 1
    assert protocol.decode_line(lines[0]) == msg


def test_decoder_errors():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_line(b"[1,2]")
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_line(b'{"no_type":1}')
    dec = protocol.LineDecoder(max_line=10)
    with pytest.raises(protocol.ProtocolError):
        dec.feed(b"x" * 20)
    assert dec.feed(b'{"type":"a"}\n') == [b'{"type":"a"}']


# ---------------------------------------------------------------- schema
def test_builtin_schemas_load_and_defaults_valid():
    schemas = load_schemas()
    assert {"memtest", "h2wflashtool"} <= set(schemas)
    for s in schemas.values():
        cfg, errors = s.validate(s.defaults())
        assert errors == {}
        assert cfg == s.defaults()


def test_schema_validation():
    s = load_schemas()["memtest"]
    cfg, errors = s.validate({"size_mb": "1024", "loops": -1, "stop_on_error": "false",
                              "tests": ["walking_ones", "stuck_address", "walking_ones"], "extra": 1})
    assert cfg["size_mb"] == 1024
    assert "loops" in errors
    assert cfg["stop_on_error"] is False
    assert cfg["tests"] == ["stuck_address", "walking_ones"]  # 去重并按 schema 顺序
    assert cfg["extra"] == 1  # 未知字段保留
    _, errors = s.validate({"tests": ["nope"]})
    assert "tests" in errors

    f = load_schemas()["h2wflashtool"]
    cfg, errors = f.validate({"block_size_kb": "4096", "mode": "write_only"})
    assert errors == {} and cfg["block_size_kb"] == 4096


def test_user_schema_override(tmp_path):
    (tmp_path / "memtest.json").write_text(json.dumps({
        "tool": "memtest", "title": "自定义", "fields": [{"key": "x", "type": "int", "default": 3}]}),
        encoding="utf-8")
    schemas = load_schemas(tmp_path)
    assert schemas["memtest"].title == "自定义"
    assert schemas["memtest"].defaults() == {"x": 3}
    assert "h2wflashtool" in schemas


def test_presets(tmp_path):
    p = PresetStore(tmp_path)
    p.save("memtest", "快速/测试", {"size_mb": 64})
    assert p.list("memtest") == ["快速_测试"]
    assert p.load("memtest", "快速/测试") == {"size_mb": 64}
    p.delete("memtest", "快速/测试")
    assert p.list("memtest") == []


def test_tool_schema_rejects_bad_type():
    with pytest.raises(Exception):
        ToolSchema.from_dict({"tool": "t", "fields": [{"key": "a", "type": "weird"}]})


# ---------------------------------------------------------------- store
def test_result_store_dedup_and_query(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    msg = {"type": "result", "tool": "memtest", "test_id": "t1", "result": "passed",
           "summary": {"errors": 0}, "start_time": 1700000000000, "duration_s": "12.5"}
    rid, new = store.add_from_message(msg, "dev1", "M1", "network")
    assert new
    rid2, new2 = store.add_from_message(msg, "dev1", "M1", "network")
    assert rid2 == rid and not new2
    store.add_from_message({"tool": "h2wflashtool", "result": "NG"}, "dev2")
    assert len(store.query()) == 2
    assert store.query(result="FAIL")[0]["device_id"] == "dev2"
    row = store.get(rid)
    assert row["result"] == "PASS" and row["duration_s"] == 12.5 and row["summary"] == {"errors": 0}
    out = tmp_path / "x.csv"
    ResultStore.export_csv(out, store.query())
    assert "dev1" in out.read_text(encoding="utf-8-sig")
    assert normalize_result("weird") == "ERROR"
    store.close()


def test_parse_adb_devices():
    out = ("List of devices attached\n"
           "R58M123 device usb:1-1 product:x model:SM_G991 device:y transport_id:1\n"
           "emulator-5554 unauthorized\n\n")
    devs = parse_devices(out)
    assert [(d.serial, d.state, d.model) for d in devs] == [
        ("R58M123", "device", "SM_G991"), ("emulator-5554", "unauthorized", "")]


def test_settings_roundtrip(tmp_path):
    s = Settings(tcp_port=1234, adb_auto_reverse=False)
    s.save(tmp_path / "s.json")
    s2 = Settings.load(tmp_path / "s.json")
    assert s2.tcp_port == 1234 and s2.adb_auto_reverse is False
    (tmp_path / "bad.json").write_text('{"tcp_port": "x"}')
    assert Settings.load(tmp_path / "bad.json").tcp_port == protocol.DEFAULT_TCP_PORT


# ---------------------------------------------------------------- hub 端到端
class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.dec = protocol.LineDecoder()
        self.pending = []

    def send(self, t, **kw):
        msg = protocol.make_message(t, **kw)
        self.sock.sendall(protocol.encode(msg))
        return msg

    def recv(self, timeout=5):
        deadline = time.time() + timeout
        while not self.pending:
            self.sock.settimeout(max(0.01, deadline - time.time()))
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError
            self.pending += [protocol.decode_line(x) for x in self.dec.feed(data)]
        return self.pending.pop(0)


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def hub(tmp_path):
    store = ResultStore(tmp_path / "r.db")
    h = Hub(tmp_path, store, load_schemas())
    events = []
    h.add_listener(events.append)
    h.events = events
    h.start_server("127.0.0.1", 0, discovery=False)
    yield h
    h.shutdown()
    store.close()


def test_hub_end_to_end(hub, tmp_path):
    c = Client(hub.tcp_port)
    hello = c.send(MsgType.HELLO, device_id="D1", model="Pixel", android_version="14", tools=["memtest"])
    welcome = c.recv()
    assert welcome["type"] == MsgType.WELCOME and welcome["ref"] == hello["id"]
    assert wait_for(lambda: hub.devices() and hub.devices()[0].device_id == "D1")
    dev = hub.devices()[0]
    assert dev.transport == "usb-adb"  # 来自回环地址

    # PC -> 设备 下发参数
    mid = hub.send_config(dev.key, "memtest", {"size_mb": 256})
    m = c.recv()
    assert m["type"] == MsgType.CONFIG_SET and m["id"] == mid
    assert m["config"]["size_mb"] == 256 and "tests" in m["config"]  # 补全默认值
    c.send(MsgType.ACK, ref=mid, ok=True)
    assert wait_for(lambda: any(e.kind == "ack" and e.data["request"].get("tool") == "memtest"
                                for e in hub.events))
    with pytest.raises(ValueError):
        hub.send_config(dev.key, "memtest", {"size_mb": -5})

    # 命令
    cid = hub.send_command(dev.key, "memtest", "start")
    m = c.recv()
    assert m["type"] == MsgType.COMMAND and m["action"] == "start" and m["id"] == cid

    # 设备 -> PC 状态/结果/文件
    c.send(MsgType.STATUS, tool="memtest", state="running", progress=50)
    r = c.send(MsgType.RESULT, tool="memtest", test_id="abc", result="FAIL", summary={"errors": 2})
    ack = c.recv()
    assert ack["type"] == MsgType.ACK and ack["ref"] == r["id"] and ack["ok"]
    rows = hub.store.query()
    assert len(rows) == 1 and rows[0]["result"] == "FAIL" and rows[0]["device_model"] == "Pixel"

    import base64
    c.send(MsgType.FILE, tool="memtest", name="../evil log.txt", data_b64=base64.b64encode(b"hello").decode())
    ack = c.recv()
    assert ack["ok"]
    files = list((tmp_path / "files" / "D1").iterdir())
    assert len(files) == 1 and files[0].read_bytes() == b"hello" and ".." not in files[0].name

    # 坏消息不会断开连接
    c.sock.sendall(b"not json\n")
    c.send(MsgType.HEARTBEAT)
    assert wait_for(lambda: any(e.kind == "error" and "协议错误" in e.data["text"] for e in hub.events))
    assert len(hub.devices()) == 1

    # result 缺少 tool -> 回 ack ok=false
    c.send(MsgType.RESULT, result="PASS")
    ack = c.recv()
    assert ack["type"] == MsgType.ACK and ack["ok"] is False

    c.sock.close()
    assert wait_for(lambda: not hub.devices())
    kinds = [e.kind for e in hub.events]
    assert "device_added" in kinds and "result" in kinds and "device_removed" in kinds


def test_hub_heartbeat_timeout(hub):
    hub.heartbeat_timeout_s = 1
    c = Client(hub.tcp_port)
    c.send(MsgType.HELLO, device_id="D2")
    c.recv()
    assert wait_for(lambda: len(hub.devices()) == 1)
    assert wait_for(lambda: not hub.devices(), timeout=6)


def test_discovery(tmp_path):
    from h2wtool_pc.transport import DiscoveryResponder
    d = DiscoveryResponder(lambda: 4321, port=0)
    d.start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(protocol.DISCOVERY_MAGIC.encode(), ("127.0.0.1", d.port))
        data, _ = s.recvfrom(2048)
        assert json.loads(data)["tcp_port"] == 4321
        s.close()
    finally:
        d.stop()


# ---------------------------------------------------------------- 串口控制台 stub (bootloader memtest)
def test_split_tagged_and_console_log():
    assert protocol.split_tagged(b'[12.3] @H2W {"type":"status"}') == b'{"type":"status"}'
    assert protocol.split_tagged(b"DDR mem test start") is None
    m = protocol.console_log_message(b"DDR freq:3\r")
    assert m["type"] == MsgType.LOG and m["tool"] == "console" and m["text"] == "DDR freq:3"


class FakeConsoleLink:
    """模拟接在 bootloader 控制台上的串口：记录 PC 写出的字节。"""

    @staticmethod
    def make(handler):
        from h2wtool_pc.transport import Link

        class _L(Link):
            transport = "usb-serial"
            console_text = True
            idle_timeout = False

            def __init__(self):
                super().__init__(handler, "COM9")
                self.written = []

            def _write_bytes(self, data):
                self.written.append(data)

            def _close_impl(self):
                pass

        link = _L()
        handler.on_link_opened(link)
        return link


MEMTEST_UART = (
    b"DDR mem test start\r\n"
    b'@H2W {"type":"hello","model":"UNISOC LK","tools":["memtest"],"passive":true,"ddr_size_mb":4096}\r\n'
    b"{not json, a console line that starts with a brace\r\n"
    b'@H2W {"type":"status","tool":"memtest","state":"running","progress":33,"message":"2/3 Stuck address"}\r\n'
    b"memtest Stuck address failed: 0x82000000 act:0x1 exp:0x0\r\n"
    b'@H2W {"type":"result","tool":"memtest","result":"FAIL","duration_s":95,'
    b'"summary":{"size_mb":2048,"tests":3,"tests_failed":1,"errors":1},"details":{"first_test":"Stuck address"}}\r\n'
)


def test_hub_serial_console_stub(hub):
    hub.heartbeat_timeout_s = 1
    link = FakeConsoleLink.make(hub)
    link._feed(MEMTEST_UART[:37])
    link._feed(MEMTEST_UART[37:])
    dev = hub.device(link.link_id)
    assert dev.greeted and dev.passive and dev.model == "UNISOC LK"
    rows = hub.store.query()
    assert len(rows) == 1 and rows[0]["result"] == "FAIL" and rows[0]["device_id"] == "COM9"
    assert not any(e.kind == "error" for e in hub.events)
    assert sum(e.kind == "log" and e.data["tool"] == "console" for e in hub.events) == 3
    assert link.written == []  # 不向 bootloader 控制台回写 (welcome/ack 都不发)
    with pytest.raises(ValueError):
        hub.send_command(link.link_id, "memtest", "start")
    time.sleep(2.5)  # 超过心跳超时也不断开
    assert hub.device(link.link_id) is not None


def test_serial_android_app_still_gets_replies(hub):
    link = FakeConsoleLink.make(hub)
    link._feed(protocol.encode(protocol.make_message(MsgType.HELLO, device_id="D9")))
    assert not hub.device(link.link_id).passive
    assert json.loads(link.written[0])["type"] == MsgType.WELCOME


# ---------------------------------------------------------------- SerialMux 汇聚口
def mux_frame(port, seq, payload):
    body = bytes([port, seq, len(payload) >> 8, len(payload) & 0xFF]) + payload
    x = 0
    for b in body:
        x ^= b
    return b"\xa5\x5a" + body + bytes([x])


def test_mux_frame_decoder():
    from h2wtool_pc.transport import MuxFrameDecoder

    stream = b"junk\xa5" + mux_frame(1, 0, b"hello") + mux_frame(31, 7, b"") + mux_frame(2, 1, b"\xa5\x5aX")
    bad = bytearray(mux_frame(4, 0, b"zz"))
    bad[-1] ^= 0xFF
    stream = bytes(bad) + stream
    d = MuxFrameDecoder()
    out = []
    for i in range(0, len(stream), 3):
        out += d.feed(stream[i:i + 3])
    assert out == [(1, b"hello"), (31, b""), (2, b"\xa5\x5aX")]
    assert d.bad_frames >= 1


def test_hub_serial_mux(hub):
    from h2wtool_pc.transport import SerialMuxReader

    class FakeSerial:
        def close(self):
            pass

    reader = SerialMuxReader(hub, "COM7", ser=FakeSerial())
    a = MEMTEST_UART
    b = MEMTEST_UART.replace(b'"FAIL"', b'"PASS"')
    # 两路交错、每路的行被拆在多帧里
    for i in range(0, max(len(a), len(b)), 40):
        reader.dispatch(mux_frame(0, i & 0xFF, a[i:i + 40]) + mux_frame(3, i & 0xFF, b[i:i + 40]))
    devs = sorted(d.peer for d in hub.devices())
    assert devs == ["COM7#ch0", "COM7#ch3"]
    rows = {r["device_id"]: r["result"] for r in hub.store.query()}
    assert rows == {"COM7#ch0": "FAIL", "COM7#ch3": "PASS"}
    assert all(d.transport == "usb-serial-mux" and d.passive for d in hub.devices())
    reader.close()
    assert not hub.devices()

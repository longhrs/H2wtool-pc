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

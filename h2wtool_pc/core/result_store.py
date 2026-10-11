"""测试结果持久化 (SQLite)。"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  REAL    NOT NULL,
    device_id    TEXT    NOT NULL,
    device_model TEXT    NOT NULL DEFAULT '',
    transport    TEXT    NOT NULL DEFAULT '',
    tool         TEXT    NOT NULL,
    test_id      TEXT    NOT NULL DEFAULT '',
    result       TEXT    NOT NULL,
    start_time   TEXT    NOT NULL DEFAULT '',
    end_time     TEXT    NOT NULL DEFAULT '',
    duration_s   REAL,
    summary      TEXT    NOT NULL DEFAULT '{}',
    config       TEXT    NOT NULL DEFAULT '{}',
    raw          TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_results_device ON results(device_id);
CREATE INDEX IF NOT EXISTS idx_results_tool ON results(tool);
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_dedup ON results(device_id, tool, test_id) WHERE test_id <> '';
"""

RESULT_VALUES = ("PASS", "FAIL", "ABORT", "ERROR")

CSV_COLUMNS = [
    "id", "received_at", "device_id", "device_model", "transport", "tool", "test_id",
    "result", "start_time", "end_time", "duration_s", "summary", "config",
]


def normalize_result(value: Any) -> str:
    v = str(value or "").strip().upper()
    aliases = {"PASSED": "PASS", "OK": "PASS", "SUCCESS": "PASS", "FAILED": "FAIL", "NG": "FAIL",
               "ABORTED": "ABORT", "STOPPED": "ABORT", "CANCELLED": "ABORT"}
    v = aliases.get(v, v)
    return v if v in RESULT_VALUES else "ERROR"


def _str_time(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):  # 毫秒时间戳
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v / 1000 if v > 1e11 else v))
    return str(v)


class ResultStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_from_message(self, msg: dict, device_id: str, device_model: str = "", transport: str = "") -> tuple[int, bool]:
        """保存一条 result 消息。返回 (行 id, 是否新插入)。同一设备同一 test_id 重复上报视为重传，不重复插入。"""
        summary = msg.get("summary") or {}
        config = msg.get("config") or {}
        duration = msg.get("duration_s")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        row = (
            time.time(), device_id, device_model, transport, str(msg.get("tool", "")),
            str(msg.get("test_id") or ""), normalize_result(msg.get("result")),
            _str_time(msg.get("start_time")), _str_time(msg.get("end_time")), duration,
            json.dumps(summary, ensure_ascii=False), json.dumps(config, ensure_ascii=False),
            json.dumps(msg, ensure_ascii=False),
        )
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO results (received_at, device_id, device_model, transport, tool, test_id,"
                " result, start_time, end_time, duration_s, summary, config, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._db.commit()
            if cur.rowcount:
                return cur.lastrowid, True
            existing = self._db.execute(
                "SELECT id FROM results WHERE device_id=? AND tool=? AND test_id=?",
                (device_id, row[4], row[5]),
            ).fetchone()
            return existing["id"], False

    def query(self, tool: str | None = None, device_id: str | None = None, result: str | None = None,
              text: str | None = None, limit: int = 5000) -> list[dict]:
        sql = "SELECT * FROM results WHERE 1=1"
        args: list = []
        if tool:
            sql += " AND tool=?"
            args.append(tool)
        if device_id:
            sql += " AND device_id=?"
            args.append(device_id)
        if result:
            sql += " AND result=?"
            args.append(result)
        if text:
            sql += " AND (device_id LIKE ? OR device_model LIKE ? OR test_id LIKE ? OR summary LIKE ?)"
            args += [f"%{text}%"] * 4
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, row_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM results WHERE id=?", (row_id,)).fetchone()
        return self._row_to_dict(r) if r else None

    def device_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT device_id FROM results ORDER BY device_id")]

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT result, COUNT(*) FROM results GROUP BY result").fetchall()
        return {r[0]: r[1] for r in rows}

    def delete(self, ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM results WHERE id=?", [(i,) for i in ids])
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM results")
            self._db.commit()

    @staticmethod
    def export_csv(path: Path | str, rows: list[dict]) -> None:
        # utf-8-sig 让 Excel 正确识别中文
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(CSV_COLUMNS)
            for r in rows:
                out = []
                for c in CSV_COLUMNS:
                    v = r.get(c)
                    if c == "received_at":
                        v = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                    elif isinstance(v, (dict, list)):
                        v = json.dumps(v, ensure_ascii=False)
                    out.append("" if v is None else v)
                w.writerow(out)

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("summary", "config", "raw"):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
        return d

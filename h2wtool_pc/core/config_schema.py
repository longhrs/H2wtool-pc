"""测试工具参数的 schema 定义、校验与预设管理。

schema 是一个 JSON 文件 (见 h2wtool_pc/schemas/*.json)，GUI 根据它动态生成设置界面。
用户可以把同名 JSON 放到 <数据目录>/schemas/ 下覆盖内置定义，或新增其他工具。

支持的字段类型: int, float, bool, string, enum (单选), multi (多选)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

FIELD_TYPES = {"int", "float", "bool", "string", "enum", "multi"}


class SchemaError(Exception):
    pass


@dataclass
class Field:
    key: str
    label: str
    type: str
    default: Any = None
    min: float | None = None
    max: float | None = None
    options: list[dict] = field(default_factory=list)  # [{"value":..., "label":...}]
    group: str = "参数"
    help: str = ""

    @property
    def option_values(self) -> list:
        return [o["value"] for o in self.options]

    @classmethod
    def from_dict(cls, d: dict) -> "Field":
        try:
            key, ftype = d["key"], d["type"]
        except KeyError as e:
            raise SchemaError(f"字段缺少 {e}") from None
        if ftype not in FIELD_TYPES:
            raise SchemaError(f"字段 {key} 类型不支持: {ftype}")
        options = []
        for o in d.get("options", []):
            options.append(o if isinstance(o, dict) else {"value": o, "label": str(o)})
        if ftype in ("enum", "multi") and not options:
            raise SchemaError(f"字段 {key} 缺少 options")
        f = cls(
            key=key,
            label=d.get("label", key),
            type=ftype,
            default=d.get("default"),
            min=d.get("min"),
            max=d.get("max"),
            options=options,
            group=d.get("group", "参数"),
            help=d.get("help", ""),
        )
        if f.default is None:
            f.default = f._implicit_default()
        return f

    def _implicit_default(self):
        return {
            "int": 0 if self.min is None else int(self.min),
            "float": 0.0 if self.min is None else float(self.min),
            "bool": False,
            "string": "",
            "enum": self.options[0]["value"] if self.options else None,
            "multi": [],
        }[self.type]

    def coerce(self, value: Any) -> Any:
        """把输入值转换为字段类型并做范围检查，失败抛 ValueError。"""
        t = self.type
        if t == "int":
            if isinstance(value, bool):
                raise ValueError("需要整数")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("需要整数")
            v = int(value)
        elif t == "float":
            if isinstance(value, bool):
                raise ValueError("需要数字")
            v = float(value)
        elif t == "bool":
            if isinstance(value, bool):
                v = value
            elif isinstance(value, (int, float)) and value in (0, 1):
                v = bool(value)
            elif isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no"):
                v = value.lower() in ("true", "1", "yes")
            else:
                raise ValueError("需要布尔值")
        elif t == "string":
            if not isinstance(value, (str, int, float)):
                raise ValueError("需要字符串")
            v = str(value)
        elif t == "enum":
            v = self._match_option(value)
        else:  # multi
            if not isinstance(value, (list, tuple)):
                raise ValueError("需要列表")
            v = []
            for item in value:
                m = self._match_option(item)
                if m not in v:
                    v.append(m)
            # 保持 schema 中的顺序，便于比较
            v.sort(key=self.option_values.index)
        if t in ("int", "float"):
            if self.min is not None and v < self.min:
                raise ValueError(f"不能小于 {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"不能大于 {self.max}")
        return v

    def _match_option(self, value):
        for ov in self.option_values:
            if ov == value or str(ov) == str(value):
                return ov
        raise ValueError(f"无效选项 {value!r}")


@dataclass
class ToolSchema:
    tool: str
    title: str
    fields: list[Field]
    description: str = ""
    order: int = 100
    source: str = ""

    @classmethod
    def from_dict(cls, d: dict, source: str = "") -> "ToolSchema":
        tool = d.get("tool")
        if not tool or not isinstance(tool, str):
            raise SchemaError("schema 缺少 tool")
        fields = [Field.from_dict(fd) for fd in d.get("fields", [])]
        keys = [f.key for f in fields]
        if len(set(keys)) != len(keys):
            raise SchemaError(f"{tool}: 字段 key 重复")
        return cls(tool=tool, title=d.get("title", tool), fields=fields,
                   description=d.get("description", ""), order=int(d.get("order", 100)), source=source)

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)

    def groups(self) -> list[tuple[str, list[Field]]]:
        out: dict[str, list[Field]] = {}
        for f in self.fields:
            out.setdefault(f.group, []).append(f)
        return list(out.items())

    def defaults(self) -> dict:
        return {f.key: (list(f.default) if isinstance(f.default, list) else f.default) for f in self.fields}

    def validate(self, config: dict, fill_defaults: bool = True) -> tuple[dict, dict[str, str]]:
        """返回 (清洗后的配置, {字段: 错误})。schema 中没有的字段原样保留 (前向兼容)。"""
        cleaned: dict = {}
        errors: dict[str, str] = {}
        for f in self.fields:
            if f.key not in config:
                if fill_defaults:
                    cleaned[f.key] = f.coerce(f.default)
                continue
            try:
                cleaned[f.key] = f.coerce(config[f.key])
            except (ValueError, TypeError) as e:
                errors[f.key] = f"{f.label}: {e}"
        for k, v in config.items():
            if k not in cleaned and k not in errors:
                cleaned[k] = v
        return cleaned, errors


def _builtin_schema_files() -> list[tuple[str, str]]:
    pkg = resources.files("h2wtool_pc") / "schemas"
    out = []
    for entry in sorted(pkg.iterdir(), key=lambda p: p.name):
        if entry.name.endswith(".json"):
            out.append((entry.read_text(encoding="utf-8"), f"builtin:{entry.name}"))
    return out


def load_schemas(user_dir: Path | None = None) -> dict[str, ToolSchema]:
    """加载内置 schema，再用 user_dir 下的同名 tool 覆盖。按 schema 的 order 字段排序。"""
    schemas: dict[str, ToolSchema] = {}
    sources = _builtin_schema_files()
    if user_dir and user_dir.is_dir():
        sources += [(p.read_text(encoding="utf-8"), str(p)) for p in sorted(user_dir.glob("*.json"))]
    for text, src in sources:
        try:
            s = ToolSchema.from_dict(json.loads(text), source=src)
        except (json.JSONDecodeError, SchemaError) as e:
            raise SchemaError(f"{src}: {e}") from None
        schemas[s.tool] = s
    return dict(sorted(schemas.items(), key=lambda kv: (kv[1].order, kv[0])))


_SAFE_NAME = re.compile(r"[^\w\-. 一-鿿]")


def safe_filename(name: str) -> str:
    name = _SAFE_NAME.sub("_", name).strip(" .")
    return name[:100] or "unnamed"


class PresetStore:
    """参数预设，存储为 <数据目录>/presets/<tool>/<名称>.json"""

    def __init__(self, root: Path):
        self.root = root

    def _dir(self, tool: str) -> Path:
        return self.root / safe_filename(tool)

    def list(self, tool: str) -> list[str]:
        d = self._dir(tool)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def load(self, tool: str, name: str) -> dict:
        path = self._dir(tool) / f"{safe_filename(name)}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, tool: str, name: str, config: dict) -> Path:
        d = self._dir(tool)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{safe_filename(name)}.json"
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def delete(self, tool: str, name: str) -> None:
        path = self._dir(tool) / f"{safe_filename(name)}.json"
        path.unlink(missing_ok=True)

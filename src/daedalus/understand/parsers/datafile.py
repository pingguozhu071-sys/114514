# -*- coding: utf-8 -*-
"""JSON / JSON-LD 与纯文本解析器

JSON：解析后**扁平化成 `路径: 值` 列表**（便于入库与检索），深度与条数有上限（防病态嵌套）。
文本：原样给出（编码探测交给上层；这里只保证"不炸"）。
"""

from __future__ import annotations

import json
import re

from daedalus.understand.registry import ParserSpec

__all__ = ["SPEC", "parse_json_doc", "parse_text", "flatten_json"]

VERSION = 1

MAX_DEPTH = 8
MAX_ITEMS = 20_000
MAX_VALUE_LEN = 2000

_WS_RE = re.compile(r"[ \t\r\f\v]+")


def flatten_json(obj, *, max_depth: int = MAX_DEPTH, max_items: int = MAX_ITEMS) -> list[tuple[str, str]]:
    """把 JSON 摊平成 `(路径, 字符串值)`（深度/条数上限，防病态结构）。"""
    out: list[tuple[str, str]] = []

    def walk(node, path: str, depth: int) -> None:
        if len(out) >= max_items or depth > max_depth:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k), depth + 1)
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", depth + 1)
        elif node is None:
            out.append((path, ""))
        else:
            out.append((path, str(node)[:MAX_VALUE_LEN]))

    walk(obj, "", 0)
    return out


def parse_json_doc(data: bytes, meta: dict) -> dict:
    text = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")
    try:
        obj = json.loads(text)
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析失败：{type(e).__name__}: {e}"}
    flat = flatten_json(obj)
    if not flat:
        return {"ok": False, "error": "JSON 是空的（无任何标量叶子）"}
    kind = "json_ld" if isinstance(obj, dict) and "@context" in obj else "json"
    # `text` 给"整段 JSON 的紧凑渲染"：质量闸与去重需要一个正文形态的可读内容
    rendered = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))[:MAX_VALUE_LEN * 10]
    return {"ok": True, "kind": kind, "flat": flat, "field_count": len(flat),
            "top_type": type(obj).__name__, "text": rendered,
            "title": str(obj.get("name") or obj.get("headline") or "") if isinstance(obj, dict) else ""}


def parse_text(data: bytes, meta: dict) -> dict:
    text = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")
    cleaned = _WS_RE.sub(" ", text).strip()
    if not cleaned:
        return {"ok": False, "error": "文本是空的"}
    return {"ok": True, "kind": "text", "text": cleaned[:2_000_000],
            "text_length": len(cleaned), "lines": cleaned.count("\n") + 1}


SPEC = ParserSpec(name="json_text", version=VERSION, accepts=("json", "text"),
                  parse=lambda data, meta: (parse_json_doc(data, meta)
                                            if str(meta.get("format")) == "json"
                                            else parse_text(data, meta)),
                  order=30, note="JSON 摊平成路径:值；文本原样")

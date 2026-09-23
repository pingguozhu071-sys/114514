# -*- coding: utf-8 -*-
"""内置解析器：HTML / 订阅与站点地图 / JSON 与文本

每个模块导出 `SPEC = ParserSpec(...)`，注册进 `understand/registry.py` 即可用。
**同构返回**：一律 `{"ok": bool, "error"?: str, ...}`。
"""

from __future__ import annotations

__all__ = ["datafile", "feed", "html_text"]

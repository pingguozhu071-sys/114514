# -*- coding: utf-8 -*-
"""解析器注册表：**插件化**的理解面入口

三条约定（"先捕获后理解"能成立的关键）：
  1) **同构返回**：所有解析器都返回 `{"ok": bool, "error"?: str, ...payload}`；
     失败给**可读原因**，不抛异常、不返回空（"诚实降级"）。
  2) **注册即可重扫历史**：新增一个解析器 → 注册 → `capture/replay.py` 就能把历史原始数据
     重新解释一遍（**不重新联网**）。这是本工程"事实不会丢"的兑现方式。
  3) **顺序可解释**：候选解析器按 `order` 升序尝试，失败自动落到下一个；都失败就返回
     `ok=False` 并把"试过哪些 + 各自为什么失败"一并给出。

解析器**必须标注版本**（`version`）：派生记录里会记下是哪个版本产出的，方便将来判断
"要不要用新版本重跑历史"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from daedalus.understand.detect import FormatGuess, detect

logger = logging.getLogger(__name__)

__all__ = ["ParserSpec", "ParserRegistry", "default_registry"]


@dataclass(frozen=True)
class ParserSpec:
    """一个解析器：能接哪些格式（`accepts`）、版本、以及 `parse(bytes, meta) -> dict`。"""

    name: str
    version: int
    accepts: tuple[str, ...]          # 格式名（来自 detect 的结论），可用 "*" 表示通配
    parse: Callable[[bytes, dict], dict]
    order: int = 100                  # 越小越先试
    note: str = ""

    def accepts_format(self, name: str) -> bool:
        return "*" in self.accepts or name in self.accepts


class ParserRegistry:
    """解析器注册表（线程安全地只读使用：注册一般在启动阶段一次性完成）。"""

    def __init__(self):
        self._parsers: list[ParserSpec] = []

    # ── 注册与查询 ───────────────────────────────────────────────
    def register(self, spec: ParserSpec) -> "ParserRegistry":
        self._parsers.append(spec)
        self._parsers.sort(key=lambda s: (s.order, s.name))
        logger.debug("注册解析器 %s v%d（接 %s）", spec.name, spec.version, ",".join(spec.accepts))
        return self

    def parsers(self) -> list[ParserSpec]:
        return list(self._parsers)

    def candidates(self, fmt: str) -> list[ParserSpec]:
        return [s for s in self._parsers if s.accepts_format(fmt)]

    def summary(self) -> list[dict]:
        return [{"name": s.name, "version": s.version, "accepts": list(s.accepts),
                 "order": s.order, "note": s.note} for s in self._parsers]

    # ── 解析 ─────────────────────────────────────────────────────
    def parse(self, data: bytes, meta: dict | None = None, url: str | None = None,
              fmt: FormatGuess | str | None = None) -> dict:
        """解析一段字节。返回**同构字典**：`{"ok", "error"?, "parser", "parser_version", ...}`。"""
        meta = dict(meta or {})
        guess = fmt if isinstance(fmt, FormatGuess) else detect(data, meta, url)
        base = {"format": guess.name, "format_how": guess.how, "format_confidence": guess.confidence}
        cands = self.candidates(guess.name)
        if not cands:
            return dict(base, ok=False,
                        error=f"没有解析器能处理格式 {guess.name!r}（原始数据已存，可延期解释）",
                        tried=[])
        tried: list[str] = []
        for spec in cands:
            tried.append(spec.name)
            try:
                out = spec.parse(bytes(data or b""), dict(meta, format=guess.name, url=url or ""))
            except Exception as e:                     # 解析器自身崩了也不能带塌上层
                logger.warning("解析器 %s 抛异常: %s", spec.name, e)
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if isinstance(out, dict) and out.get("ok"):
                return dict(base, ok=True, parser=spec.name, parser_version=spec.version,
                            tried=tried, **{k: v for k, v in out.items() if k != "ok"})
        return dict(base, ok=False, parser="", parser_version=0, tried=tried,
                    error=f"所有候选解析器都失败（试过 {tried}）："
                          f"{'; '.join(f'{p}' for p in tried)}")


def default_registry() -> ParserRegistry:
    """装上内置解析器（HTML / 订阅与站点地图 / JSON 与文本）。"""
    from daedalus.understand.parsers import datafile, feed, html_text
    reg = ParserRegistry()
    reg.register(html_text.SPEC)
    reg.register(feed.SPEC)
    reg.register(datafile.SPEC)
    return reg

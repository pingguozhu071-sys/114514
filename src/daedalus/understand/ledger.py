# -*- coding: utf-8 -*-
"""变更台账：每次运行都要能回答"这次到底抓全了没有？"

五态（**固定**，别随手加）：
    new            新增（内容哈希没见过）
    unchanged      未变（内容哈希与上次相同 → 一等结果，不是"没抓到"）
    updated        更新（同一 URL，内容哈希变了）
    failed         失败（抓取/解析失败，且不在重试中）
    policy_denied  被策略拒绝（robots/SSRF/合规）

台账同时落 JSONL（每次运行一份，追加），便于离线对账与画曲线。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

logger = logging.getLogger(__name__)

__all__ = ["ChangeLedger", "LEDGER_STATES"]

LEDGER_STATES = ("new", "unchanged", "updated", "failed", "policy_denied")


class ChangeLedger:
    """一次运行的变更台账（计数 + 最近若干事件）。"""

    def __init__(self, run_id: str | None = None, keep_events: int = 5000):
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.started_at = time.time()
        self._counts = {s: 0 for s in LEDGER_STATES}
        self._events: list[dict] = []
        self._keep = int(keep_events)

    # ── 记 ───────────────────────────────────────────────────────
    def record(self, state: str, *, url: str = "", detail: str = "", **extra) -> None:
        if state not in self._counts:
            raise KeyError(f"未知台账状态: {state!r}（合法值 {LEDGER_STATES}）")
        self._counts[state] += 1
        if len(self._events) < self._keep:
            ev = {"at": time.time(), "state": state, "url": str(url)[:500],
                  "detail": str(detail)[:500]}
            ev.update(extra)
            self._events.append(ev)

    def bump(self, state: str, n: int = 1, **kw) -> None:
        for _ in range(max(0, int(n))):
            self.record(state, **kw)

    # ── 读 ───────────────────────────────────────────────────────
    def summary(self) -> dict:
        total = sum(self._counts.values())
        return {"run_id": self.run_id, "started_at": self.started_at,
                "counts": dict(self._counts), "total": total,
                "changed": self._counts["new"] + self._counts["updated"],
                "events_kept": len(self._events)}

    def to_lines(self) -> list[str]:
        s = self.summary()
        c = s["counts"]
        return [
            f"运行 {s['run_id']}：共 {s['total']} 条",
            f"  新增 {c['new']} · 未变 {c['unchanged']} · 更新 {c['updated']} "
            f"· 失败 {c['failed']} · 被策略拒绝 {c['policy_denied']}",
            f"  内容有变化的：{s['changed']} 条",
        ]

    def events(self, state: str | None = None, limit: int = 100) -> list[dict]:
        evs = [e for e in self._events if state is None or e["state"] == state]
        return evs[-int(limit):]

    # ── 落盘 ─────────────────────────────────────────────────────
    def write_jsonl(self, path) -> str:
        """把事件追加写入 JSONL（文件不存在则建；**追加**，不覆盖历史运行）。"""
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(dict(self.summary(), kind="run_summary"), ensure_ascii=False) + "\n")
            for ev in self._events:
                fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
        logger.info("变更台账已写出：%s（%d 事件）", p, len(self._events))
        return str(p)

    def reset(self) -> None:
        self._counts = {s: 0 for s in LEDGER_STATES}
        self._events.clear()
        self.started_at = time.time()

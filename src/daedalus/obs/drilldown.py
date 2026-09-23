# -*- coding: utf-8 -*-
"""任务下钻（drill-down）：从"整体数字"走到"这一个任务到底经历了什么"

清单 L3 要的不只是"台账能计数"，而是**能查、能导出、能解释**。这里把四张表拼成一个
视图（全靠只读连接，不改任何状态）：

    tasks（任务本体与记账）
      └─ task_evidence（证据链：看到了什么 → 决定了什么 → 为什么）
      └─ raw_artifacts（原始层：内容寻址的字节事实 + 血缘）
      └─ pages（派生层：规范化后的记录）
      └─ deadletter（失败原文与最后一段 SQL trace，若有）

导出三种粒度：单任务 `task_view()` / 一批任务 `tasks_overview()` / 全量 `export_jsonl()`。
**导出走 JSONL**（一行一条），因为它是"可 diff、可管道、可回归对比"的形状——基准与
长跑脚本也吃这个格式（L7）。

三个刻意的取舍：
  * `facts_json` 在这里**解析回字典**（而不是原样字符串）：下钻是给人看/给脚本用的，
    字符串套 JSON 会让每个使用者都得再解一次。解析失败**不抛异常**，退回原文并标注 `__raw__`。
  * **SQL 一律字面量内联**（本工程的安全钩子硬要求：`execute()` 的第一个参数必须是字符串
    字面量）。所以这里的形状是"每个查询一个小方法 + 一个字面量 SQL"，而不是拼一个通用
    `_query(sql)`——参数永远是**数据**（`?` 绑定），永远不是 SQL 片段。
    代价是几行重复；换来的是**结构上不可能出现动态 SQL**。
  * 查询自己开关连接（绝不常驻句柄），每次只读。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

__all__ = ["Drilldown"]

_TASK_COLS = ("task_id", "kind", "target", "state", "attempts", "throttles", "transitions",
              "bytes_done", "seconds_done", "created_at", "updated_at", "parent_id",
              "discovery_path", "worker_id", "idempotency_key")


class Drilldown:
    """只读下钻视图（构造时给 `Database`；每次查询自己开关连接）。"""

    def __init__(self, db, *, ledger=None):
        self.db = db
        self.ledger = ledger                 # 可选：变更台账（`understand/ledger.py`）

    # ── 内部：借一条只读连接执行"字面量 SQL 的 lambda"────────────────
    def _one(self, fn):
        conn = self.db.connect(readonly=True)
        try:
            return fn(conn)
        finally:
            conn.close()

    # ── 各查询（每个方法的 SQL 都是字面量）──────────────────────────
    def _rows_task_by_id(self, tid: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (tid,)).fetchall()])

    def _rows_evidence(self, tid: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT id, at, stage, signal, decision, reason, facts_json FROM task_evidence "
            "WHERE task_id = ? ORDER BY id", (tid,)).fetchall()])

    def _rows_artifacts(self, tid: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT sha256, url, size, mime, status, source, session_id, discovery_path, "
            "parent_task, path, note, fetched_at FROM raw_artifacts "
            "WHERE parent_task = ? ORDER BY fetched_at", (tid,)).fetchall()])

    def _rows_pages(self, url_hash: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT url_hash, url, status, content_hash, simhash, size, fetched_at "
            "FROM pages WHERE url_hash = ?", (url_hash,)).fetchall()])

    def _rows_deadletter(self, tid: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT at, label, error, statements_json, replayed FROM deadletter "
            "WHERE label LIKE '%' || ? || '%' ORDER BY at DESC LIMIT 5",
            (tid,)).fetchall()])

    def _rows_children(self, tid: str) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT task_id, target, state, discovery_path FROM tasks WHERE parent_id = ?",
            (tid,)).fetchall()])

    def _rows_overview_all(self, lim: int, off: int) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT t.task_id, t.target, t.state, t.attempts, t.throttles, t.transitions, "
            "t.bytes_done, t.updated_at, "
            "(SELECT COUNT(*) FROM task_evidence e WHERE e.task_id = t.task_id) AS evidence_n "
            "FROM tasks t ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
            (lim, off)).fetchall()])

    def _rows_overview_state(self, state: str, lim: int, off: int) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT t.task_id, t.target, t.state, t.attempts, t.throttles, t.transitions, "
            "t.bytes_done, t.updated_at, "
            "(SELECT COUNT(*) FROM task_evidence e WHERE e.task_id = t.task_id) AS evidence_n "
            "FROM tasks t WHERE t.state = ? ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
            (state, lim, off)).fetchall()])

    def _rows_state_counts(self) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()])

    def _rows_artifacts_export(self, lim: int) -> list[dict]:
        return self._one(lambda c: [dict(r) for r in c.execute(
            "SELECT sha256, url, size, mime, status, source, parent_task, path, note, "
            "fetched_at FROM raw_artifacts ORDER BY fetched_at LIMIT ?", (lim,)).fetchall()])

    def _has_table(self, name: str) -> bool:
        try:
            return bool(self._one(lambda c: c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
                (str(name),)).fetchone()))
        except Exception:
            return False

    # ── 单任务 ────────────────────────────────────────────────
    def task_view(self, task_id: str) -> dict:
        """一个任务的完整视图（任务 + 证据链 + 原始层 + 派生层 + 子任务 + 死信）。"""
        tid = str(task_id)
        rows = self._rows_task_by_id(tid)
        if not rows:
            return {"task_id": tid, "found": False,
                    "note": "没有这个任务（可能还没入队，或已被清理）"}
        task = {k: rows[0].get(k) for k in _TASK_COLS if k in rows[0]}
        evidence = [self._decode_facts(r) for r in self._rows_evidence(tid)]
        artifacts = self._rows_artifacts(tid)
        pages = self._rows_pages(str(task.get("idempotency_key") or ""))
        dead = self._rows_deadletter(tid) if self._has_deadletter() else []
        return {"task_id": tid, "found": True, "task": task,
                "evidence": evidence, "artifacts": artifacts, "pages": pages,
                "children": self._rows_children(tid), "deadletter": dead,
                "summary": self._explain(task, evidence, artifacts, pages)}

    @staticmethod
    def _decode_facts(row: dict) -> dict:
        raw = row.get("facts_json")
        if not raw:
            row["facts"] = {}
            return row
        try:
            row["facts"] = json.loads(raw)
        except Exception:
            row["facts"] = {"__raw__": str(raw)[:500]}     # 脏数据不抛异常，如实标注
        return row

    @staticmethod
    def _explain(task: dict, evidence: list[dict], artifacts: list[dict],
                 pages: list[dict]) -> str:
        """一句人话把这条任务的一生讲清楚（给 GUI 详情页 / CLI 用）。"""
        parts = [f"{task.get('state')}（尝试 {task.get('attempts')} / 限流 {task.get('throttles')}"
                 f" / 转移 {task.get('transitions')}）",
                 f"{task.get('bytes_done')} 字节",
                 f"{len(evidence)} 条证据"]
        if artifacts:
            parts.append(f"原始 {len(artifacts)} 份")
        if pages:
            parts.append("有派生记录")
        decisive = [e for e in evidence if e.get("decision")]
        if decisive:
            last = decisive[-1]
            parts.append(f"最后决定：{last.get('decision')}"
                         f"（{str(last.get('reason') or '')[:60]}）")
        return "；".join(parts)

    # ── 一批 / 全量 ────────────────────────────────────────────
    def tasks_overview(self, *, state: str = "", limit: int = 200, offset: int = 0) -> list[dict]:
        """任务列表（带每条任务的证据条数）。`state` 为空 = 全部。"""
        lim = max(1, min(int(limit), 5000))
        off = max(0, int(offset))
        if state:
            return self._rows_overview_state(str(state), lim, off)
        return self._rows_overview_all(lim, off)

    def ledger_counts(self) -> dict:
        """变更台账五态计数（L3）。有台账对象就用它的；没有就回退到库内任务状态。"""
        if self.ledger is not None:
            try:
                return dict(self.ledger.summary().get("counts") or {})
            except Exception as e:
                logger.debug("台账读取失败，回退库统计：%s", e)
        counts = {"new": 0, "unchanged": 0, "updated": 0, "failed": 0, "policy_denied": 0}
        for r in self._rows_state_counts():
            st, n = str(r.get("state")), int(r.get("n") or 0)
            if st == "done":
                counts["new"] += n
            elif st == "policy_denied":
                counts["policy_denied"] += n
            elif st == "dead":
                counts["failed"] += n
        return counts

    def export_jsonl(self, *, limit: int = 10000) -> str:
        """全量导出（JSONL：一行一条，可 diff / 可 jq / 可回归对比）。"""
        lim = max(1, min(int(limit), 50000))
        out: list[str] = [json.dumps({"kind": "ledger", "counts": self.ledger_counts()},
                                     ensure_ascii=False, sort_keys=True)]
        for t in self.tasks_overview(limit=lim):
            out.append(json.dumps({"kind": "task", **t}, ensure_ascii=False, sort_keys=True,
                                  default=str))
        for a in self._rows_artifacts_export(lim):
            out.append(json.dumps({"kind": "artifact", **a}, ensure_ascii=False, sort_keys=True,
                                  default=str))
        return "\n".join(out)

    # ── 能力探测（表不存在时**如实说**，不抛异常）───────────────
    def _has_deadletter(self) -> bool:
        if not hasattr(self, "_dl_cache"):
            self._dl_cache = self._has_table("deadletter")
        return bool(self._dl_cache)

    def available(self) -> dict:
        """哪些下钻数据可用（缺表要说清缺什么）。"""
        return {"tasks": self._has_table("tasks"),
                "task_evidence": self._has_table("task_evidence"),
                "raw_artifacts": self._has_table("raw_artifacts"),
                "pages": self._has_table("pages"),
                "deadletter": self._has_deadletter(),
                "ledger": self.ledger is not None}

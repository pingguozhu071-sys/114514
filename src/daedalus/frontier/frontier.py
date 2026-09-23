# -*- coding: utf-8 -*-
"""前沿（Frontier）：持久队列 + **租约** + **CAS** + 心跳 + 看门狗 + 有界入队

这是"可持久状态"的核心，四条不变量（都是 Kiana 用事故换来的）：
  1) **CAS 必须在写产物之前**，且与产物写入在**同一个事务**里（`commit_done` 就是这么做的）：
     `UPDATE ... WHERE task_id=? AND state='leased' AND leased_at=?` 失守 → **一行都不写**。
  2) **心跳续的是 `lease_expires`，不是 `leased_at`**——改后者会破坏 CAS 闭环
     （"租约还是不是我的"判定就失效了）。
  3) **CAS 三态**：`True`=赢了 / `False`=租约已易主 / `None`=落库失败。
     落库失败**必须上抛重试**，绝不能当成"被别人抢走"（否则成功结果被静默丢弃）。
     （本实现里"落库失败"由 `SingleWriter` 抛异常表达，调用方按可重试处理。）
  4) **看门狗不杀线程**：超时只把任务打回 `retry` 或转 `dead`，由任务自己退出
     （Python 没有安全的线程取消）。

入队有界：`enqueue()` 在前沿积压达到 `max_queue` 时**拒绝**（返回原因），而不是无限增长。

⚠️ **SQL 一律内联字面量**：本机安全策略要求 `execute()` 的第一参数是字符串字面量
（模块级 SQL 常量传进去也会被判为"动态 SQL"）。所以本文件里没有具名 SQL 常量——
这是刻意的，别"顺手重构"成常量。
"""

from __future__ import annotations

import json
import logging
import time

from daedalus.core.evidence import Evidence, from_decision
from daedalus.core.task import Task, TaskState
from daedalus.frontier.dedup import clamp63

logger = logging.getLogger(__name__)

__all__ = ["Frontier", "LeaseLost", "FrontierFull"]


class LeaseLost(Exception):
    """租约已易主（CAS 失守）——**不可重试**：别人已接手，本次结果作废。"""


class FrontierFull(Exception):
    """前沿已满（有界队列的背压信号）。"""


class Frontier:
    """任务前沿。所有写操作都经 `SingleWriter`（单写线程 + 批提交）。"""

    def __init__(self, writer, max_queue: int = 200_000, lease_timeout: float = 300.0,
                 heartbeat_interval: float = 60.0):
        self.writer = writer
        self.max_queue = int(max_queue)
        self.lease_timeout = float(lease_timeout)
        self.heartbeat_interval = float(heartbeat_interval)

    # ── 入队（有界 + 幂等）─────────────────────────────────────────
    def enqueue(self, task: Task, *, check_capacity: bool = True) -> tuple[bool, str]:
        """入队。返回 `(是否新入队, 原因)`。

        * **有界**：前沿积压 ≥ `max_queue` → 拒绝（背压），不是无限增长；
        * **幂等**：`idempotency_key` 命中已有任务 → 不入队（返回 False + 原因）。
        """

        def job(conn):
            if check_capacity:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('pending','retry')"
                ).fetchone()
                n = int(row["n"] if row else 0)
                if n >= self.max_queue:
                    return False, f"前沿已满（{n}/{self.max_queue}）——背压：请稍后再入队"
            row = task.to_row()
            cur = conn.execute(
                "INSERT OR IGNORE INTO tasks (task_id, kind, target, goal, scope, "
                "idempotency_key, parent_id, discovery_path, policy_json, resources_json, "
                "budget_json, state, attempts, throttles, transitions, bytes_done, seconds_done, "
                "leased_at, lease_expires, worker_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["task_id"], row["kind"], row["target"], row["goal"], row["scope"],
                 row["idempotency_key"], row["parent_id"], row["discovery_path"],
                 row["policy_json"], row["resources_json"], row["budget_json"], row["state"],
                 row["attempts"], row["throttles"], row["transitions"], row["bytes_done"],
                 row["seconds_done"], row["leased_at"], row["lease_expires"], row["worker_id"],
                 row["created_at"], row["updated_at"]))
            if cur.rowcount == 0:
                return False, "重复任务（幂等键命中已有任务）"
            self._write_evidence(conn, task.task_id, task.evidence)
            return True, "ok"

        return self.writer.run_now(job, label="frontier.enqueue")

    def requeue(self, task_ids, *, max_attempts: int = 5) -> list[tuple[str, bool, str]]:
        """把**已有任务**重新排回队列（界面上的「重试」走这里）。返回 `[(id, 成功?, 原因)]`。

        为什么不能复用 `enqueue`：那条路是 `INSERT OR IGNORE`，幂等键命中就拒绝——
        那是"防重复入队"的正确行为，而重试恰恰要**同一条任务再来一次**。

        三条硬规矩：
          * **重试有上限**（`max_attempts`，默认 5）：反复重试不能无限，到顶如实拒绝；
          * **正在跑的不能重排**（`leased`/`running`）：否则同一条任务会有两个执行者；
          * **原始层不动**：只是把这条任务的 state 改回 `pending`，已经捕获的字节一个都不碰。
        """
        ids = [str(t) for t in (task_ids or []) if str(t)]

        def job(conn):
            out: list[tuple[str, bool, str]] = []
            now = time.time()
            for tid in ids:
                row = conn.execute(
                    "SELECT state, attempts FROM tasks WHERE task_id = ?", (tid,)).fetchone()
                if row is None:
                    out.append((tid, False, "没有这个任务"))
                    continue
                st, att = str(row["state"]), int(row["attempts"])
                if st in ("leased", "running"):
                    out.append((tid, False, "任务正在跑，不能重复入队"))
                    continue
                if att >= int(max_attempts):
                    out.append((tid, False, f"重试次数已达上限（{att}/{int(max_attempts)}）"))
                    continue
                conn.execute(
                    "UPDATE tasks SET state = 'pending', attempts = attempts + 1, "
                    "leased_at = NULL, lease_expires = NULL, worker_id = '', updated_at = ? "
                    "WHERE task_id = ?", (now, tid))
                out.append((tid, True, f"已重新入队（第 {att + 1} 次尝试）"))
            return out

        return self.writer.run_now(job, label="frontier.requeue")

    # ── 领取（租约）──────────────────────────────────────────────
    def claim_batch(self, n: int, worker_id: str) -> list[Task]:
        """回收过期租约 → 领 `n` 条 → 打租约。返回的任务**带着本次真实的 `leased_at`**。

        （Kiana 的坑：曾把 UPDATE 之前的旧值返回，导致 CAS 分支永不生效。）
        """
        now = time.time()
        expires = now + self.lease_timeout

        def job(conn):
            # ① 回收过期租约：还能重试就回 retry，超了就 dead（**不杀线程**）
            for r in conn.execute(
                    "SELECT task_id, attempts, budget_json FROM tasks "
                    "WHERE state IN ('leased','running') AND COALESCE(lease_expires, 0) < ?",
                    (now,)).fetchall():
                tid = r["task_id"]
                attempts = int(r["attempts"] or 0)
                try:
                    max_attempts = int(json.loads(r["budget_json"] or "{}").get("max_attempts", 3))
                except Exception:
                    max_attempts = 3
                if attempts + 1 >= max_attempts:
                    conn.execute(
                        "UPDATE tasks SET state='dead', worker_id='', leased_at=NULL, "
                        "lease_expires=NULL, updated_at=? "
                        "WHERE task_id=? AND state IN ('leased','running')", (now, tid))
                else:
                    conn.execute(
                        "UPDATE tasks SET state='retry', attempts=attempts+1, worker_id='', "
                        "leased_at=NULL, lease_expires=NULL, updated_at=? "
                        "WHERE task_id=? AND state IN ('leased','running')", (now, tid))
            # ② 领取：只认 pending/retry
            rows = conn.execute(
                "SELECT * FROM tasks WHERE state IN ('pending','retry') "
                "ORDER BY updated_at LIMIT ?", (int(n),)).fetchall()
            claimed: list[Task] = []
            for r in rows:
                cur = conn.execute(
                    "UPDATE tasks SET state='leased', leased_at=?, lease_expires=?, "
                    "worker_id=?, updated_at=? "
                    "WHERE task_id=? AND state IN ('pending','retry')",
                    (now, expires, worker_id, now, r["task_id"]))
                if cur.rowcount == 0:
                    continue                       # 被别人抢走了（竞态正常）
                t = Task.from_row(r)
                t.state, t.leased_at, t.lease_expires = TaskState.LEASED, now, expires
                t.worker_id = worker_id
                claimed.append(t)
            return claimed

        return self.writer.run_now(job, label="frontier.claim")

    # ── 心跳（续**到期时间**，不动 `leased_at`）────────────────────
    def heartbeat(self, task: Task) -> bool:
        now = time.time()
        new_expires = now + self.lease_timeout

        def job(conn):
            cur = conn.execute(
                "UPDATE tasks SET lease_expires=?, updated_at=? "
                "WHERE task_id=? AND state IN ('leased','running') "
                "AND worker_id=? AND leased_at=?",
                (new_expires, now, task.task_id, task.worker_id, task.leased_at))
            return cur.rowcount > 0

        ok = bool(self.writer.run_now(job, label="frontier.heartbeat"))
        if ok:
            task.lease_expires = new_expires
        return ok

    # ── 提交（**CAS 在写产物之前**，且同一事务）──────────────────
    def commit_done(self, task: Task, *, artifact: dict | None = None,
                    page: dict | None = None, evidence: list[Evidence] | None = None,
                    bytes_done: int | None = None, seconds_done: float | None = None) -> bool:
        """打卡成功并写产物。返回 True=成功；False=**租约已易主（一行都没写）**。

        顺序刻意如此：先 CAS 打卡，再写产物——两者在同一个事务里，所以"失守则不写"是原子的。
        """
        now = time.time()
        b = int(bytes_done if bytes_done is not None else task.bytes_done)
        s = float(seconds_done if seconds_done is not None else task.seconds_done)

        def job(conn):
            cur = conn.execute(
                "UPDATE tasks SET state='done', bytes_done=?, seconds_done=?, updated_at=? "
                "WHERE task_id=? AND state IN ('leased','running') "
                "AND worker_id=? AND leased_at=?",
                (b, s, now, task.task_id, task.worker_id, task.leased_at))
            if cur.rowcount == 0:
                return False                      # 租约易主 → 一行都不写
            if artifact:
                a = dict(artifact)
                if a.get("simhash") is not None:
                    a["simhash"] = clamp63(a["simhash"])          # **入库前必须钳位**
                conn.execute(
                    "INSERT OR IGNORE INTO raw_artifacts (sha256, url, size, mime, status, "
                    "headers_json, fetched_at, source, session_id, parent_task, discovery_path, "
                    "path, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (a.get("sha256", ""), a.get("url", task.target), int(a.get("size", 0)),
                     a.get("mime", ""), int(a.get("status", 0)), a.get("headers_json", "{}"),
                     float(a.get("fetched_at", now)), a.get("source", ""),
                     a.get("session_id", ""), a.get("parent_task", task.task_id),
                     a.get("discovery_path", task.discovery_path), a.get("path", ""),
                     a.get("note", "")))
            if page:
                p = dict(page)
                if p.get("simhash") is not None:
                    p["simhash"] = clamp63(p["simhash"])
                conn.execute(
                    "INSERT OR REPLACE INTO pages (url_hash, url, fetched_at, status, "
                    "content_hash, simhash, duplicate_of, size, source_sha256) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (p.get("url_hash", ""), p.get("url", task.target),
                     float(p.get("fetched_at", now)), int(p.get("status", 0)),
                     p.get("content_hash", ""), p.get("simhash"), p.get("duplicate_of"),
                     int(p.get("size", 0)), p.get("source_sha256", "")))
                # 全文索引与派生行**同一事务**（单写线程的连接）：不会出现"页在、索引不在"。
                # 正文由调用方（运行器）从派生记录里带进来——`pages` 表本身不存正文。
                if p.get("title") or p.get("text"):
                    from daedalus.store.search import index_page
                    index_page(conn, url_hash=p.get("url_hash", ""), url=p.get("url", ""),
                               title=p.get("title", ""), text=p.get("text", ""))
            self._write_evidence(conn, task.task_id, self._evidence_list(evidence) or task.evidence)
            return True

        return bool(self.writer.run_now(job, label="frontier.commit"))

    # ── 交还（不记失败：任务回到可领取状态）────────────────────────
    def release(self, task: Task, reason: str, *,
                evidence: Evidence | list[Evidence] | None = None) -> str:
        """把任务**交还队列**（`leased/running → retry`），**不计 attempts、不计 throttles**。

        什么时候用：进程要优雅退出（还剩没跑完的活）、或调用方主动放弃这次尝试而任务本身
        没有问题。这不是失败——所以既不递增重试计数，也不进死信。
        返回新状态；CAS 失守返回 `'lease_lost'`。
        """
        now = time.time()
        evs = self._evidence_list(evidence) or [
            from_decision("ok", "release", f"交还队列（不计失败）：{reason}", stage="frontier")]

        def job(conn):
            cur = conn.execute(
                "UPDATE tasks SET state='retry', worker_id='', leased_at=NULL, "
                "lease_expires=NULL, updated_at=? "
                "WHERE task_id=? AND state IN ('leased','running') "
                "AND worker_id=? AND leased_at=?",
                (now, task.task_id, task.worker_id, task.leased_at))
            if cur.rowcount == 0:
                return "lease_lost"
            self._write_evidence(conn, task.task_id, evs)
            return TaskState.RETRY

        return str(self.writer.run_now(job, label="frontier.release"))

    # ── 失败（同样走 CAS；限流与重试**分别记账**）──────────────────
    def mark_failed(self, task: Task, reason: str, *, state: str | None = None,
                    retryable: bool = True, throttled: bool = False,
                    evidence: Evidence | list[Evidence] | None = None) -> str:
        """记录失败。返回新状态（`'retry'` / `'dead'` / `'policy_denied'` / `'lease_lost'`）。

        * `state=...`：**路由已经裁决的终态**，照用不重新推导
          （`policy_denied` 曾经被 `retryable=False` 吞成 `dead`，S6 门禁 D 用例抓到的）；
        * `throttled=True` → **只加 `throttles`**，不动 `attempts`（限流≠重试）；
        * 策略拒绝同样**不计重试**（它不是重试，是被规矩挡住）；
        * 其余情况：重试到上限 → `dead`。
        """
        throttled = bool(throttled)
        attempts = task.attempts + (0 if (throttled or state == TaskState.POLICY_DENIED) else 1)
        throttles = task.throttles + (1 if throttled else 0)
        if state is not None:
            if state not in TaskState.TERMINAL:
                raise ValueError(f"mark_failed 只接终态，收到 {state!r}（合法：{TaskState.TERMINAL}）")
        elif not retryable:
            state = TaskState.DEAD
        elif attempts >= task.budget.max_attempts or throttles >= task.budget.max_throttles:
            state = TaskState.DEAD
        else:
            state = TaskState.RETRY
        now = time.time()
        evs = self._evidence_list(evidence) or [
            from_decision("throttled" if throttled else "transient_failure",
                          state, reason, stage="frontier")]

        def job(conn):
            cur = conn.execute(
                "UPDATE tasks SET state=?, attempts=?, throttles=?, transitions=?, "
                "worker_id='', leased_at=NULL, lease_expires=NULL, updated_at=? "
                "WHERE task_id=? AND state IN ('leased','running') "
                "AND worker_id=? AND leased_at=?",
                (state, attempts, throttles, task.transitions, now, task.task_id,
                 task.worker_id, task.leased_at))
            if cur.rowcount == 0:
                return "lease_lost"
            self._write_evidence(conn, task.task_id, evs)
            return state

        return str(self.writer.run_now(job, label="frontier.fail"))

    # ── 看门狗与观测 ─────────────────────────────────────────────
    def watchdog_scan(self, now: float | None = None) -> dict:
        """扫"租约已过期"的任务数（真正的回收发生在下次 `claim_batch`）。"""
        now = time.time() if now is None else float(now)

        def job(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE state IN ('leased','running') "
                "AND COALESCE(lease_expires, 0) < ?", (now,)).fetchone()
            return {"expired_leases": int(row["n"] if row else 0),
                    "lease_timeout": self.lease_timeout,
                    "heartbeat_interval": self.heartbeat_interval}

        return self.writer.run_now(job, label="frontier.watchdog")

    def stats(self) -> dict:
        def job(conn):
            counts = {r["state"]: int(r["n"]) for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()}
            return {"counts": counts,
                    "claimable": counts.get(TaskState.PENDING, 0) + counts.get(TaskState.RETRY, 0),
                    "in_flight": counts.get(TaskState.LEASED, 0) + counts.get(TaskState.RUNNING, 0),
                    "max_queue": self.max_queue}

        return self.writer.run_now(job, label="frontier.stats")

    # ── 内部 ─────────────────────────────────────────────────────
    @staticmethod
    def _evidence_list(evidence) -> list[Evidence]:
        """统一证据入参：单条 / 一串 / 空 都接（调用方不必记住是哪种）。

        这个刺扎过：`commit_done` 收一串、`mark_failed` 收单条，运行器把**证据链**传进来时
        变成 `[[…]]`，`_write_evidence` 里 `ev.at` 直接 AttributeError（S6 门禁五个用例同红）。
        接口形状不一致，就是等着出这种事。
        """
        if evidence is None:
            return []
        if isinstance(evidence, (list, tuple)):
            return [e for e in evidence if e is not None]
        return [evidence]

    @staticmethod
    def _write_evidence(conn, task_id: str, evidence: list[Evidence] | None) -> None:
        for ev in (evidence or []):
            conn.execute(
                "INSERT INTO task_evidence (task_id, at, stage, signal, decision, reason, "
                "facts_json) VALUES (?,?,?,?,?,?,?)",
                (task_id, ev.at, ev.stage, ev.signal, ev.decision, ev.reason,
                 json.dumps(ev.facts, ensure_ascii=False)))

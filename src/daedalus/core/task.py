# -*- coding: utf-8 -*-
"""统一任务模型：**Task 是核心对象，不是 URL**

为什么不是 URL：一个任务可以跨多个采集环境（直连网络 → 制品与媒体 → 浏览器），
中途会换执行路径、会发现新事实、会产生新任务。用 URL 当核心对象，就表达不了这些。

本模块只定义**数据结构与不变量**（不碰数据库、不碰网络）：
  * `TaskKind`  —— 任务的种类（采集 / 观察 / 派生 / 重放）
  * `TaskState` —— **有界**的状态集合（不许扩展成"什么都有"）
  * `ResourceRequest` —— 任务**显式声明**它要什么资源（缺省即拒绝，见 `core/registry.py`）
  * `Task` —— 任务本体（含幂等键、父任务、发现路径、证据链、预算与已花费）

序列化：`to_row()/from_row()` 给 SQLite 用（列名与 `frontier/migrations/0001_init.sql` 对齐）。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from daedalus.core.budget import Budget
from daedalus.core.evidence import Evidence

__all__ = ["TaskKind", "TaskState", "ResourceRequest", "Task", "make_idempotency_key"]


class TaskKind:
    """任务种类（**有界**；新增种类要同时补路由表，见 `core/router.py`）。"""

    ACQUIRE = "acquire"      # 去拿（默认：从最便宜的路开始）
    OBSERVE = "observe"      # 观察（例如浏览器环境的网络观察；S8）
    DERIVE = "derive"        # 从原始层派生（解析/重放；不联网）
    REPLAY = "replay"        # 重放历史原始数据（`reparse`）
    ALL = (ACQUIRE, OBSERVE, DERIVE, REPLAY)


class TaskState:
    """任务状态（**有界**）。

    流转：`pending → leased → running → done`
          `running → retry`（可重试的失败，回 pending 重排队）
          `running → dead`（重试耗尽 / 证据不足 / 预算耗尽）
          `任何 → policy_denied`（robots/合规/SSRF 从源头拒绝；**不可重试**）
    """

    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    RETRY = "retry"
    DEAD = "dead"
    POLICY_DENIED = "policy_denied"

    ALL = (PENDING, LEASED, RUNNING, DONE, RETRY, DEAD, POLICY_DENIED)
    # "还能被领取"的状态
    CLAIMABLE = (PENDING, RETRY)
    TERMINAL = (DONE, DEAD, POLICY_DENIED)


@dataclass(frozen=True)
class ResourceRequest:
    """任务**显式声明**的资源需求。**缺省即拒绝**：没声明的东西不会"偷偷跑起来"。

    `browser` 默认 0 且注册表里浏览器容量默认 0 —— 也就是说：没启用浏览器环境时，
    任何要浏览器的任务都会被拒（这正是"缺省即拒绝"的落地）。
    """

    network: int = 0             # 同时在用的网络请求数（0 = 不联网，例如纯派生任务）
    browser: int = 0             # 浏览器上下文/页槽位（最稀缺；默认 0）
    process: int = 0             # 需要进程隔离的槽位（文档解析/OCR 等）
    subprocess: int = 0          # 外部二进制槽位（ffmpeg 等）
    cpu: str = "low"             # low | medium | high（只用于排队优先级与仲裁）
    memory_mb: int = 128
    disk: str = "low"            # low | high（大对象下载声明 high）
    domain_concurrency: int = 1  # 该域的并发份额（礼貌预算用）

    def to_json(self) -> str:
        return json.dumps({"network": self.network, "browser": self.browser,
                           "process": self.process, "subprocess": self.subprocess,
                           "cpu": self.cpu, "memory_mb": self.memory_mb,
                           "disk": self.disk, "domain_concurrency": self.domain_concurrency},
                          ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> "ResourceRequest":
        try:
            d = json.loads(text or "{}")
        except Exception:
            d = {}
        fields = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in fields})


def make_idempotency_key(kind: str, target: str) -> str:
    """幂等键：同 kind + 同 target 视为**同一个任务**（重复入队 = 无操作）。

    target 应当是**规范化过的**（URL 规范化见 S4；带签名参数的直链不要当 target，
    那是"运行态钥匙"，每次都会变，会让幂等失效）。
    """
    raw = f"{kind}|{target}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass
class Task:
    """一个采集任务。字段分四组：身份 / 目标与策略 / 预算 / 运行态。"""

    # ── 身份 ──
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    kind: str = TaskKind.ACQUIRE
    idempotency_key: str = ""
    parent_id: str | None = None
    discovery_path: str = ""            # 这个任务是怎么被发现的（审计与去重溯源用）

    # ── 目标与策略 ──
    target: str = ""
    goal: str = ""                      # 人话描述（"抓这个页面里可访问的全部产品数据"）
    scope: str = ""                     # 范围（域/深度/前缀……由上层解释）
    policy: dict = field(default_factory=dict)   # 合规与礼貌策略快照（robots 结果、速率档）

    # ── 声明与预算 ──
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    budget: Budget = field(default_factory=Budget)

    # ── 运行态 ──
    state: str = TaskState.PENDING
    attempts: int = 0                   # **重试**计数（限流不计在这里）
    throttles: int = 0                  # 被限流的**独立**计数（连续 N 次 → dead）
    transitions: int = 0                # 路由状态转移次数（有界，防"无限升级"）
    bytes_done: int = 0
    seconds_done: float = 0.0
    leased_at: float | None = None      # 租约开始（**心跳不改它**）
    lease_expires: float | None = None  # 租约到期（心跳**续这个**）
    worker_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    evidence: list[Evidence] = field(default_factory=list)

    def __post_init__(self):
        if not self.idempotency_key:
            self.idempotency_key = make_idempotency_key(self.kind, self.target)
        if self.kind not in TaskKind.ALL:
            raise ValueError(f"未知任务种类: {self.kind!r}（合法值 {TaskKind.ALL}）")
        if self.state not in TaskState.ALL:
            raise ValueError(f"未知状态: {self.state!r}（合法值 {TaskState.ALL}）")

    # ── 便捷构造 ──────────────────────────────────────────────
    @classmethod
    def acquire(cls, target: str, *, goal: str = "", scope: str = "",
                resources: ResourceRequest | None = None, budget: Budget | None = None,
                parent_id: str | None = None, discovery_path: str = "",
                policy: dict | None = None) -> "Task":
        return cls(kind=TaskKind.ACQUIRE, target=target, goal=goal, scope=scope,
                   resources=resources or ResourceRequest(network=1),
                   budget=budget or Budget(), parent_id=parent_id,
                   discovery_path=discovery_path, policy=dict(policy or {}))

    @classmethod
    def derive(cls, target: str, **kw) -> "Task":
        """派生任务（从原始层算，不联网）：默认**不声明网络资源**——这样它物理上没法出网。"""
        kw.setdefault("resources", ResourceRequest(network=0, cpu="medium"))
        t = cls(kind=TaskKind.DERIVE, target=target, **kw)
        return t

    # ── 状态流转（全部返回新对象，避免"就地改"带来的竞态）──────────
    def with_state(self, state: str, *, worker_id: str | None = None,
                   evidence: Evidence | None = None) -> "Task":
        t = replace(self, state=state, updated_at=time.time())
        if worker_id is not None:
            t.worker_id = worker_id
        if evidence is not None:
            t.evidence = list(self.evidence) + [evidence]
        return t

    def add_evidence(self, ev: Evidence) -> "Task":
        t = replace(self)
        t.evidence = list(self.evidence) + [ev]
        t.updated_at = time.time()
        return t

    # ── 预算与状态的组合判断 ──────────────────────────────────
    def exhausted(self) -> str | None:
        """返回"耗尽的维度"（None = 还有余量）。**限流不计入 attempts**。"""
        if self.attempts >= self.budget.max_attempts:
            return "attempts"
        if self.transitions >= self.budget.max_transitions:
            return "transitions"
        if self.seconds_done >= self.budget.max_seconds:
            return "seconds"
        if self.bytes_done >= self.budget.max_bytes:
            return "bytes"
        if self.throttles >= self.budget.max_throttles:
            return "throttles"
        return None

    # ── 序列化（列名与 0001_init.sql 对齐）────────────────────
    def to_row(self) -> dict:
        return {
            "task_id": self.task_id, "kind": self.kind, "target": self.target,
            "goal": self.goal, "scope": self.scope,
            "idempotency_key": self.idempotency_key, "parent_id": self.parent_id,
            "discovery_path": self.discovery_path,
            "policy_json": json.dumps(self.policy, ensure_ascii=False),
            "resources_json": self.resources.to_json(),
            "budget_json": json.dumps(self.budget.to_dict(), ensure_ascii=False),
            "state": self.state, "attempts": self.attempts, "throttles": self.throttles,
            "transitions": self.transitions, "bytes_done": self.bytes_done,
            "seconds_done": self.seconds_done, "leased_at": self.leased_at,
            "lease_expires": self.lease_expires, "worker_id": self.worker_id,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Task":
        g = (lambda k, d=None: row[k] if k in row.keys() else d)
        return cls(
            task_id=g("task_id"), kind=g("kind", TaskKind.ACQUIRE), target=g("target", ""),
            goal=g("goal", ""), scope=g("scope", ""),
            idempotency_key=g("idempotency_key", ""), parent_id=g("parent_id"),
            discovery_path=g("discovery_path", ""),
            policy=json.loads(g("policy_json", "{}") or "{}"),
            resources=ResourceRequest.from_json(g("resources_json", "{}")),
            budget=Budget.from_dict(json.loads(g("budget_json", "{}") or "{}")),
            state=g("state", TaskState.PENDING), attempts=int(g("attempts", 0) or 0),
            throttles=int(g("throttles", 0) or 0), transitions=int(g("transitions", 0) or 0),
            bytes_done=int(g("bytes_done", 0) or 0),
            seconds_done=float(g("seconds_done", 0.0) or 0.0),
            leased_at=g("leased_at"), lease_expires=g("lease_expires"),
            worker_id=g("worker_id", "") or "",
            created_at=float(g("created_at", 0) or 0), updated_at=float(g("updated_at", 0) or 0),
        )

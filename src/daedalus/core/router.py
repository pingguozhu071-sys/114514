# -*- coding: utf-8 -*-
"""证据驱动路由：**有界**状态机（不是 AI，也不是"聪明"的东西）

它只做四件事（《写作.txt》§十五）：
    1) 识别（What do I know?）——看证据；
    2) 判断（What execution modes are valid?）——看当前启用了哪些环境；
    3) 选择（Which valid mode is cheapest?）——**便宜先行**；
    4) 升级（Did execution provide evidence that another mode is necessary?）——**只在有证据时**。

三条硬约束（本文件里都能看到）：
  * **转移有界**：次数超过预算 → 直接 dead（防"坏目标变资源黑洞"）；
  * **每次转移都给理由**：返回的 `Decision` 必须带 reason，并被记进任务证据链；
  * **未启用的环境只记"候选"**：例如 V0.1 不含浏览器环境，遇到"内容空壳"就记一条
    browser 候选证据并终止，而**不是**偷偷起浏览器。

环境名（`Environment`）：`network`（直连网络）/ `artifact`（制品与媒体）/ `browser`（浏览器运行时）。
阶段（`Stage`）就是 Progressive Acquisition 的 L0–L5：分类 → 直连 → 内容检查 → 浏览器 → 网络观察 → 专用制品。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from daedalus.core.evidence import Evidence, from_decision
from daedalus.core.task import Task, TaskState

logger = logging.getLogger(__name__)

__all__ = ["Environment", "Stage", "Decision", "Router"]


class Environment:
    NETWORK = "network"
    ARTIFACT = "artifact"
    BROWSER = "browser"

    ALL = (NETWORK, ARTIFACT, BROWSER)


class Stage:
    """Progressive Acquisition 的层级（**便宜先行**）。"""

    CLASSIFY = "classify"     # L0：只分类，不出网
    DIRECT = "direct"         # L1：直连网络
    INSPECT = "inspect"       # L2：内容检查（质量/格式）
    BROWSER = "browser"       # L3：浏览器环境
    OBSERVE = "observe"       # L4：网络观察
    ARTIFACT = "artifact"     # L5：专用制品获取

    ALL = (CLASSIFY, DIRECT, INSPECT, BROWSER, OBSERVE, ARTIFACT)
    RANK = {CLASSIFY: 0, DIRECT: 1, INSPECT: 2, BROWSER: 3, OBSERVE: 4, ARTIFACT: 5}


# 每个阶段需要哪个环境（未启用 → 只能记"候选"）
STAGE_ENV = {
    Stage.CLASSIFY: None,
    Stage.DIRECT: Environment.NETWORK,
    Stage.INSPECT: None,
    Stage.BROWSER: Environment.BROWSER,
    Stage.OBSERVE: Environment.BROWSER,
    Stage.ARTIFACT: Environment.ARTIFACT,
}


@dataclass(frozen=True)
class Decision:
    """一次路由决定：去哪（`next_stage`）或收在哪（`terminal`），以及**为什么**。"""

    next_stage: str | None = None
    terminal: str | None = None            # TaskState 里的终态
    environment: str | None = None
    reason: str = ""
    evidence: Evidence | None = None

    @property
    def is_terminal(self) -> bool:
        return self.terminal is not None

    def describe(self) -> str:
        if self.is_terminal:
            return f"→ 终态 {self.terminal}（{self.reason}）"
        return f"→ {self.next_stage}[{self.environment or '-'}]（{self.reason}）"


class Router:
    """确定性策略引擎 + 有界状态机（**可解释**：每个决定都有 reason）。"""

    def __init__(self, enabled_environments=(Environment.NETWORK, Environment.ARTIFACT),
                 max_transitions: int = 8):
        self.enabled = tuple(enabled_environments)
        self.max_transitions = int(max_transitions)

    # ── 对外主入口 ────────────────────────────────────────────────
    def start(self, task: Task) -> Decision:
        """任务开始：L0 分类 → L1 直连（最便宜的路）。"""
        if task.state in TaskState.TERMINAL:
            return Decision(terminal=task.state, reason=f"任务已处于终态 {task.state}")
        return Decision(next_stage=Stage.DIRECT, environment=Environment.NETWORK,
                        reason="初始路径：直连网络（最便宜）")

    def on_evidence(self, task: Task, ev: Evidence) -> Decision:
        """按当前阶段 + 证据决定下一步。**所有分支都必须有 reason。**"""
        exhausted = task.exhausted()
        if exhausted:
            return self._terminal(task, TaskState.DEAD,
                                  f"预算耗尽（{exhausted}）", "budget_exceeded")
        stage = self._stage_of(task)
        if task.transitions >= self.max_transitions:
            return self._terminal(task, TaskState.DEAD,
                                  f"转移次数达上限 {self.max_transitions}（防无限升级）",
                                  "budget_exceeded")

        # ── 与阶段无关的通用处理（先判，保证语义一致）──────────────
        if ev.signal == "policy_denied":
            return self._terminal(task, TaskState.POLICY_DENIED, ev.reason, ev.signal)
        if ev.signal == "throttled":
            return self._retry(task, ev.reason, ev, throttled=True)
        if ev.signal == "transient_failure":
            return self._retry(task, ev.reason, ev)
        if ev.signal == "resource_denied":
            return self._terminal(task, TaskState.DEAD, ev.reason, ev.signal)
        if ev.signal == "permanent_failure":
            return self._terminal(task, TaskState.DEAD, ev.reason, ev.signal)
        if ev.signal == "parser_failed":
            # **"解析失败"不是失败**：原始层已经保住了事实，这条只是"现在解释不了，延期解释"。
            # 它是**与阶段无关**的规则——直连阶段解析失败和在制品阶段解析失败是同一件事
            # （S6 门禁 A2 用例就是在直连阶段走到这里的；以前只在制品阶段有这条规则，
            #   直连阶段会掉进"不猜"兜底被误判成 dead）。
            return self._terminal(task, TaskState.DONE,
                                  "解析失败但原始已存 → 延期解释（不是失败）",
                                  ev.signal, decision="keep_raw")

        # ── 阶段相关 ────────────────────────────────────────────
        if stage == Stage.CLASSIFY:
            return Decision(next_stage=Stage.DIRECT, environment=Environment.NETWORK,
                            reason="分类完成 → 直连（L1）")

        if stage == Stage.DIRECT:
            if ev.signal == "ok":
                return self._terminal(task, TaskState.DONE, "直连成功且内容可用（进捕获/理解）",
                                      ev.signal, decision="capture")
            if ev.signal in ("media_manifest", "large_object"):
                return self._to_stage(task, Stage.ARTIFACT, ev,
                                      "命中媒体清单/大对象 → 制品与媒体环境")
            if ev.signal == "stream":
                return self._unsupported(task, ev, "流式适配器（SSE/WebSocket）",
                                         "V0.1 未启用协议适配器")
            if ev.signal == "empty_content":
                return self._to_stage(task, Stage.BROWSER, ev,
                                      "内容空壳 → 需要真实浏览器环境")
            if ev.signal == "unknown_format":
                return self._to_stage(task, Stage.ARTIFACT, ev,
                                      "未知二进制 → 只捕获原始，延期解释")

        if stage == Stage.INSPECT:
            if ev.signal == "ok":
                return self._terminal(task, TaskState.DONE, "内容检查通过", ev.signal)
            if ev.signal == "empty_content":
                return self._to_stage(task, Stage.BROWSER, ev,
                                      "内容质量不足 → 需要真实浏览器环境")

        if stage == Stage.ARTIFACT:
            if ev.signal == "ok":
                return self._terminal(task, TaskState.DONE, "制品获取成功（已验产物）", ev.signal)

        if stage in (Stage.BROWSER, Stage.OBSERVE):
            if ev.signal == "ok":
                return self._terminal(task, TaskState.DONE, "浏览器环境拿到可用内容", ev.signal)
            if ev.signal == "network_activity":
                return self._terminal(task, TaskState.DONE,
                                      "观察到网络活动 → 已入原始层，可派生新任务",
                                      ev.signal, decision="capture_observed")

        # ── 兜底：有证据但没规则 → 明确失败，不猜 ──────────────────
        return self._terminal(task, TaskState.DEAD,
                              f"当前阶段 {stage} 下没有匹配 {ev.signal} 的转移规则（不猜）",
                              ev.signal)

    # ── 内部 ────────────────────────────────────────────────────
    def _stage_of(self, task: Task) -> str:
        try:
            return (task.policy or {}).get("stage") or Stage.DIRECT
        except Exception:
            return Stage.DIRECT

    def _terminal(self, task: Task, state: str, reason: str, signal: str,
                  decision: str = "") -> Decision:
        ev = from_decision(signal, decision or state, reason, stage="route")
        return Decision(terminal=state, reason=reason, evidence=ev)

    def _retry(self, task: Task, reason: str, ev: Evidence, throttled: bool = False) -> Decision:
        """可重试：限流走**独立计数**（不计 attempts）；其它失败计 attempts。"""
        if throttled:
            if task.throttles + 1 >= task.budget.max_throttles:
                return self._terminal(task, TaskState.DEAD,
                                      f"连续被限流 {task.throttles + 1} 次 → 转死信：{reason}",
                                      "throttled")
            return Decision(next_stage=Stage.DIRECT, environment=Environment.NETWORK,
                            reason=f"被限流 → 退避后重试（独立计数 {task.throttles + 1}）：{reason}",
                            evidence=from_decision("throttled", "retry_backoff", reason, stage="route"))
        if task.attempts + 1 >= task.budget.max_attempts:
            return self._terminal(task, TaskState.DEAD,
                                  f"重试已达上限 {task.budget.max_attempts}：{reason}", ev.signal)
        return Decision(next_stage=Stage.DIRECT, environment=Environment.NETWORK,
                        reason=f"可重试失败（第 {task.attempts + 1} 次）：{reason}",
                        evidence=from_decision("transient_failure", "retry", reason, stage="route"))

    def _to_stage(self, task: Task, stage: str, ev: Evidence, reason: str) -> Decision:
        """升级到某个阶段——**未启用就只记候选并终止**（不偷偷起环境）。"""
        env = STAGE_ENV.get(stage)
        if env is not None and env not in self.enabled:
            allowed = "、".join(self.enabled) or "（无）"
            ev2 = from_decision(ev.signal, f"{env}_candidate",
                                f"{reason}；但环境 {env} 未启用（已启用：{allowed}）→ 只记候选",
                                stage="route", target_stage=stage)
            return Decision(terminal=TaskState.DEAD,
                            reason=f"{reason}；环境 {env} 未启用 → 记候选并终止（不偷偷起环境）",
                            evidence=ev2)
        ev2 = from_decision(ev.signal, f"escalate_to_{stage}", reason, stage="route",
                            target_stage=stage)
        return Decision(next_stage=stage, environment=env, reason=reason, evidence=ev2)

    def _unsupported(self, task: Task, ev: Evidence, what: str, why: str) -> Decision:
        ev2 = from_decision(ev.signal, "unsupported_candidate",
                            f"{what}：{why} → 记候选并终止", stage="route")
        return Decision(terminal=TaskState.DEAD,
                        reason=f"{what} 未启用（{why}）→ 记候选并终止", evidence=ev2)

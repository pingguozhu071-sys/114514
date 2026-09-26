# -*- coding: utf-8 -*-
"""证据驱动路由：**有界**状态机（不是 AI，也不是「聪明」的东西）

它只做四件事（《写作.txt》§十五）：
    1) 识别（What do I know?）——看证据；
    2) 判断（What execution modes are valid?）——看当前启用了哪些环境；
    3) 选择（Which valid mode is cheapest?）——**便宜先行**；
    4) 升级（Did execution provide evidence that another mode is necessary?）——**只在有证据时**。

三条硬约束（本文件里都能看到）：
  * **转移有界**：次数超过预算 → 直接 dead（防「坏目标变资源黑洞」）；
  * **每次转移都给理由**：返回的 `Decision` 必须带 reason **和 evidence**，并被记进任务证据链
    （`start()` 与「分类完成 → 直连」这两处曾经只有 reason、没有证据，S6 门禁 K 用例守着）；
  * **未启用的环境只记「候选」**：例如 V0.1 不含浏览器环境，遇到「内容空壳」就记一条
    browser 候选证据并终止，而**不是**偷偷起浏览器。

环境名（`Environment`）：`network`（直连网络）/ `artifact`（制品与媒体）/ `browser`（浏览器运行时）。
阶段（`Stage`）就是 Progressive Acquisition 的 L0–L5：分类 → 直连 → 内容检查 → 浏览器 → 网络观察 → 专用制品。

**转移是表，不是 if/else 链**（`RULES`：顺序 = 优先级，先匹配先赢）：
    一行 = 适用阶段 + 触发信号 + **具名谓词** + 动作/目标 + **理由模板**。
    `on_evidence` 只做三件事——查表、求值谓词、产 `Decision`；
    **新增一种信号 / 一条转移 = 加一行表（需要时再加一个模块级谓词函数），不改 `on_evidence` 本体**。
    这是「专家只有三个 + 顶层只留几个对象」那条设计承诺的落地方式：S6 门禁 L 用例
    往表里**注入**一行规则并断言行为随之改变，而函数体一个字节都没动。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from daedalus.core.evidence import Evidence, from_decision
from daedalus.core.task import Task, TaskState

logger = logging.getLogger(__name__)

__all__ = ["Environment", "Stage", "Decision", "TransitionRule", "RULES", "rules_summary",
           "Router"]


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


# 每个阶段需要哪个环境（未启用 → 只能记「候选」）
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


# ══════════════════════════════════════════════════════════════════
# 转移表：**数据，不是代码**。改行为 = 改表；改表不改 `on_evidence`。
# ══════════════════════════════════════════════════════════════════
ANY_SIGNAL = "*"                 # 任何信号都触发（「这一步在这个阶段必然发生」的那些行）

# 动作（`TransitionRule.action`）：决定这一行**产出什么形状**的决定
ACTION_STAGE = "to_stage"        # 升级到某个阶段（未启用环境 → 记候选并终止）
ACTION_TERMINAL = "terminal"     # 收在某个终态
ACTION_RETRY = "retry"           # 退避重试（计 attempts）
ACTION_THROTTLE = "throttle"     # 退避重试（计 throttles，**不计 attempts**）
ACTION_CANDIDATE = "candidate"   # 未启用的能力：记候选并终止（不偷偷起环境/上手段）


# ── 具名谓词（模块级函数：可命名、可单测、可被门禁直接引用）────────────
def _always(ctx: RuleContext) -> bool:
    """条件恒成立——这一行「看到信号就走」，理由由理由模板给。"""
    return True


def _budget_exhausted(ctx: RuleContext) -> bool:
    """五维预算（墙钟/字节/转移/重试/限流）任一耗尽都先收口，并记下是哪一维。"""
    dim = ctx.task.exhausted()
    return bool(dim) and ctx.note(dim=dim)


def _transitions_capped(ctx: RuleContext) -> bool:
    """路由自己的转移次数上限（与预算里的 max_transitions 是同一个量，先判预算）。"""
    return ctx.task.transitions >= ctx.router.max_transitions


def _throttles_capped(ctx: RuleContext) -> bool:
    """连续被限流到上限 → 转死信（限流走**独立计数**，不看 attempts）。"""
    return ctx.task.throttles + 1 >= ctx.task.budget.max_throttles \
        and ctx.note(next=ctx.task.throttles + 1)


def _throttles_left(ctx: RuleContext) -> bool:
    """还有限流余量 → 退避后重试（独立计数加一，**不占 attempts**）。"""
    return ctx.task.throttles + 1 < ctx.task.budget.max_throttles \
        and ctx.note(next=ctx.task.throttles + 1)


def _attempts_capped(ctx: RuleContext) -> bool:
    """重试次数到上限 → 转死信（重试走 attempts 这本账，不看 throttles）。"""
    return ctx.task.attempts + 1 >= ctx.task.budget.max_attempts \
        and ctx.note(max=ctx.task.budget.max_attempts)


def _attempts_left(ctx: RuleContext) -> bool:
    """还有重试余量 → 退避后重试（attempts 加一，**不占 throttles**）。"""
    return ctx.task.attempts + 1 < ctx.task.budget.max_attempts \
        and ctx.note(next=ctx.task.attempts + 1)


def _stream_needs_adapter(ctx: RuleContext) -> bool:
    """流式响应要协议适配器（SSE/WebSocket），V0.1 没有 → 记候选，不偷偷连。"""
    return ctx.note(what="流式适配器（SSE/WebSocket）", why="V0.1 未启用协议适配器")


@dataclass
class RuleContext:
    """一次查表的现场：任务 + 证据 + 当前阶段 + 路由器。

    **只读**，除了 `why` / `fields`：谓词可以往那里补一句细节（让理由模板填得出话），
    并用 `note()` 一步完成「填字段 + 返回 True」。
    """

    task: Task
    ev: Evidence
    stage: str
    router: "Router"
    why: str = ""
    fields: dict[str, Any] = field(default_factory=dict)

    def note(self, **kw) -> bool:
        """谓词用它补充理由里的细节，并返回 True（谓词因此可以一行写完）。"""
        self.fields.update(kw)
        return True

    def render(self, template: str) -> str:
        """把理由模板填成一句人话（`{why}` 缺省用证据自己的原因）。"""
        values: dict[str, Any] = {"why": self.why or self.ev.reason, "stage": self.stage,
                                  "signal": self.ev.signal,
                                  "limit": self.router.max_transitions}
        values.update(self.fields)
        return template.format(**values)


Predicate = Callable[[RuleContext], bool]


@dataclass(frozen=True)
class TransitionRule:
    """一行转移规则（**数据**）。

    `name`（稳定标识，门禁与文档引用它）/ `stages`（适用阶段，`Stage.ALL` = 任何阶段）/
    `signal`（触发信号，`ANY_SIGNAL` = 任何信号；也接受一组信号）/
    `when`（具名条件谓词，**模块级函数**，不许塞 lambda 串）/
    `action` + `target`（去哪或收在哪）/ `reason`（理由模板）/
    `decision`（证据里的 decision 标签，缺省 = 目标）/ `evidence_signal`（证据信号，
    缺省 = 触发信号）/ `evidence_reason`（证据里的话术，缺省 = 同 reason）/
    `requires_env`（升级前是否要检查目标阶段的环境已启用——L0 分类不出网，故为 False）。
    """

    name: str
    stages: tuple[str, ...]
    signal: str | tuple[str, ...]
    when: Predicate
    action: str
    target: str
    reason: str
    decision: str = ""
    evidence_signal: str = ""
    evidence_reason: str = ""
    requires_env: bool = True

    @property
    def signal_label(self) -> str:
        """给门禁/文档看的信号标签（一组信号用 `/` 连起来）。"""
        if isinstance(self.signal, tuple):
            return "/".join(self.signal)
        return self.signal

    def matches(self, stage: str, signal: str) -> bool:
        """阶段 + 信号是否命中这一行（谓词另行求值）。"""
        if not self._signal_ok(signal):
            return False
        return stage in self.stages

    def _signal_ok(self, signal: str) -> bool:
        if self.signal == ANY_SIGNAL:
            return True
        if isinstance(self.signal, tuple):
            return signal in self.signal
        return self.signal == signal


# 表：**顺序 = 优先级**。与阶段无关的通用行在前（先判，保证语义一致），阶段行在后。
RULES: tuple[TransitionRule, ...] = (
    # ── 与阶段无关：任何阶段看到这些信号都走同一条路 ──────────────────
    TransitionRule(
        name="budget_exhausted", stages=Stage.ALL, signal=ANY_SIGNAL,
        when=_budget_exhausted, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="预算耗尽（{dim}）", evidence_signal="budget_exceeded"),
    TransitionRule(
        name="transitions_capped", stages=Stage.ALL, signal=ANY_SIGNAL,
        when=_transitions_capped, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="转移次数达上限 {limit}（防无限升级）", evidence_signal="budget_exceeded"),
    TransitionRule(
        name="policy_denied", stages=Stage.ALL, signal="policy_denied",
        when=_always, action=ACTION_TERMINAL, target=TaskState.POLICY_DENIED,
        reason="{why}"),
    TransitionRule(
        name="throttle_capped", stages=Stage.ALL, signal="throttled",
        when=_throttles_capped, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="连续被限流 {next} 次 → 转死信：{why}"),
    TransitionRule(
        name="throttle_retry", stages=Stage.ALL, signal="throttled",
        when=_throttles_left, action=ACTION_THROTTLE, target=Stage.DIRECT,
        reason="被限流 → 退避后重试（独立计数 {next}）：{why}"),
    TransitionRule(
        name="attempts_capped", stages=Stage.ALL, signal="transient_failure",
        when=_attempts_capped, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="重试已达上限 {max}：{why}"),
    TransitionRule(
        name="transient_retry", stages=Stage.ALL, signal="transient_failure",
        when=_attempts_left, action=ACTION_RETRY, target=Stage.DIRECT,
        reason="可重试失败（第 {next} 次）：{why}"),
    TransitionRule(
        name="resource_denied", stages=Stage.ALL, signal="resource_denied",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="{why}"),
    TransitionRule(
        name="permanent_failure", stages=Stage.ALL, signal="permanent_failure",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DEAD,
        reason="{why}"),
    # 「解析失败」不是失败：原始层已经保住了事实，这条只是「现在解释不了，延期解释」。
    # 它是**与阶段无关**的规则——直连阶段解析失败和在制品阶段解析失败是同一件事
    # （S6 门禁 A2 用例就是在直连阶段走到这里的；以前只在制品阶段有这条规则，
    #   直连阶段会掉进「不猜」兜底被误判成 dead）。
    TransitionRule(
        name="parser_failed_is_not_failure", stages=Stage.ALL, signal="parser_failed",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        reason="解析失败但原始已存 → 延期解释（不是失败）", decision="keep_raw"),

    # ── 阶段相关（便宜先行：L0 → L1 → L2 → L3 → L4 → L5）─────────────
    TransitionRule(
        name="classify_done_to_direct", stages=(Stage.CLASSIFY,), signal=ANY_SIGNAL,
        when=_always, action=ACTION_STAGE, target=Stage.DIRECT,
        reason="分类完成 → 直连（L1）", requires_env=False),
    TransitionRule(
        name="direct_ok", stages=(Stage.DIRECT,), signal="ok",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        reason="直连成功且内容可用（进捕获/理解）", decision="capture"),
    TransitionRule(
        name="direct_media_to_artifact", stages=(Stage.DIRECT,),
        signal=("media_manifest", "large_object"),
        when=_always, action=ACTION_STAGE, target=Stage.ARTIFACT,
        reason="命中媒体清单/大对象 → 制品与媒体环境"),
    TransitionRule(
        name="direct_stream_candidate", stages=(Stage.DIRECT,), signal="stream",
        when=_stream_needs_adapter, action=ACTION_CANDIDATE, target=TaskState.DEAD,
        reason="{what}未启用（{why}）→ 记候选并终止",
        evidence_reason="{what}：{why} → 记候选并终止"),
    TransitionRule(
        name="direct_empty_to_browser", stages=(Stage.DIRECT,), signal="empty_content",
        when=_always, action=ACTION_STAGE, target=Stage.BROWSER,
        reason="内容空壳 → 需要真实浏览器环境"),
    TransitionRule(
        name="direct_unknown_format_to_artifact", stages=(Stage.DIRECT,),
        signal="unknown_format",
        when=_always, action=ACTION_STAGE, target=Stage.ARTIFACT,
        reason="未知二进制 → 只捕获原始，延期解释"),
    TransitionRule(
        name="inspect_ok", stages=(Stage.INSPECT,), signal="ok",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        reason="内容检查通过"),
    TransitionRule(
        name="inspect_empty_to_browser", stages=(Stage.INSPECT,), signal="empty_content",
        when=_always, action=ACTION_STAGE, target=Stage.BROWSER,
        reason="内容质量不足 → 需要真实浏览器环境"),
    TransitionRule(
        name="artifact_ok", stages=(Stage.ARTIFACT,), signal="ok",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        reason="制品获取成功（已验产物）"),
    TransitionRule(
        name="browser_ok", stages=(Stage.BROWSER, Stage.OBSERVE), signal="ok",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        reason="浏览器环境拿到可用内容"),
    TransitionRule(
        name="browser_network_activity", stages=(Stage.BROWSER, Stage.OBSERVE),
        signal="network_activity",
        when=_always, action=ACTION_TERMINAL, target=TaskState.DONE,
        decision="capture_observed",
        reason="观察到网络活动 → 已入原始层，可派生新任务"),
)


def rules_summary() -> list[dict]:
    """把表**读出来**（门禁与文档引用它，而不是去数源码行）。

    每行：名字 / 适用阶段 / 触发信号 / 谓词名 / 动作 / 目标 / 证据标签 / 理由模板。
    """
    out: list[dict] = []
    for r in RULES:
        out.append({"name": r.name, "stages": list(r.stages), "signal": r.signal_label,
                    "when": getattr(r.when, "__name__", "?"), "action": r.action,
                    "target": r.target, "decision": r.decision or r.target,
                    "reason": r.reason})
    return out


class Router:
    """确定性策略引擎 + 有界状态机（**可解释**：每个决定都有 reason 和 evidence）。"""

    def __init__(self, enabled_environments=(Environment.NETWORK, Environment.ARTIFACT),
                 max_transitions: int = 8):
        self.enabled = tuple(enabled_environments)
        self.max_transitions = int(max_transitions)

    # ── 对外主入口 ────────────────────────────────────────────────
    def start(self, task: Task) -> Decision:
        """任务开始：L0 分类 → L1 直连（最便宜的路）。**两条路都带证据**。"""
        if task.state in TaskState.TERMINAL:
            ev = from_decision("classified", "already_terminal",
                               f"任务已处于终态 {task.state} → 不再路由（不重开已了结的事）",
                               stage="route", state=task.state)
            return Decision(terminal=task.state,
                            reason=f"任务已处于终态 {task.state}", evidence=ev)
        ev = from_decision("classified", "go_direct",
                           f"初始路径：直连 {Environment.NETWORK}（最便宜；"
                           f"{Stage.CLASSIFY} → {Stage.DIRECT}）",
                           stage="route", target_stage=Stage.DIRECT)
        return Decision(next_stage=Stage.DIRECT, environment=Environment.NETWORK,
                        reason="初始路径：直连网络（最便宜）", evidence=ev)

    def on_evidence(self, task: Task, ev: Evidence) -> Decision:
        """查表决定下一步：**先匹配先赢**；一行都没命中 → 明确失败（不猜）。

        新增信号/转移请改 `RULES`（加一行 + 需要时加一个谓词函数），**不要在这里加 if**。
        """
        ctx = RuleContext(task=task, ev=ev, stage=self._stage_of(task), router=self)
        for rule in RULES:
            if rule.matches(ctx.stage, ev.signal) and rule.when(ctx):
                return self._act(rule, ctx)
        return self._terminal(task, TaskState.DEAD,
                              f"当前阶段 {ctx.stage} 下没有匹配 {ev.signal} 的转移规则（不猜）",
                              ev.signal)

    # ── 内部 ────────────────────────────────────────────────────
    def _stage_of(self, task: Task) -> str:
        try:
            return (task.policy or {}).get("stage") or Stage.DIRECT
        except Exception:
            return Stage.DIRECT

    def _act(self, rule: TransitionRule, ctx: RuleContext) -> Decision:
        """按一行规则产出 Decision（**唯一的分派点**：加动作只改这里）。"""
        # 理由**必须非空**（硬约束）：证据没带话术时退回到规则名，绝不产出空理由。
        reason = ctx.render(rule.reason).strip() or f"规则 {rule.name}（信号 {ctx.ev.signal}）"
        if rule.action == ACTION_STAGE:
            return self._to_stage(ctx.task, rule.target, ctx.ev, reason,
                                  check_env=rule.requires_env)
        if rule.action == ACTION_TERMINAL:
            return self._terminal(ctx.task, rule.target, reason,
                                  rule.evidence_signal or ctx.ev.signal, rule.decision)
        if rule.action == ACTION_RETRY:
            return self._retry(ctx.task, reason, ctx.ev)
        if rule.action == ACTION_THROTTLE:
            return self._retry(ctx.task, reason, ctx.ev, throttled=True)
        if rule.action == ACTION_CANDIDATE:
            return self._unsupported(ctx.task, ctx.ev, reason,
                                     ctx.render(rule.evidence_reason or rule.reason))
        raise ValueError(f"未知动作 {rule.action!r}（规则 {rule.name}）")

    def _terminal(self, task: Task, state: str, reason: str, signal: str,
                  decision: str = "") -> Decision:
        ev = from_decision(signal, decision or state, reason, stage="route")
        return Decision(terminal=state, reason=reason, evidence=ev)

    def _retry(self, task: Task, reason: str, ev: Evidence, throttled: bool = False) -> Decision:
        """可重试：限流走**独立计数**（不计 attempts）；其它失败计 attempts。

        上限**由表决定**（`throttle_capped` / `attempts_capped` 两行谓词，排在重试行之前）；
        下面这两处同条件检查是**兜底**：将来谁新增一条 `ACTION_RETRY` / `ACTION_THROTTLE`
        规则却忘了配对应的收口行，也不会退化成「无限重试」（不变量优先于「只有一处真相」）。
        """
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

    def _to_stage(self, task: Task, stage: str, ev: Evidence, reason: str,
                  *, check_env: bool = True) -> Decision:
        """升级到某个阶段——**未启用就只记候选并终止**（不偷偷起环境）。"""
        env = STAGE_ENV.get(stage)
        if check_env and env is not None and env not in self.enabled:
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

    def _unsupported(self, task: Task, ev: Evidence, reason: str, ev_reason: str = "") -> Decision:
        """未启用的能力：**记候选并终止**（不偷偷起环境、也不偷偷上手段）。"""
        ev2 = from_decision(ev.signal, "unsupported_candidate", ev_reason or reason, stage="route")
        return Decision(terminal=TaskState.DEAD, reason=reason, evidence=ev2)

# -*- coding: utf-8 -*-
"""证据（Evidence）：路由决策的**唯一依据**，且必须**可回放**。

"路由器不神化"这条原则要落地，就需要一件事：**每次改道都要能说出为什么**。
所以每次路由/失败/成功都记一条 Evidence：
    signal（我看到了什么）→ decision（我决定走哪条路）→ reason（为什么）

信号来自**高质量事实**（不是猜测）：状态码、响应头、声明的 MIME、**魔数**、体积、
重定向史、解析结果、内容质量、观测到的网络活动、历史任务状态、资源可用量。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

__all__ = ["Evidence", "SIGNALS", "from_response", "from_parse", "from_decision"]

# 允许的信号集合（**有界**：新增信号请同时考虑路由表怎么用它）
SIGNALS = (
    "classified",        # L0：URL/声明类型分类的结论
    "ok",                # 拿到可用内容
    "empty_content",     # 内容空壳/质量不足
    "media_manifest",    # 发现媒体清单（m3u8/dash）
    "large_object",      # 大对象（应走制品通道）
    "stream",            # 流式响应（SSE/WS）
    "unknown_format",    # 未知二进制（只捕获，延期解释）
    "parser_failed",     # 解析失败（原始已存，可延期/换解析器）
    "transient_failure", # 瞬时失败（超时/连接）
    "throttled",         # 被限流（429/503）
    "permanent_failure", # 永久失败（404/410）
    "policy_denied",     # 合规拒绝（robots/SSRF/需授权）
    "budget_exceeded",   # 预算耗尽
    "resource_denied",   # 资源缺省即拒绝
    "network_activity",  # 观测到的网络活动（浏览器环境的产物）
)


@dataclass(frozen=True)
class Evidence:
    """一条证据。`facts` 里只放**事实**（可核对的值），不放推测。"""

    signal: str
    decision: str = ""          # 决定走哪条路（如 "direct" / "artifact" / "browser_candidate"）
    reason: str = ""            # 人话原因（要能读懂，别只写错误码）
    stage: str = ""             # 产生它的阶段（classify/direct/inspect/parse/...）
    facts: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def __post_init__(self):
        if self.signal not in SIGNALS:
            raise ValueError(f"未知信号: {self.signal!r}（合法值见 SIGNALS）")

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Evidence":
        d = json.loads(text or "{}")
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# ── 常用构造：把"原始事实"抽成信号 ────────────────────────────────
def from_response(status: int, headers: dict | None = None, size: int = 0,
                  final_url: str = "", hops: int = 0, stage: str = "direct",
                  decision: str = "") -> Evidence:
    """按响应事实给出一条证据（不猜内容，只看能核对的事实）。"""
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    ctype = str(h.get("content-type") or "").split(";", 1)[0].strip().lower()
    facts = {"status": int(status), "content_type": ctype, "size": int(size),
             "final_url": str(final_url)[:300], "redirect_hops": int(hops)}
    signal, reason = "ok", f"HTTP {status}"
    if status in (429, 503):
        signal = "throttled"
        reason = f"被限流 HTTP {status}（Retry-After={h.get('retry-after')}）"
    elif status in (404, 410):
        signal = "permanent_failure"
        reason = f"永久失败 HTTP {status}"
    elif 500 <= status < 600:
        signal = "transient_failure"
        reason = f"服务端错误 HTTP {status}（可重试）"
    elif "mpegurl" in ctype or "dash" in ctype:
        signal = "media_manifest"
        reason = f"媒体清单（{ctype}）→ 应交给制品与媒体环境"
    elif "text/event-stream" in ctype or "websocket" in ctype:
        signal = "stream"
        reason = f"流式响应（{ctype}）→ 应交给协议适配器"
    elif ctype in ("application/octet-stream", "application/zip") or size > 8 << 20:
        signal = "large_object"
        reason = f"大对象/未知二进制（{ctype or 'n/a'}, {size} 字节）"
    return Evidence(signal=signal, decision=decision, reason=reason, stage=stage, facts=facts)


def from_parse(ok: bool, quality: float = 0.0, detail: str = "",
               stage: str = "parse") -> Evidence:
    """按解析结果给出一条证据（解析失败也要留痕：原始已存，可延期解释）。"""
    if ok:
        return Evidence(signal="ok", decision="capture", stage=stage,
                        reason=f"解析成功（质量分 {quality:.2f}）{detail}".strip(),
                        facts={"quality": float(quality)})
    return Evidence(signal="parser_failed", decision="keep_raw",
                    stage=stage,
                    reason=f"解析失败，原始数据保留可延期解释：{detail}"[:300],
                    facts={"quality": float(quality)})


def from_decision(signal: str, decision: str, reason: str, stage: str = "route",
                  **facts) -> Evidence:
    return Evidence(signal=signal, decision=decision, reason=reason, stage=stage, facts=facts)

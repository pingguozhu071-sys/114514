# -*- coding: utf-8 -*-
"""告警阈值：把"数字难看"变成"**能说清哪一条超了、超了多少**"（清单 L4）

为什么要有默认值：没有默认阈值的监控等于没有监控——用户不会去配，出事时也说不清。
为什么全部可配：不同机器（8GB 本子 vs 32GB 台机）、不同目标站的合理值不一样。

输出形状固定（GUI/CLI 共用，且能进 JSON）：
    {"key": "queue.depth", "level": "warn|critical", "value": ..., "limit": ...,
     "message": "队列 frontier 深度 85000 / 上限 100000（85%，阈值 80%）"}

**这条也不许静默**：指标缺失时（比如没装 psutil）返回 `unknown` 级别的一条，说明"看不出来"，
而不是"没超标"。看不见 ≠ 没问题。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

__all__ = ["Thresholds", "Alert", "evaluate"]


@dataclass(frozen=True)
class Thresholds:
    """阈值集合（键名与 `config.DEFAULTS["alerts"]` 一一对应）。"""

    queue_depth_pct: float = 80.0
    failure_rate_pct: float = 20.0
    disk_free_pct: float = 10.0
    rss_mb: float = 3072.0
    latency_p95_ms: float = 5000.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> "Thresholds":
        from daedalus.config import merged
        c = merged(cfg if (cfg and "alerts" in cfg) else {"alerts": cfg or {}})
        a = c.get("alerts") or {}
        fields = {f for f in cls.__dataclass_fields__}       # type: ignore[attr-defined]
        return cls(**{k: float(v) for k, v in a.items() if k in fields and v is not None})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Alert:
    key: str
    level: str                     # ok | warn | critical | unknown
    value: float | None = None
    limit: float | None = None
    message: str = ""
    facts: dict = field(default_factory=dict)


def _alert(key: str, value, limit, pct: float, *, label: str, unit: str = "",
           extra: dict | None = None) -> Alert:
    """按"用掉的比例"定级：≥阈值 = warn，≥阈值 + 20 个百分点 = critical。"""
    if value is None or limit in (None, 0):
        return Alert(key, "unknown", value, limit,
                     f"{label}：读不到（指标缺失或未采样）——看不见不等于没问题",
                     extra or {})
    warn = float(pct)
    crit = min(100.0, warn + 20.0)
    used = float(value) / float(limit) * 100.0
    level = "critical" if used >= crit else ("warn" if used >= warn else "ok")
    return Alert(key, level, float(value), float(limit),
                 f"{label}：{value:.4g}{unit} / 上限 {limit:.4g}{unit}"
                 f"（{used:.1f}%，阈值 {warn:.0f}%）", extra or {})


def evaluate(metrics_summary: dict, *, thresholds: Thresholds | None = None,
             queue_limits: dict | None = None, queue_depths: dict | None = None) -> list[Alert]:
    """按阈值评估一批指标 → 告警列表（**永远返回列表，不抛异常**）。"""
    th = thresholds or Thresholds()
    s = dict(metrics_summary or {})
    out: list[Alert] = []

    # ① 队列积压（每个队列一条；深度来自 gauge `queue.depth{name=...}`）
    for name, depth in (queue_depths or {}).items():
        limit = (queue_limits or {}).get(name)
        out.append(_alert(f"queue.depth.{name}", depth, limit, th.queue_depth_pct,
                          label=f"队列 {name} 深度", extra={"name": name}))

    # ② 失败率
    done = float(s.get("tasks_done", 0.0) or 0.0)
    failed = float(s.get("tasks_failed", 0.0) or 0.0)
    total = done + failed
    rate = (failed / total * 100.0) if total > 0 else None
    if rate is None:
        out.append(Alert("task.failure_rate", "unknown", None, th.failure_rate_pct,
                         "失败率：还没有完成任务，无法评估"))
    else:
        level = "critical" if rate >= th.failure_rate_pct * 1.5 else (
            "warn" if rate >= th.failure_rate_pct else "ok")
        out.append(Alert("task.failure_rate", level, round(rate, 2), th.failure_rate_pct,
                         f"失败率 {rate:.1f}%（{failed:.0f}/{total:.0f}，阈值 {th.failure_rate_pct:.0f}%）"))

    # ③ 磁盘水位（百分比是"剩余"，与上面"用掉"的方向相反，单独判）
    free_pct = s.get("disk_free_pct")
    if free_pct is None:
        # 由 free/total 兜算（summary 里只有 MB 时不强求）
        out.append(Alert("disk.free_pct", "unknown", None, th.disk_free_pct,
                         "磁盘水位：未采样（数据根还没定或采样器未启动）"))
    else:
        fp = float(free_pct)
        level = "critical" if fp <= th.disk_free_pct / 2 else (
            "warn" if fp <= th.disk_free_pct else "ok")
        out.append(Alert("disk.free_pct", level, round(fp, 2), th.disk_free_pct,
                         f"数据盘剩余 {fp:.1f}%（下限 {th.disk_free_pct:.0f}%）"))

    # ④ 常驻内存
    out.append(_alert("proc.rss_mb", s.get("rss_mb"), th.rss_mb, 80.0,
                      label="常驻内存", unit=" MB"))

    # ⑤ 请求 p95 延迟（毫秒）
    p95 = s.get("net_latency_p95")
    p95_ms = (float(p95) * 1000.0) if p95 is not None else None
    out.append(_alert("net.latency_p95_ms", p95_ms, th.latency_p95_ms, 80.0,
                      label="请求 p95 延迟", unit=" ms"))

    # ⑥ 指标面自身健康（序列溢出说明有地方在建高基数标签，必须让人看到）
    if float(s.get("series_overflow", 0.0) or 0.0) > 0:
        out.append(Alert("metrics.series_overflow", "warn", float(s["series_overflow"]), 0.0,
                         f"指标序列溢出 {s['series_overflow']:.0f} 条（有地方在用高基数标签）"))
    return out


def worst_level(alerts: list[Alert]) -> str:
    """取最严重的级别（CLI 退出码 / GUI 颜色用）。"""
    order = {"critical": 3, "warn": 2, "unknown": 1, "ok": 0}
    worst = "ok"
    for a in alerts or []:
        if order.get(a.level, 0) > order.get(worst, 0):
            worst = a.level
    return worst

# -*- coding: utf-8 -*-
"""资源计划：把"用多少资源"写成**可校验的算术**，而不是散落在各处的魔法数字

T1/02 §2 的硬要求：常驻内存 ≤4GB，且必须**写出** `Σ(池规模 × 单任务峰值) ≤ 4GB`。
这句话如果只是文档里的一句口号，就没有任何约束力；写成一个能算、能验、能报错的
`ResourcePlan` 才有——S7 门禁会直接断言这条不等式成立。

三件必须显式的事：
  1) **每个队列都有上限**（无界队列是 OOM 第一原因）——`validate()` 会拒绝 maxsize<=0；
  2) **每个池都是显式容量**（不"看机器性能自动调"）；
  3) **内存预算按算术核对**：池规模 × 单任务峰值之和 ≤ 预算，超出就报错并给出差多少。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["ResourcePlan", "PlanViolation", "MEMORY_BUDGET_MB"]

MB = 1 << 20
MEMORY_BUDGET_MB = 4096.0          # 常驻内存预算（T1 原文：≤4GB）


class PlanViolation(ValueError):
    """资源计划不自洽（无界队列 / 超预算）。**不给"警告后继续"的选项**。"""


@dataclass(frozen=True)
class ResourcePlan:
    """一张资源计划表（键名与 `config.DEFAULTS["limits"]` 一一对应）。

    默认值取自施工计划 §2.4 的吞吐判据：下载 128 线程（每域 ≤8）、解析 20 线程
    （重解析备选 12 进程）、写盘 1 线程 + 批提交；三条队列 200k / 10k / 20k。
    """

    download_threads: int = 128
    per_domain_concurrency: int = 8
    parse_threads: int = 20
    reparse_processes: int = 12
    writer_threads: int = 1
    subprocess_slots: int = 4
    browser_contexts: int = 0            # 缺省 0：浏览器环境未启用（缺省即拒绝）
    browser_pages: int = 0
    queue_frontier: int = 200_000
    queue_download_parse: int = 10_000
    queue_parse_store: int = 20_000
    queue_writer: int = 20_000
    batch_rows: int = 1000               # 500–2000 行
    flush_interval: float = 1.0          # 或每 1s
    # 单任务峰值内存估计（MB）：一个下载任务（缓冲区+连接）与一个解析任务（DOM+派生）
    peak_download_mb: float = 8.0
    peak_parse_mb: float = 48.0
    peak_browser_mb: float = 300.0
    memory_budget_mb: float = MEMORY_BUDGET_MB

    # ── 算术 ────────────────────────────────────────────────────
    def memory_terms(self) -> list[tuple[str, int, float, float]]:
        """`[(池名, 规模, 单任务峰值MB, 小计MB)]` —— **算给人看**，不是黑箱。"""
        return [
            ("download", int(self.download_threads), float(self.peak_download_mb),
             int(self.download_threads) * float(self.peak_download_mb)),
            ("parse", int(self.parse_threads), float(self.peak_parse_mb),
             int(self.parse_threads) * float(self.peak_parse_mb)),
            ("reparse", int(self.reparse_processes), float(self.peak_parse_mb),
             int(self.reparse_processes) * float(self.peak_parse_mb)),
            ("subprocess", int(self.subprocess_slots), float(self.peak_parse_mb),
             int(self.subprocess_slots) * float(self.peak_parse_mb)),
            ("browser", int(self.browser_contexts), float(self.peak_browser_mb),
             int(self.browser_contexts) * float(self.peak_browser_mb)),
            ("writer", int(self.writer_threads), float(self.peak_download_mb),
             int(self.writer_threads) * float(self.peak_download_mb)),
        ]

    def memory_total_mb(self) -> float:
        return sum(t[3] for t in self.memory_terms())

    def memory_headroom_mb(self) -> float:
        return float(self.memory_budget_mb) - self.memory_total_mb()

    def memory_arithmetic(self) -> str:
        """人话一行：`128×8(download) + 20×48(parse) + … = 2760 MB ≤ 4096 MB（余 1336 MB）`。

        形状刻意照抄施工计划 §2.4 的要求：**池规模 × 单任务峰值**，逐个池写出来。
        """
        body = " + ".join(f"{size}×{peak:g}({name})"
                          for name, size, peak, _ in self.memory_terms())
        return (f"{body} = {self.memory_total_mb():.0f} MB "
                f"≤ {self.memory_budget_mb:.0f} MB（余 {self.memory_headroom_mb():.0f} MB）")

    def queues(self) -> dict:
        return {"frontier": int(self.queue_frontier),
                "download_parse": int(self.queue_download_parse),
                "parse_store": int(self.queue_parse_store),
                "writer": int(self.queue_writer)}

    # ── 校验 ────────────────────────────────────────────────────
    def validate(self) -> "ResourcePlan":
        """不自洽就**抛异常**（不给"警告后继续"）。返回自身，方便链式使用。"""
        problems: list[str] = []
        for name, size in self.queues().items():
            if size <= 0:
                problems.append(f"队列 {name} 无上限（{size}）——无界队列是 OOM 第一原因")
        if int(self.writer_threads) != 1:
            problems.append(f"写盘线程必须恰好 1 个（当前 {self.writer_threads}）"
                            "：SQLite 单写者，多写会互相撞锁")
        if not (500 <= int(self.batch_rows) <= 2000):
            problems.append(f"批提交行数应在 500–2000（当前 {self.batch_rows}）")
        if float(self.flush_interval) > 2.0:
            problems.append(f"批提交间隔过长（{self.flush_interval}s）；应 ≤2s 或按行数触发")
        for name in ("download_threads", "parse_threads", "reparse_processes"):
            if int(getattr(self, name)) < 0:
                problems.append(f"{name} 不能为负（{getattr(self, name)}）")
        total = self.memory_total_mb()
        if total > float(self.memory_budget_mb):
            problems.append(f"内存预算超了 {total - self.memory_budget_mb:.0f} MB："
                            f"{self.memory_arithmetic()}")
        if problems:
            raise PlanViolation("；".join(problems))
        return self

    # ── 构造与导出 ──────────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg: dict | None) -> "ResourcePlan":
        from daedalus.config import merged
        c = merged(cfg if (cfg and "limits" in cfg) else {"limits": cfg or {}})
        lim = c.get("limits") or {}
        fields = {f for f in cls.__dataclass_fields__}       # type: ignore[attr-defined]
        kw: dict = {}
        for k, v in lim.items():
            if k in fields and v is not None:
                kw[k] = v
        plan = cls(**kw)
        plan.validate()
        return plan

    def to_dict(self) -> dict:
        d = asdict(self)
        d["memory_total_mb"] = round(self.memory_total_mb(), 1)
        d["memory_headroom_mb"] = round(self.memory_headroom_mb(), 1)
        d["memory_arithmetic"] = self.memory_arithmetic()
        return d

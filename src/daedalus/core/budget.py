# -*- coding: utf-8 -*-
"""任务预算：**每个任务都有上限**（时间 / 字节 / 转移次数 / 重试 / 限流次数）。

为什么必须有：没有预算的任务在坏目标上会变成**资源黑洞**——无限重试、无限升级、
或者下到 100GB。预算与"失败只能触发有限转移"是同一条纪律的两个面。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

__all__ = ["Budget", "DEFAULT"]

MB = 1 << 20


@dataclass(frozen=True)
class Budget:
    max_seconds: float = 600.0        # 单任务墙钟上限（含排队后的执行时间）
    max_bytes: int = 64 * MB          # 单任务下载上限
    max_transitions: int = 8          # **路由状态转移**上限（防"无限升级"，见 写作.txt §九）
    max_attempts: int = 3             # 失败重试上限（**限流不计在这里**）
    max_throttles: int = 10           # 连续被限流上限 → 转死信（独立计数）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Budget":
        fields = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in fields})

    # 常用档位（在任务构造处显式选择，避免各处拍脑袋）
    @classmethod
    def small(cls) -> "Budget":
        """小请求：一个页面/一个 JSON API。"""
        return cls(max_seconds=120.0, max_bytes=8 * MB, max_transitions=4)

    @classmethod
    def media(cls) -> "Budget":
        """媒体/大对象：允许更长时间与更大体积，但转移次数仍受限。"""
        return cls(max_seconds=3600.0, max_bytes=8 << 30, max_transitions=6)


DEFAULT = Budget()

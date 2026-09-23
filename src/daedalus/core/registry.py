# -*- coding: utf-8 -*-
"""资源注册表：**任务声明资源，注册表裁决**；**缺省即拒绝**。

这是 Ray 那条教训的落地：不显式声明的资源会被无限铺开（Ray 里 actor 默认"调度时占 1 CPU、
运行时占 0"），所以本工程的做法是——**没在注册表里登记、或登记容量为 0 的资源，一律拒绝**。

默认容量（与并发预算一致）：
    network    128   下载线程池
    thread      20   解析线程池（GIL 开启时超过核数无收益）
    process      0   **默认关**（"不要因为听起来更强就盲目引入 multiprocessing"）
    subprocess   2   外部二进制（ffmpeg 等）
    browser      0   **默认关**：没启用浏览器环境时，要浏览器的任务直接被拒
    async        0   V0.1 不含异步执行格
"""

from __future__ import annotations

import logging
import threading

from daedalus.core.task import ResourceRequest

logger = logging.getLogger(__name__)

__all__ = ["ResourceDenied", "ResourceRegistry", "DEFAULT_CAPACITIES"]

DEFAULT_CAPACITIES = {
    "network": 128,
    "thread": 20,
    "process": 0,
    "subprocess": 2,
    "browser": 0,
    "async": 0,
}


class ResourceDenied(Exception):
    """资源缺省即拒绝（**不是**传输失败；记录原因后按"当前环境不可用"处理）。"""


class ResourceRegistry:
    """容量登记 + 任务资源声明校验（线程安全）。"""

    def __init__(self, capacities: dict | None = None):
        self._lock = threading.Lock()
        self._cap = dict(DEFAULT_CAPACITIES)
        if capacities:
            self._cap.update({k: int(v) for k, v in capacities.items()})

    # ── 登记 ─────────────────────────────────────────────────────
    def register(self, name: str, capacity: int) -> None:
        """启用/调整一种执行资源的容量（启用浏览器环境时就会登记 `browser=1..N`）。"""
        with self._lock:
            self._cap[str(name)] = max(0, int(capacity))

    def capacity(self, name: str) -> int:
        with self._lock:
            return int(self._cap.get(name, 0))

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._cap)

    # ── 校验 ─────────────────────────────────────────────────────
    def problems(self, res: ResourceRequest) -> list[str]:
        """返回"这条声明为什么不成立"的清单（空 = 通过）。"""
        out: list[str] = []
        with self._lock:
            cap = dict(self._cap)
        for name in ("network", "browser", "process", "subprocess"):
            want = int(getattr(res, name, 0) or 0)
            if want <= 0:
                continue
            if name not in cap:
                out.append(f"未登记的资源类型: {name}")
            elif cap[name] <= 0:
                out.append(f"资源 {name} 未启用（容量 0）——**缺省即拒绝**")
            elif want > cap[name]:
                out.append(f"资源 {name} 申请 {want} 超过容量 {cap[name]}")
        if res.cpu not in ("low", "medium", "high"):
            out.append(f"未知 cpu 档位: {res.cpu!r}")
        if res.disk not in ("low", "high"):
            out.append(f"未知 disk 档位: {res.disk!r}")
        if int(res.memory_mb) <= 0:
            out.append("memory_mb 必须为正")
        if int(res.domain_concurrency) <= 0:
            out.append("domain_concurrency 必须为正（礼貌预算要用）")
        return out

    def require(self, res: ResourceRequest) -> None:
        """通过则静默返回；否则抛 `ResourceDenied`（带可读原因）。"""
        probs = self.problems(res)
        if probs:
            raise ResourceDenied("；".join(probs))

    def cost(self, res: ResourceRequest) -> dict:
        """把声明折成"账本上的数"（供观测与仲裁使用；**不**含业务细节）。"""
        return {"network": res.network, "browser": res.browser, "process": res.process,
                "subprocess": res.subprocess, "memory_mb": res.memory_mb,
                "cpu": res.cpu, "disk": res.disk, "domain_concurrency": res.domain_concurrency}

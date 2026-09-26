# -*- coding: utf-8 -*-
"""资源注册表：**任务声明资源，注册表裁决**；**缺省即拒绝**。

这是 Ray 那条教训的落地：不显式声明的资源会被无限铺开（Ray 里 actor 默认"调度时占 1 CPU、
运行时占 0"），所以本工程的做法是——**没在注册表里登记、或登记容量为 0 的资源，一律拒绝**。

默认容量（与并发预算一致）：
    network     128  下载线程池
    thread       20  解析线程池（GIL 开启时超过核数无收益）
    process       0  **默认关**（「不要因为听起来更强就盲目引入 multiprocessing」）
    subprocess    2  外部二进制（ffmpeg 等）
    browser       0  **默认关**：没启用浏览器环境时，要浏览器的任务直接被拒
    async         0  V0.1 不含异步执行格
    hls_segments 16  HLS 分片并发（**进程级**：多个 HLS 下载共享这一个额度）

「登记」与「限流」是两件事——这一点吃过亏（自有审计：「写好了没通电」）：
`subprocess_slots` 曾经只被登记，**从没被用来限流**，于是「外部二进制槽位」这件事
在代码里存在、在运行时不存在。所以本模块除了 `require()`（启动前的声明校验），
还提供 `gate()`（运行期的**并发槽位闸**）：容量真的会拦住人，峰值真的看得见。
"""

from __future__ import annotations

import logging
import threading

from daedalus.core.task import ResourceRequest

logger = logging.getLogger(__name__)

__all__ = ["ResourceDenied", "ResourceRegistry", "SlotGate", "DEFAULT_CAPACITIES",
           "DEFAULT_SLOT_WAIT"]

DEFAULT_CAPACITIES = {
    "network": 128,
    "thread": 20,
    "process": 0,
    "subprocess": 2,
    "browser": 0,
    "async": 0,
    "hls_segments": 16,
}

# 取槽位的**等待上限**（秒）：超时就明确失败。本工程不要「无限等待」这种失败形态——
# 卡住比报错难查得多（用户看到的只是「命令没反应」）。
DEFAULT_SLOT_WAIT = 30.0


class ResourceDenied(Exception):
    """资源缺省即拒绝（**不是**传输失败；记录原因后按「当前环境不可用」处理）。"""


class SlotGate:
    """并发槽位闸：**容量来自注册表**，取不到槽就明确失败（不静默无限等待）。

    三种失败原因（`acquire()` 的第二个返回值）互不混淆，调用方要能据此说清：
      * `not_registered` —— 这个科目压根没登记（连容量行都没有）；
      * `capacity_zero`  —— 登记了但容量是 0：**缺省即拒绝**（立刻失败，不排队）；
      * `timeout`        —— 有额度但被占满，等到上限就失败（**说清容量与等待时长**）。

    `peak` 记录实际峰值占用——门禁用它断言「实际并发 ≤ 登记容量」，
    让「登记值真的在管人」这件事**可读**（而不是靠读源码相信它）。
    """

    def __init__(self, name: str, capacity: int, *, registered: bool = True):
        self.name = str(name)
        self.capacity = max(0, int(capacity))
        self.registered = bool(registered)
        self._sem = threading.BoundedSemaphore(self.capacity) if self.capacity > 0 else None
        self._lock = threading.Lock()
        self.acquired = 0            # 当前占用
        self.peak = 0                # 峰值占用（门禁读数）
        self.granted = 0
        self.refused = 0
        self.timed_out = 0

    def acquire(self, timeout: float | None = None) -> tuple[bool, str]:
        """取一个槽。返回 `(是否拿到, 原因)`（拿到时原因为空串）。

        `timeout=None` 用 `DEFAULT_SLOT_WAIT`——**本工程不接受无上限等待**。
        """
        if not self.registered:
            with self._lock:
                self.refused += 1
            return False, "not_registered"
        if self.capacity <= 0:
            with self._lock:
                self.refused += 1
            return False, "capacity_zero"
        wait = DEFAULT_SLOT_WAIT if timeout is None else max(0.0, float(timeout))
        if not self._sem.acquire(timeout=wait):
            with self._lock:
                self.timed_out += 1
            return False, "timeout"
        with self._lock:
            self.acquired += 1
            self.granted += 1
            self.peak = max(self.peak, self.acquired)
        return True, ""

    def release(self) -> None:
        with self._lock:
            if self.acquired <= 0:
                return                      # 重复释放不炸（但也不放水）
            self.acquired -= 1
        self._sem.release()

    def stats(self) -> dict:
        with self._lock:
            return {"name": self.name, "capacity": self.capacity, "acquired": self.acquired,
                    "peak": self.peak, "granted": self.granted, "refused": self.refused,
                    "timed_out": self.timed_out}


class ResourceRegistry:
    """容量登记 + 任务资源声明校验（线程安全）。"""

    def __init__(self, capacities: dict | None = None):
        self._lock = threading.Lock()
        self._cap = dict(DEFAULT_CAPACITIES)
        self._gates: dict[str, SlotGate] = {}
        if capacities:
            self._cap.update({k: int(v) for k, v in capacities.items()})

    # ── 登记 ─────────────────────────────────────────────────────
    def register(self, name: str, capacity: int) -> None:
        """启用/调整一种执行资源的容量（启用浏览器环境时就会登记 `browser=1..N`）。

        容量一变就把该科目的槽位闸**丢掉重建**（见 `gate()`）——老闸上已经拿到的名额
        不会被追溯作废（容量变小时不该把正在跑的活掐掉）。
        """
        key = str(name)
        with self._lock:
            self._cap[key] = max(0, int(capacity))
            self._gates.pop(key, None)

    def capacity(self, name: str) -> int:
        with self._lock:
            return int(self._cap.get(name, 0))

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return str(name) in self._cap

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._cap)

    # ── 运行期限流（**登记值真的会拦住人**）───────────────────────
    def gate(self, name: str) -> SlotGate:
        """取某科目的槽位闸（按**当前**容量构造；容量变过就换新闸）。"""
        key = str(name)
        with self._lock:
            g = self._gates.get(key)
            registered = key in self._cap
            cap = int(self._cap.get(key, 0))
            if g is None or g.capacity != cap or g.registered != registered:
                g = SlotGate(key, cap, registered=registered)
                self._gates[key] = g
            return g

    def gates(self) -> dict:
        """已构造出来的闸快照（`{科目: stats}`）——观测面读它看「谁在占用」。"""
        with self._lock:
            return {k: g.stats() for k, g in self._gates.items()}

    # ── 校验 ─────────────────────────────────────────────────────
    def problems(self, res: ResourceRequest) -> list[str]:
        """返回「这条声明为什么不成立」的清单（空 = 通过）。

        只校验 `ResourceRequest` 里**有的**科目（任务声明面）。像 `hls_segments` 这种
        「环境内部的并发科目」（任务不会声明它、但会撞上它）不走 `require()`，
        而是走运行期的 `gate()`——两条路都指向**同一个容量表**，所以不会各说各话。
        """
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
        """把声明折成「账本上的数」（供观测与仲裁使用；**不**含业务细节）。"""
        return {"network": res.network, "browser": res.browser, "process": res.process,
                "subprocess": res.subprocess, "memory_mb": res.memory_mb,
                "cpu": res.cpu, "disk": res.disk, "domain_concurrency": res.domain_concurrency}

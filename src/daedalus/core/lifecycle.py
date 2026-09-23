# -*- coding: utf-8 -*-
"""生命周期：**在飞任务强引用** + **优雅关闭链**（清单 E9 / F 组 / L5）

两件事，都对应真实事故类型：

A) **在飞任务强引用**（E9）
   任务对象交给线程池/流水线后，如果代码里只剩一个 `Future`，一旦某处 `del` 掉、
   或者队列被 GC 掉最后一个引用，任务会**静默消失**（不报错、不落库、不重试——
   "跑着跑着少了一批"就是它）。所以：**每一个正在跑的任务，注册表里必须有一份强引用**。
   注册表还兼任"取消点"：关闭时标记取消，工作循环在**下一个安全点**退出
   （不 kill 线程——Python 里没有安全的线程取消，这条与 `exec/pools.py` 一致）。

B) **优雅关闭链**（关窗/Ctrl+C 走同一条路）
   顺序**写死**（顺序错了就有数据损失）：
       ① 停止接收新任务 → ② 按阶段顺序排空流水线 → ③ 冲刷写线程（批提交落库）
       → ④ 停采样器 → ⑤ 关数据库（WAL checkpoint）→ ⑥ 停看门狗
   三条纪律：
     * **每一步都执行**：前一步失败/超时**不阻断**后面的（否则"关不干净"）；
     * **每步有超时**：任何一步都不许把进程挂死（关窗卡住不动 = 用户强杀 = 真丢数据）；
     * **可重复调用**：第二次调用是空操作（信号可能来两次）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["InflightRegistry", "ShutdownChain", "ShutdownReport"]


class InflightRegistry:
    """在飞任务的**强引用**集合 + 取消点（线程安全）。

    * `add/remove`：进入与离开时调用（务必用 `try/finally`，否则表会越涨越大）；
    * `cancelled(id)`：工作循环每个安全点查一次；
    * `cancel_all()`：关闭时标记全部取消，返回被标记的 id 列表（**不等待、不 kill**）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tasks: dict[str, object] = {}       # 强引用：键在 → 对象活着
        self._cancelled: set[str] = set()
        self.added = 0
        self.removed = 0

    def add(self, task) -> None:
        tid = str(getattr(task, "task_id", "") or "")
        if not tid:
            return
        with self._lock:
            self._tasks[tid] = task               # 覆盖也没关系：同一个任务只有一次运行态
            self.added += 1
        METRICS.set("exec.inflight", len(self._tasks))

    def remove(self, task_id: str) -> None:
        with self._lock:
            if self._tasks.pop(str(task_id), None) is not None:
                self.removed += 1
            self._cancelled.discard(str(task_id))
        METRICS.set("exec.inflight", len(self._tasks))

    def get(self, task_id: str):
        with self._lock:
            return self._tasks.get(str(task_id))

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if str(task_id) not in self._tasks:
                return False
            self._cancelled.add(str(task_id))
            return True

    def cancel_all(self) -> list[str]:
        with self._lock:
            self._cancelled.update(self._tasks)
            return sorted(self._tasks)

    def cancelled(self, task_id: str) -> bool:
        with self._lock:
            return str(task_id) in self._cancelled

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._tasks)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def stats(self) -> dict:
        with self._lock:
            return {"inflight": len(self._tasks), "cancelled": len(self._cancelled),
                    "added": self.added, "removed": self.removed,
                    "ids": sorted(self._tasks)[:20]}


@dataclass
class ShutdownReport:
    """关闭报告：每步结果 + 线程数回落（L5 就查这个）。"""

    steps: list[dict] = field(default_factory=list)
    threads_before: int = 0
    threads_after: int = 0
    leaked_threads: list[str] = field(default_factory=list)
    seconds: float = 0.0
    ok: bool = True

    def add(self, name: str, ok: bool, seconds: float, note: str = "") -> None:
        self.steps.append({"name": name, "ok": bool(ok), "seconds": round(seconds, 4),
                           "note": note})
        self.ok = self.ok and bool(ok)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "seconds": round(self.seconds, 4),
                "threads_before": self.threads_before, "threads_after": self.threads_after,
                "leaked_threads": self.leaked_threads, "steps": self.steps}

    def describe(self) -> str:
        body = " → ".join(f"{s['name']}{'' if s['ok'] else '(失败)'}" for s in self.steps)
        return (f"{'成功' if self.ok else '有失败'} {len(self.steps)} 步：{body}；"
                f"线程 {self.threads_before} → {self.threads_after}"
                + (f"；残留 {self.leaked_threads}" if self.leaked_threads else ""))


class ShutdownChain:
    """按固定顺序关停（可重复调用；每步独立超时；前一步失败不阻断后面）。"""

    def __init__(self, *, inflight: InflightRegistry | None = None, logger_=None):
        self.inflight = inflight
        self._lock = threading.Lock()
        self._done = False
        self._log = logger_ or logger

    # 线程名黑名单：这些是本进程自己起的（关完就不该还在）
    _OWN_PREFIXES = ("dae-", "daedalus-")

    @staticmethod
    def _threads() -> list[str]:
        return [t.name for t in threading.enumerate()]

    def run(self, *, pipeline=None, pools=(), writer=None, sampler=None, db=None,
            watchdog=None, extra: list[tuple[str, object, float]] | None = None,
            cancel_first: bool = True) -> ShutdownReport:
        """执行关停链。所有参数都可选（有什么关什么），顺序**不随参数变化**。"""
        with self._lock:
            if self._done:
                rep = ShutdownReport()
                rep.add("already_closed", True, 0.0, "重复调用：空操作")
                rep.threads_before = rep.threads_after = len(self._threads())
                return rep
            self._done = True

        t_all = time.monotonic()
        rep = ShutdownReport()
        rep.threads_before = len(self._threads())

        # ① 取消在飞任务（不 kill：只标记，让工作循环在安全点退出）
        if cancel_first and self.inflight is not None:
            t0 = time.monotonic()
            ids = self.inflight.cancel_all()
            rep.add("cancel_inflight", True, time.monotonic() - t0,
                    f"标记取消 {len(ids)} 个在飞任务（不 kill 线程）")

        # ② 排空流水线（按阶段顺序，`Pipeline.stop` 内部已保证）
        if pipeline is not None:
            rep.add("pipeline_drain", *self._call(pipeline.stop, drain=True, timeout=30.0))

        # ③ 池 shut down（每个池自己等自己排空）
        for p in (pools or ()):
            name = str(getattr(p, "name", "pool"))
            rep.add(f"pool_shutdown:{name}", *self._call(p.shutdown, drain=True, timeout=30.0))

        # ④ 采样器
        if sampler is not None:
            rep.add("sampler_stop", *self._call(sampler.stop, timeout=3.0))

        # ⑤ 写线程冲刷（**必须在关库之前**：批提交里还有没落库的行）
        if writer is not None:
            rep.add("writer_flush", *self._call(writer.stop, drain=True, timeout=30.0))

        # ⑥ 关数据库（WAL checkpoint 在 `db.close` 内部做）
        if db is not None:
            rep.add("db_close", *self._call(db.close))

        # ⑦ 看门狗
        if watchdog is not None:
            rep.add("watchdog_stop", *self._call(watchdog.stop, timeout=5.0))

        # ⑧ 额外的自定义步骤（GUI 关窗要额外做的事情放这里）
        for name, fn, timeout in (extra or []):
            rep.add(str(name), *self._call(fn, timeout=timeout))

        rep.seconds = time.monotonic() - t_all
        time.sleep(0.05)                     # 给刚置了停止标志的线程一个退出的窗口
        rep.threads_after = len(self._threads())
        rep.leaked_threads = [n for n in self._threads()
                              if n.startswith(self._OWN_PREFIXES) and "sampler" not in n]
        METRICS.set("exec.threads_total", rep.threads_after)
        METRICS.inc("lifecycle.shutdown")
        self._log.info("关闭链完成：%s", rep.describe())
        return rep

    def _call(self, fn, **kw) -> tuple[bool, float, str]:
        """调一步：吞掉异常但**如实记录**（关闭过程不许把异常甩给调用方）。"""
        t0 = time.monotonic()
        try:
            out = fn(**kw)
            note = ""
            if isinstance(out, dict):
                note = str({k: out[k] for k in ("thread_alive", "queue_depth", "samples")
                            if k in out})[:120]
            return True, time.monotonic() - t0, note
        except TypeError:
            # 步骤函数签名不同（比如 db.close() 不收参数）→ 退一步裸调
            try:
                out = fn()
                return True, time.monotonic() - t0, str(out)[:80]
            except Exception as e:
                return False, time.monotonic() - t0, f"{type(e).__name__}: {e}"
        except Exception as e:
            return False, time.monotonic() - t0, f"{type(e).__name__}: {e}"

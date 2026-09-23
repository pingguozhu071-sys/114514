# -*- coding: utf-8 -*-
"""执行面：**线程池是一种执行资源**（不是"业务专家"）

四条不变量（每条都对应《02》里的坑）：
  1) **容量显式**：池大小来自配置/注册表（`core/registry.py`），不"看机器性能自动调"；
     资源声明不通过就**拒绝启动**（缺省即拒绝）。
  2) **有界提交**：提交队列有上限，满则**阻塞**（背压）。无界队列是 OOM 第一原因。
  3) **任务级看门狗**：`collect(fut, timeout=300s)` 超时 → **标记作废**并让线程自己退出；
     **绝不去 kill 线程**（Python 没有安全的线程取消）。作废任务的迟到结果会被丢弃。
  4) **优雅关闭顺序写死**：置停止标志 → 停止播种 → 池 `shutdown(wait=True, cancel_futures=False)`
     → 排空下游队列 → 落检查点（由调用方做）→ 关库。Ctrl+C 与关窗走同一条路径。

阶段流水线：`download → parse → store` 各自有界（默认 10k / 20k），本模块的 `Pipeline`
把这三段串起来，负责"背压 + 排空 + 幂等关闭"。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["BoundedQueue", "ManagedPool", "Pipeline", "PoolStats"]

_SENTINEL = object()


class BoundedQueue:
    """有界队列：满则阻塞（背压）。`maxsize` **必填**——本工程不允许无界队列。"""

    def __init__(self, name: str, maxsize: int):
        if int(maxsize) <= 0:
            raise ValueError(f"{name}: maxsize 必须为正（无界队列是 OOM 第一原因）")
        self.name = name
        self.maxsize = int(maxsize)
        self._q: "queue.Queue" = queue.Queue(maxsize=self.maxsize)
        self._closed = False
        self._peak = 0

    def put(self, item, timeout: float | None = None) -> bool:
        """放一个。**满则阻塞**（背压）；阻塞到超时返回 `False`（不抛异常）。
        关闭后拒绝放入。"""
        if self._closed:
            return False
        try:
            self._q.put(item, block=True, timeout=timeout)
        except queue.Full:
            METRICS.inc("queue.blocked_puts", name=self.name)
            return False                    # 背压：调用方自己决定重试还是丢弃
        d = self._q.qsize()
        if d > self._peak:
            self._peak = d
        METRICS.set("queue.depth", d, name=self.name)
        return True

    def get(self, timeout: float | None = None):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return _SENTINEL

    def task_done(self) -> None:
        try:
            self._q.task_done()
        except Exception:
            pass

    def join(self) -> None:
        self._q.join()

    def close(self) -> None:
        self._closed = True

    @property
    def depth(self) -> int:
        return self._q.qsize()

    @property
    def closed(self) -> bool:
        return self._closed

    def stats(self) -> dict:
        return {"name": self.name, "depth": self.depth, "maxsize": self.maxsize,
                "peak": self._peak, "closed": self._closed}


class PoolStats:
    def __init__(self):
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.timed_out = 0
        self.abandoned = 0
        self.saturated_waits = 0


class ManagedPool:
    """受管的线程池：容量显式 + 有界提交 + 看门狗 + 优雅关闭。"""

    def __init__(self, name: str, max_workers: int, *, registry=None,
                 capacity_name: str = "thread", queue_max: int = 10_000,
                 watchdog_timeout: float = 300.0):
        self.name = name
        self.max_workers = int(max_workers)
        self.watchdog_timeout = float(watchdog_timeout)
        self.capacity_name = capacity_name
        self.stats_obj = PoolStats()
        self._q = BoundedQueue(f"{name}-submit", queue_max)
        self._ex: ThreadPoolExecutor | None = None
        self._abandoned: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if registry is not None:
            registry.require(_dummy_request(capacity_name, self.max_workers))

    # ── 生命周期 ─────────────────────────────────────────────────
    def start(self) -> "ManagedPool":
        if self._ex is None:
            self._stop.clear()
            self._ex = ThreadPoolExecutor(max_workers=self.max_workers,
                                          thread_name_prefix=f"dae-{self.name}")
            METRICS.inc("exec.pool_started", name=self.name)
            METRICS.set("exec.threads", self.max_workers, name=self.name)
        return self

    def shutdown(self, drain: bool = True, timeout: float = 30.0) -> dict:
        """优雅关闭：等已提交的干完 → 停止收新活 → 关池 → 线程数回落。"""
        if self._ex is None:
            return self.stats()
        if drain:
            try:
                self._q.join()
            except Exception:
                pass
        self._q.close()
        self._stop.set()
        self._ex.shutdown(wait=True, cancel_futures=False)   # 不取消：正在跑的要跑完
        self._ex = None
        METRICS.inc("exec.pool_stopped", name=self.name)
        METRICS.set("exec.threads", 0, name=self.name)       # 线程数回落必须可见（L5 看这条）
        return self.stats()

    # ── 提交与收集 ───────────────────────────────────────────────
    def submit(self, fn, *args, task_id: str = "", timeout: float | None = 30.0, **kw):
        """提交一个任务。池满时**阻塞**（背压）；超时返回 `(None, 原因)`。"""
        if self._ex is None:
            self.start()
        if self._q.closed:
            return None, "池已关闭"
        try:
            self._q.put(None, timeout=timeout)               # 占位（背压信号）
        except queue.Full:
            self.stats_obj.saturated_waits += 1
            return None, "提交队列已满（背压生效）"
        try:
            fut = self._ex.submit(self._wrap, fn, args, kw, task_id)
        finally:
            self._q.task_done()
        self.stats_obj.submitted += 1
        return fut, "ok"

    def _wrap(self, fn, args, kw, task_id: str):
        try:
            out = fn(*args, **kw)
            with self._lock:
                if task_id and task_id in self._abandoned:
                    logger.warning("任务 %s 的结果迟到（已被看门狗作废）→ 丢弃", task_id)
                    self._abandoned.discard(task_id)
                    return None
            self.stats_obj.completed += 1
            METRICS.inc("exec.tasks_done", name=self.name)
            return out
        except Exception as e:
            self.stats_obj.failed += 1
            METRICS.inc("exec.tasks_failed", name=self.name)
            raise

    def collect(self, future: Future, timeout: float | None = None, task_id: str = "") -> tuple[str, object]:
        """等结果。返回 `("ok", 值)` / `("timeout", None)` / `("error", 异常)`。

        超时**不 kill 线程**：把任务标记作废（迟到结果会被丢弃），线程自己退出。
        """
        t = self.watchdog_timeout if timeout is None else float(timeout)
        try:
            return "ok", future.result(timeout=t)
        except TimeoutError:
            self.stats_obj.timed_out += 1
            if task_id:
                with self._lock:
                    self._abandoned.add(task_id)
            logger.warning("任务 %s 超时（%.0fs）→ 标记作废，**不强杀线程**", task_id or "?", t)
            return "timeout", None
        except Exception as e:
            return "error", e

    def stats(self) -> dict:
        s = self.stats_obj
        return {"name": self.name, "max_workers": self.max_workers,
                "submitted": s.submitted, "completed": s.completed, "failed": s.failed,
                "timed_out": s.timed_out, "abandoned_now": len(self._abandoned),
                "saturated_waits": s.saturated_waits,
                "queue": self._q.stats(), "watchdog_timeout": self.watchdog_timeout,
                "alive": self._ex is not None}


def _dummy_request(capacity_name: str, want: int):
    """把"池大小"翻译成一次资源声明，交给注册表校验（缺省即拒绝）。"""
    from daedalus.core.task import ResourceRequest
    if capacity_name == "browser":
        return ResourceRequest(network=0, browser=want)
    if capacity_name == "process":
        return ResourceRequest(network=0, process=want)
    if capacity_name == "subprocess":
        return ResourceRequest(network=0, subprocess=want)
    return ResourceRequest(network=want if capacity_name == "network" else 0,
                           **({} if capacity_name == "network" else {"cpu": "low"}))


class Pipeline:
    """阶段流水线：每段一个池 + 一个**有界**队列，段间背压。

    用法：`pipe = Pipeline(...); pipe.add_stage("download", pool, fn); ...; pipe.start(); pipe.feed(x)`
    关闭顺序：`pipe.stop(drain=True)`（先停播种 → 逐段排空 → 关池）。
    """

    def __init__(self, name: str = "pipeline", queue_after: tuple[int, ...] = (10_000, 20_000)):
        self.name = name
        self.queue_after = tuple(int(x) for x in queue_after)
        self._stages: list[dict] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._errors: list[str] = []

    def add_stage(self, name: str, pool: ManagedPool, fn) -> "Pipeline":
        """`fn(item)`：处理一条；返回 `None` 或"要交给下一段的条目"。"""
        self._stages.append({"name": name, "pool": pool, "fn": fn, "in": None, "out": None})
        return self

    def start(self) -> "Pipeline":
        # ① **先建齐所有入队**，再接线（否则上游的 out 会因为下游 in 还没建而变成 None，
        #    表现为"流水线跑完但下游一条都没收到"——本工程门禁 E5 抓到过）
        for i, st in enumerate(self._stages):
            cap = self.queue_after[i] if i < len(self.queue_after) else self.queue_after[-1]
            st["in"] = BoundedQueue(f"{st['name']}-in", cap)
        # ② 接线：每段的输出 = 下一段的输入
        for i, st in enumerate(self._stages):
            st["out"] = self._stages[i + 1]["in"] if i + 1 < len(self._stages) else None
        # ③ 起 worker
        for st in self._stages:
            st["pool"].start()
            for _ in range(st["pool"].max_workers):
                t = threading.Thread(target=self._worker, args=(st,),
                                     name=f"{self.name}-{st['name']}", daemon=True)
                t.start()
                self._threads.append(t)
        return self

    def _worker(self, st: dict) -> None:
        while not self._stop.is_set():
            item = st["in"].get(timeout=0.2)
            if item is _SENTINEL:
                if self._stop.is_set():
                    break
                continue
            try:
                out = st["fn"](item)
                if out is not None and st["out"] is not None:
                    st["out"].put(out)
            except Exception as e:
                self._errors.append(f"{st['name']}: {type(e).__name__}: {e}")
                logger.warning("阶段 %s 处理失败：%s", st["name"], e)
            finally:
                st["in"].task_done()

    def feed(self, item) -> bool:
        """往第一段投喂（满则阻塞 = 背压）。"""
        return self._stages[0]["in"].put(item)

    def stop(self, drain: bool = True, timeout: float = 30.0) -> dict:
        """优雅关闭：**逐段按序排空** → 停止收新活 → 关池。

        为什么要逐段按序：只等第一段是不够的——它的输出还在下游队列里，
        此时若就置停止标志，下游 worker 会立刻退出，**队列里剩下的条目就丢了**
        （表现为"偶发少几条"，最阴的一类 bug；本工程门禁 E5 抓到过）。
        """
        if drain:
            for st in self._stages:                      # 0 → 1 → 2 顺序 join
                try:
                    st["in"].join()
                except Exception:
                    pass
        self._stop.set()
        for st in self._stages:
            st["in"].close()
            for _ in range(st["pool"].max_workers):
                try:
                    st["in"].put(_SENTINEL, timeout=0.5)
                except Exception:
                    pass
        for t in self._threads:
            t.join(timeout=timeout)
        for st in self._stages:
            st["pool"].shutdown(drain=False)
        self._threads.clear()
        return self.stats()

    def stats(self) -> dict:
        return {"name": self.name,
                "stages": [dict(st["pool"].stats(), queue=st["in"].stats()) for st in self._stages],
                "errors": self._errors[-10:], "stopped": self._stop.is_set()}

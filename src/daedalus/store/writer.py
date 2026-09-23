# -*- coding: utf-8 -*-
"""单写线程 + 批提交（SQLite 的写是**串行资源**，多线程写只会抢锁）

设计要点（每条都对应一条踩过的坑）：
  1) **只有一个写线程**：业务线程通过 `submit(fn)` 提交"在事务里执行的函数"，拿到 `Future` 结果。
     提交队列**有界**（满了就阻塞 = 背压），不做无界堆积。
  2) **批提交**：攒够 `batch_rows` 条或到 `flush_interval` 就提交一次（不是每条都 commit）。
  3) **每个任务一个 SAVEPOINT**：批里某一条坏数据只回滚它自己，**不拖垮整批**
     （Kiana 的"一条溢出 → 整批回滚 → 数据静默丢失"就是这么来的）。
  4) **失败必留痕**：失败任务的 SQL 轨迹（trace 回调抓到，带参数值）落死信，**不许静默**。
  5) 所有 SQL 都是**字符串字面量**（本机安全策略的硬要求）；参数一律走绑定。

写路径的调用方约定：**产物行与状态变更要在同一个 `submit()` 事务里**——
这样"CAS 打卡在写产物之前"才是原子的（`frontier.commit_done` 就是这么做的）。
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field

from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["SingleWriter", "WriterStats", "Job"]


@dataclass
class Job:
    fn: object                      # Callable[[sqlite3.Connection], Any]
    label: str = ""
    future: Future = field(default_factory=Future)
    # `sync=True`：调用方**正阻塞等结果**（`run_now`：CAS 打卡、领取、交还都是这种）。
    # 这类作业不许被"攒批"拖延——攒批是为批量写优化吞吐的，不是为了给同步调用加 1 秒延迟。
    # （基准脚本把这暴露出来了：`flush_interval=1.0` 时每个任务白等最多 1 秒。）
    sync: bool = False


@dataclass
class WriterStats:
    jobs: int = 0
    batches: int = 0
    failed: int = 0
    last_batch_ms: float = 0.0
    queue_high_water: int = 0

    def snapshot(self) -> dict:
        return {"jobs": self.jobs, "batches": self.batches, "failed": self.failed,
                "last_batch_ms": round(self.last_batch_ms, 2),
                "queue_high_water": self.queue_high_water}


class SingleWriter:
    """单写线程。`submit()` 阻塞式入队（有界 = 背压），返回 `Future`。"""

    def __init__(self, db, dead_letter=None, batch_rows: int = 1000,
                 flush_interval: float = 1.0, queue_max: int = 20_000,
                 submit_timeout: float | None = 30.0):
        self.db = db
        self.dead_letter = dead_letter
        self.batch_rows = max(1, int(batch_rows))
        self.flush_interval = max(0.05, float(flush_interval))
        self.queue_max = max(1, int(queue_max))
        self.submit_timeout = submit_timeout
        self._q: "queue.Queue[Job | None]" = queue.Queue(maxsize=self.queue_max)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: sqlite3.Connection | None = None
        self._stats = WriterStats()
        self._crash: str = ""                      # 最近一次"意外崩"的记录（非空=要查）

    # ── 活着吗 ───────────────────────────────────────────────────
    def alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ── 生命周期 ─────────────────────────────────────────────────
    def start(self) -> "SingleWriter":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="daedalus-writer", daemon=True)
        self._thread.start()
        return self

    def stop(self, drain: bool = True, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        if drain:
            try:
                self._q.join()                     # 先把已提交的活干完
            except Exception:
                pass
        self._stop.set()
        try:
            self._q.put_nowait(None)               # 唤醒可能正在 wait 的循环
        except Exception:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None

    # ── 提交 ─────────────────────────────────────────────────────
    def submit(self, fn, label: str = "", *, sync: bool = False) -> Future:
        """把一个"在事务里执行的函数"交给写线程。队列满则**阻塞**（背压）。

        `sync=True` 表示调用方会立刻等结果（`run_now`）→ 攒批逻辑会**优先立刻提交**。
        """
        job = Job(fn=fn, label=label or getattr(fn, "__name__", "job"), sync=bool(sync))
        if not self.alive():
            if self._thread is None:
                # 从未启动、或已经被 stop()（属于关闭流程）→ 拒绝新写入并说清原因
                job.future.set_exception(RuntimeError(
                    "写线程未启动（或已关闭）：请先 `SingleWriter.start()`"))
                return job.future
            self.start()          # 线程崩过 → 这里拉起（`_run` 内部也会兜住，属双保险）
        try:
            self._q.put(job, timeout=self.submit_timeout)
        except queue.Full:
            METRICS.inc("queue.blocked_puts", name="writer")
            job.future.set_exception(RuntimeError(
                f"写队列已满（{self.queue_max}）——背压生效；调用方应稍后重试"))
            return job.future
        depth = self._q.qsize()
        if depth > self._stats.queue_high_water:
            self._stats.queue_high_water = depth
        METRICS.set("queue.depth", depth, name="writer")
        METRICS.inc("queue.submitted", label=job.label)
        return job.future

    def run_now(self, fn, label: str = "", timeout: float = 30.0):
        """同步提交并等结果（启动阶段/迁移/CAS 打卡/测试用；高频纯写入请用 `submit`）。

        **失败要快且要说得清**：写线程已死或超时，都抛带原因的 `RuntimeError`，
        而不是让调用方收到一个空消息的 `TimeoutError`（那等于没说）。
        """
        fut = self.submit(fn, label=label, sync=True)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError as e:
            raise RuntimeError(
                f"写线程 {timeout:.0f}s 未响应（label={label}，存活={self.alive()}，"
                f"队列深度={self._q.qsize()}，最近崩溃={self._crash or '无'}）") from e

    def stats(self) -> dict:
        return dict(self._stats.snapshot(), queue_depth=self._q.qsize(),
                    batch_rows=self.batch_rows, flush_interval=self.flush_interval)

    # ── 写循环 ───────────────────────────────────────────────────
    def _run(self) -> None:
        """写循环。**绝不静默死**：任何意外崩都记下来、留在 `_crash` 里、并让调用方**立刻知道**。

        为什么要有这层：写线程一死，引擎的每个写入都会干等到超时（默认 30s），表现为
        "整台机器没反应"——完全查不出原因。所以这里做两件事：
          1) 崩了**不退出循环**（重连一次继续跑；单批失败不影响后续）；
          2) 真到了活不下去的地步，`_crash` 有话说，`submit/run_now` 会**立即抛明确错误**
             而不是让调用方等 30 秒（S9 门禁跑出的现象就是"全部写入超时"）。
        """
        self._conn = self.db.connect()
        try:
            while not self._stop.is_set():
                try:
                    batch = self._collect()
                    if batch:
                        self._execute_batch(batch)
                except BaseException as e:                     # noqa: BLE001 - 兜住一切
                    self._crash = f"{type(e).__name__}: {e}"
                    logger.exception("写线程出现意外异常（已记录并继续）：%s", e)
                    if self.dead_letter is not None:
                        try:
                            import traceback as _tb
                            self.dead_letter.write("writer.crash", self._crash,
                                                   _tb.format_exc().splitlines()[-12:])
                        except Exception:
                            pass
                    self._reconnect()
                    time.sleep(0.05)                           # 避免崩→重试→崩的空转
        finally:
            try:
                if self._conn is not None:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _reconnect(self) -> None:
        """连接坏了就换一条（**不改停止标志、不丢队列**：队列里的活还在）。"""
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        try:
            self._conn = self.db.connect()
        except Exception as e:
            self._crash = f"重连失败：{type(e).__name__}: {e}"
            logger.error("写线程重连失败：%s", e)
            self._conn = None

    def _collect(self) -> list[Job]:
        """攒一批：先阻塞等第一个，再按 `batch_rows` 与 `flush_interval` 尽量多收。

        **同步作业立刻提交**（不攒批）：它在等结果，让它白等一秒等于把 CAS 打卡的
        延迟直接加在任务的关键路径上（基准脚本量出来的就是这个）。
        攒批只服务"反正不等结果"的异步作业。
        """
        batch: list[Job] = []
        try:
            first = self._q.get(timeout=0.2)
        except queue.Empty:
            return batch
        if first is None:
            self._q.task_done()
            return batch
        batch.append(first)
        if first.sync:
            batch.extend(self._drain_nowait())      # 顺手带上已经排好的（不额外等）
            return batch
        deadline = time.monotonic() + self.flush_interval
        while len(batch) < self.batch_rows:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                nxt = self._q.get_nowait() if timeout <= 0 else self._q.get(timeout=min(0.05, timeout))
            except queue.Empty:
                break
            if nxt is None:
                self._q.task_done()
                break
            batch.append(nxt)
            if nxt.sync:                            # 有人在等 → 别攒了，立刻提交这一批
                break
        return batch

    def _drain_nowait(self) -> list[Job]:
        """把队列里**已经就绪**的作业全取走（非阻塞），不等待新作业。"""
        out: list[Job] = []
        while len(out) < self.batch_rows:
            try:
                nxt = self._q.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                self._q.task_done()
                break
            out.append(nxt)
        return out

    def _execute_batch(self, batch: list[Job]) -> None:
        t0 = time.monotonic()
        conn = self._conn
        assert conn is not None
        trace: list[str] = []
        conn.set_trace_callback(trace.append)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for job in batch:
                    trace.clear()
                    conn.execute("SAVEPOINT job")
                    try:
                        result = job.fn(conn)
                        conn.execute("RELEASE job")
                        self._stats.jobs += 1
                        if not job.future.done():
                            job.future.set_result(result)
                    except Exception as e:
                        # 只回滚这一个任务（**不拖垮整批**），并把它写进死信
                        try:
                            conn.execute("ROLLBACK TO job")
                            conn.execute("RELEASE job")
                        except Exception:
                            pass
                        self._stats.failed += 1
                        METRICS.inc("db.job_failed", label=job.label)
                        if self.dead_letter is not None:
                            # **传自己的连接**：死信入库必须在同一个批事务里，
                            # 另开连接会撞锁并卡住 busy_timeout（门禁 C2 抓到过）
                            self.dead_letter.write(job.label, f"{type(e).__name__}: {e}",
                                                   list(trace), conn=conn)
                        if not job.future.done():
                            job.future.set_exception(e)
                conn.execute("COMMIT")
                self._stats.batches += 1
            except Exception as e:
                # 连 BEGIN/COMMIT 都出问题：整批作废并留痕（这时丢的是"批次"，但**有记录**）
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                for job in batch:
                    if not job.future.done():
                        job.future.set_exception(e)
                if self.dead_letter is not None:
                    self.dead_letter.write("writer.batch", f"{type(e).__name__}: {e}",
                                           list(trace), conn=conn)
                self._stats.failed += len(batch)
        finally:
            conn.set_trace_callback(None)
            for _ in batch:
                self._q.task_done()
            elapsed = time.monotonic() - t0
            self._stats.last_batch_ms = elapsed * 1000.0
            # 批提交的三个关键数：批大小、批耗时（p95 用得上）、累计行数
            METRICS.observe("db.flush", elapsed)
            METRICS.inc("db.batches")
            METRICS.inc("db.rows", len(batch))
            METRICS.set("queue.depth", self._q.qsize(), name="writer")

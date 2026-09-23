# -*- coding: utf-8 -*-
"""渲染调度：**防抖 + 忙时挂起 + 代数号守卫 + 主线程投递**

这是"性能红线"的实现部分（停顿 <200ms / 切页 <400ms / 最大化重排 <1200ms），
四条手法各自对应一类真实事故：

  1) **防抖 250ms**：拖窗/连点会瞬间产生几十次重排请求。每次重排都要重跑底图管线与重刷 QSS，
     不防抖就是"拖一下卡三秒"。
  2) **忙时挂起但不丢请求**：上一轮还没跑完就来新请求 → **记下"还有活儿"，跑完立刻补跑**，
     而不是丢掉（丢了会出现"最后停在了中间那种状态"）。
  3) **代数号守卫**：异步管线跑完时，世界可能已经变了（用户又换了图）。给每次请求发一个**代数号**，
     完成时对不上就**直接丢弃结果**——否则会出现"显示上一张图"这种看着像鬼影的 bug。
  4) **postEvent 投递**：工作线程**绝不碰 QPixmap**（Qt 的绘图资源只在主线程安全），
     结果通过事件投递回主线程再转 QPixmap/应用样式。

本模块的逻辑部分（`RenderScheduler`）**不依赖 Qt**，可以脱离事件循环单测；
Qt 侧的桥（`RenderBridge`）只负责"到点触发 + 投递"，非常薄。
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

__all__ = ["RenderScheduler", "RenderBridge", "RenderRequest"]


class RenderRequest:
    """一次渲染请求（代数号 + 载荷 + 时间戳）。"""

    __slots__ = ("gen", "payload", "at", "reason")

    def __init__(self, gen: int, payload, reason: str = ""):
        self.gen = int(gen)
        self.payload = payload
        self.reason = str(reason)
        self.at = time.monotonic()

    def __repr__(self) -> str:
        return f"RenderRequest(gen={self.gen}, reason={self.reason!r})"


class RenderScheduler:
    """纯逻辑的调度器（线程安全）。

    用法（主线程）：`request(payload)` → 到点后 `due()` 为真 → `take()` 取走 → 后台跑 →
    `complete(gen, result)` 为真才投递。
    """

    def __init__(self, *, debounce_ms: int = 250, now=time.monotonic):
        self.debounce_ms = max(0, int(debounce_ms))
        self._now = now
        self._lock = threading.Lock()
        self._gen = 0
        self._pending: RenderRequest | None = None
        self._deadline = 0.0
        self._busy = False
        self._deferred = False              # 忙时被挂起（不丢）
        self.dropped_by_generation = 0
        self.coalesced = 0                  # 被防抖合并掉的次数
        self.delivered = 0

    # ── 请求 ────────────────────────────────────────────────────
    def request(self, payload=None, *, reason: str = "") -> int:
        """提出一次渲染请求，返回它的代数号。**新的请求让旧的作废**。"""
        with self._lock:
            self._gen += 1
            if self._pending is not None:
                self.coalesced += 1                 # 防抖：合并
            self._pending = RenderRequest(self._gen, payload, reason)
            self._deadline = self._now() + self.debounce_ms / 1000.0
            return self._gen

    def due(self) -> bool:
        with self._lock:
            if self._pending is None or self._busy:
                return False
            return self._now() >= self._deadline

    def take(self) -> RenderRequest | None:
        """取走待跑的请求（取走后进入"忙"状态，直到 `finish_busy`）。"""
        with self._lock:
            if self._pending is None or self._busy or self._now() < self._deadline:
                return None
            req, self._pending = self._pending, None
            self._busy = True
            return req

    # ── 忙 / 完成 ───────────────────────────────────────────────
    def set_busy(self, busy: bool) -> None:
        with self._lock:
            self._busy = bool(busy)

    def finish_busy(self) -> bool:
        """一轮跑完。返回 True 表示**还有被挂起的活儿要补跑**（不丢请求）。"""
        with self._lock:
            self._busy = False
            again = self._pending is not None
            if again:
                self._deferred = False
                self._deadline = self._now()          # 补跑的不用再等防抖（已经等过了）
            return again

    def is_current(self, gen: int) -> bool:
        with self._lock:
            return int(gen) == self._gen

    def complete(self, gen: int, result, deliver=None):
        """一轮结果回来了。**代数号对不上就丢弃**（绝不用过期结果覆盖当前状态）。"""
        with self._lock:
            fresh = int(gen) == self._gen
            if not fresh:
                self.dropped_by_generation += 1
            else:
                self.delivered += 1
        if not fresh:
            return None
        if deliver is not None:
            return deliver(result)
        return result

    def stats(self) -> dict:
        with self._lock:
            return {"gen": self._gen, "pending": self._pending is not None,
                    "busy": self._busy, "coalesced": self.coalesced,
                    "dropped_by_generation": self.dropped_by_generation,
                    "delivered": self.delivered, "debounce_ms": self.debounce_ms}


class RenderBridge:
    """Qt 侧薄桥：定时检查 `due()/take()`，把结果用**事件**投回主线程。

    没有 Qt（或没有事件循环）时构造会失败——调用方应捕获并用纯逻辑调度器兜底
    （离线测试就是这么跑的）。
    """

    def __init__(self, parent=None, *, scheduler: RenderScheduler | None = None,
                 interval_ms: int = 50, worker=None):
        from PySide6.QtCore import QObject, QTimer
        self.scheduler = scheduler or RenderScheduler()
        self._worker = worker                     # `worker(payload) -> result`
        self._deliver = None

        class _Ready(QObject):
            """事件投递的载体：`QApplication.postEvent` 把「结果就绪」塞进主线程事件队列。

            为什么不用跨线程 `Signal`：信号那条路依赖元类型注册与连接类型推断，
            在"payload 里塞 dict/大图"的形态下偶发 arity/类型异常，而且**投递顺序**不如
            事件队列直观。`postEvent` 是它的等价替代，也是 Kiana 那边用血换来的写法
            （见其交接文档：「不要改回信号机制」）。
            """

            def __init__(self, owner, gen: int, result):
                super().__init__()
                self.owner, self.gen, self.result = owner, gen, result

            def event(self, ev) -> bool:
                self.owner._on_done(self.gen, self.result)     # noqa: SLF001
                return True

        self._ready_cls = _Ready
        self._host = parent if parent is not None else QObject()
        self._timer = QTimer(parent)
        self._timer.setInterval(max(16, int(interval_ms)))
        self._timer.timeout.connect(self._tick)

    def on_deliver(self, fn) -> None:
        self._deliver = fn

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def request(self, payload=None, *, reason: str = "") -> int:
        return self.scheduler.request(payload, reason=reason)

    def _tick(self) -> None:
        req = self.scheduler.take()
        if req is None:
            return
        if self._worker is None:
            self.scheduler.finish_busy()
            return
        payload = req.payload
        gen = req.gen

        def run():
            try:
                result = self._worker(payload)
            except Exception as e:                 # 工作线程异常：记下并交回主线程
                result = {"error": f"{type(e).__name__}: {e}"}
            # 跨线程只投递**纯数据**：QPixmap/QImage 一律在主线程里造。
            # 用 `postEvent` 而不是 `Signal`：事件队列的投递顺序确定、不依赖元类型推断。
            try:
                from PySide6.QtCore import QCoreApplication
                app = QCoreApplication.instance()
                if app is not None:
                    app.postEvent(self._host, self._ready_cls(self, gen, result))
                    return
            except Exception as e:
                logger.debug("postEvent 失败，退回直接投递：%s", e)
            self._on_done(gen, result)             # 没有事件循环（纯逻辑测试）时直接调

        threading.Thread(target=run, name="dae-render", daemon=True).start()

    def _on_done(self, gen: int, result) -> None:
        if self.scheduler.complete(gen, result, deliver=self._deliver) is not None:
            pass
        self.scheduler.finish_busy()

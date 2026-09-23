# -*- coding: utf-8 -*-
"""线程安全限速：令牌桶 + 每域并发上限 + Retry-After 处理（纯同步，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/rate_limiter.py` 与 `concurrency.py`
的语义，经《新工程开工包》**为线程模型重写**；Daedalus 内的改动：
  1) 导入示例改为包内路径；docs 措辞去掉"新工程红线"改为本工程条款；
  2) **域表加 LRU 上限**（原实现四个 dict 按域无上限增长 = 开放域集合下的慢性内存泄漏）；
  3) 新增 `snapshot()/restore()`：冷却与计数可**持久化**（重启后继续休息——
     Kiana 的冷却状态重启即忘，而库里有 `cooldowns` 表正好承接；接库在 S3 做）；
  4) 新增 `stats()` 供观测面使用。
  仍在 S3 做的：把它包成**所有环境共享的单一咽喉**（当前 `resumable`/`hls` 只过闸不限速）。

────────────────────────────────────────────────────────────────
这份代码对应本工程的哪些条款
    C4（合规/礼貌）：**按域限速 + 并发上限 + Retry-After 优先 + 被拦即降速**
    C5（稳定性）  ：限流不能变成重试风暴；`Retry-After` 必须有上限
    C2 原则（并发预算）：每域 QPS 1–2、并发 ≤8 这类数字，靠这里的组件落地

三条来自 Kiana 的教训
  1) **限流 ≠ 重试**：被限流的任务**不要**递增重试计数，要用独立计数兜底
     （否则任务永驻重试队列，"没有待处理"的退出条件永不成立）。
     本文件的 `DomainLimiter.note_throttled()` 就是给这个独立计数用的。
  2) **`Retry-After` 是服务器可控的值**：必须设上限（默认 300 秒），
     否则对方回一个 `Retry-After: 31536000` 就能把你的任务挂起一年。
  3) **礼貌延迟的预约要原子**：Kiana 曾"读上次时间 → 等待 → 写当前时间"，
     同域并发下被击穿（等于没有礼貌延迟）。正确做法是**在同一个锁内先占位再等待**——
     本文件的 `_reserve_slot()` 就是这个模式。

────────────────────────────────────────────────────────────────
用法

    from daedalus.core.rate_limiter import DomainLimiter

    limiter = DomainLimiter(per_domain_concurrency=5, per_domain_qps=1.0)

    with limiter.slot("example.com"):          # 阻塞直到：① 该域并发有名额 ② 礼貌间隔已到
        resp = safe_open(url, timeout=10)
        wait = limiter.retry_after_seconds(resp.headers)
        if wait is not None:
            limiter.note_throttled("example.com", wait)   # 独立计数 + 冷却
"""

from __future__ import annotations

import email.utils
import threading
import time

__all__ = ["TokenBucket", "DomainLimiter", "RETRY_AFTER_CAP"]

# Retry-After 上限（秒）：服务器可控值不得挂起任务数天
RETRY_AFTER_CAP = 300.0

# 域表上限：超过后按"最久未用"淘汰（防开放域集合下的慢性内存泄漏）
DEFAULT_MAX_DOMAINS = 4096


class TokenBucket:
    """经典令牌桶（线程安全）。`rate` = 每秒补充的令牌数，`burst` = 桶容量。"""

    def __init__(self, rate: float, burst: float | None = None):
        self.rate = max(0.0, float(rate))
        self.burst = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.burst
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """取令牌；不足则**按需睡眠**。timeout 为 None 表示一直等到够。"""
        tokens = max(0.0, float(tokens))
        deadline = None if timeout is None else (time.monotonic() + float(timeout))
        while True:
            with self._lock:
                now = time.monotonic()
                if self.rate > 0:
                    self._tokens = min(self.burst,
                                       self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                need = (tokens - self._tokens) / self.rate if self.rate > 0 else None
            if need is None:
                return False                       # rate=0：永不放行（显式关闭该域）
            if deadline is not None and (time.monotonic() + need) > deadline:
                return False
            time.sleep(min(need, 0.5))


class DomainLimiter:
    """按域的并发名额 + 礼貌间隔（原子预约）+ 限流冷却 + 独立计数。

    * `per_domain_concurrency`：同一域同时最多几个请求（Kiana 取值 5，本工程上限 8）
    * `per_domain_qps`        ：同一域的每秒请求上限（本工程默认 1–2，按站点 ToS 调）
    * `cooldown`              ：被限流后的冷却秒数（由 note_throttled 触发）
    * `max_domains`           ：域表上限，超出按最早预约时间淘汰（防内存慢性增长）
    """

    def __init__(self, per_domain_concurrency: int = 5, per_domain_qps: float = 1.0,
                 max_domains: int = DEFAULT_MAX_DOMAINS):
        self.max_conc = max(1, int(per_domain_concurrency))
        self.qps = max(0.0, float(per_domain_qps))
        self.max_domains = max(64, int(max_domains))
        self._lock = threading.Lock()
        self._sem: dict[str, threading.BoundedSemaphore] = {}
        self._next_ok: dict[str, float] = {}        # 该域"下次可发请求"的时间点
        self._cooldown_until: dict[str, float] = {}
        self._throttle_count: dict[str, int] = {}   # ← 独立计数，**不影响**重试次数

    # ── 内部：同一把锁内完成"占位"，避免被并发击穿 ──────────────────
    def _reserve_slot(self, domain: str) -> float:
        """预约一个发出时间点并返回需要等待的秒数（**原子**）。"""
        key = domain or ""
        with self._lock:
            self._evict_if_needed_locked()
            now = time.monotonic()
            start = max(self._next_ok.get(key, 0.0), self._cooldown_until.get(key, 0.0), now)
            gap = (1.0 / self.qps) if self.qps > 0 else 0.0
            self._next_ok[key] = start + gap
            return max(0.0, start - now)

    def _evict_if_needed_locked(self) -> None:
        """域表超限时，淘汰"最早预约"的域（调用方须持锁）。

        只淘汰**没有在冷却中**的域：正在休息的域必须保住冷却状态，否则"被拦即降速"会失效。
        """
        if len(self._next_ok) <= self.max_domains:
            return
        now = time.monotonic()
        candidates = [(k, t) for k, t in self._next_ok.items()
                      if self._cooldown_until.get(k, 0.0) <= now]
        candidates.sort(key=lambda kv: kv[1])
        for key, _ in candidates[: max(1, len(candidates) // 8)]:
            self._next_ok.pop(key, None)
            self._sem.pop(key, None)
            self._throttle_count.pop(key, None)
            self._cooldown_until.pop(key, None)

    class _Slot:
        def __init__(self, limiter: "DomainLimiter", domain: str):
            self._limiter = limiter
            self._domain = domain
            self._sem = None

        def __enter__(self):
            lim = self._limiter
            key = self._domain or ""
            with lim._lock:                                  # 名额表本身也要加锁
                sem = lim._sem.get(key)
                if sem is None:
                    sem = threading.BoundedSemaphore(lim.max_conc)
                    lim._sem[key] = sem
            self._sem = sem
            sem.acquire()                                    # ① 该域并发名额
            wait = lim._reserve_slot(self._domain)           # ② 礼貌间隔（原子预约）
            if wait > 0:
                time.sleep(wait)
            return self

        def __exit__(self, *exc):
            try:
                if self._sem is not None:
                    self._sem.release()                      # ③ 名额归还（异常路径也必须还）
            except Exception:
                pass
            return False

    def slot(self, domain: str):
        """上下文管理器：进入时阻塞直到"该域有名额且礼貌间隔已到"。"""
        return DomainLimiter._Slot(self, domain)

    # ── 被限流的处理（独立计数，**不要**混进重试次数）────────────────
    def note_throttled(self, domain: str, wait_seconds: float | None = None) -> int:
        """记录一次被限流：设置冷却 + 递增**独立**计数，返回当前连续计数。"""
        key = domain or ""
        wait = RETRY_AFTER_CAP if wait_seconds is None else max(0.0, min(float(wait_seconds), RETRY_AFTER_CAP))
        with self._lock:
            self._cooldown_until[key] = time.monotonic() + wait
            self._throttle_count[key] = self._throttle_count.get(key, 0) + 1
            return self._throttle_count[key]

    def note_success(self, domain: str) -> None:
        """一次成功就把该域的限流计数清零（连续计数语义）。"""
        with self._lock:
            self._throttle_count.pop(domain or "", None)

    def throttle_count(self, domain: str) -> int:
        with self._lock:
            return self._throttle_count.get(domain or "", 0)

    def is_resting(self, domain: str) -> bool:
        with self._lock:
            return self._cooldown_until.get(domain or "", 0.0) > time.monotonic()

    # ── 冷却/计数的持久化接口（S3 接 DB 的 `cooldowns` 表）────────────
    def snapshot(self) -> dict:
        """导出"还需休息多久"（相对秒），供落库/落盘。"""
        with self._lock:
            now = time.monotonic()
            return {
                k: {"rest_for": max(0.0, until - now), "throttle_count": self._throttle_count.get(k, 0)}
                for k, until in self._cooldown_until.items() if until > now
            }

    def restore(self, state: dict) -> int:
        """从 `snapshot()` 的形态恢复冷却（重启后继续休息）。返回恢复条数。"""
        n = 0
        with self._lock:
            now = time.monotonic()
            for k, v in (state or {}).items():
                try:
                    rest = float(v.get("rest_for", 0.0))
                    if rest <= 0:
                        continue
                    self._cooldown_until[str(k)] = now + min(rest, RETRY_AFTER_CAP)
                    self._throttle_count[str(k)] = int(v.get("throttle_count", 0) or 0)
                    n += 1
                except Exception:
                    continue
        return n

    def stats(self) -> dict:
        """只读快照，供观测面（obs/metrics）使用。"""
        with self._lock:
            now = time.monotonic()
            return {
                "domains": len(self._next_ok),
                "max_domains": self.max_domains,
                "resting": sum(1 for t in self._cooldown_until.values() if t > now),
                "max_conc": self.max_conc,
                "qps": self.qps,
            }

    # ── Retry-After 解析（带上限）──────────────────────────────────
    @staticmethod
    def retry_after_seconds(headers) -> float | None:
        """从响应头解析 Retry-After（秒数或 HTTP 日期），**钳到 RETRY_AFTER_CAP**。

        返回 None 表示响应头里没有该字段。
        """
        try:
            raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
            if not raw:
                return None
            raw = str(raw).strip()
            if raw.isdigit():
                return min(float(raw), RETRY_AFTER_CAP)
            dt = email.utils.parsedate_to_datetime(raw)      # HTTP 日期形式
            if dt is None:
                return None
            delta = dt.timestamp() - time.time()
            return max(0.0, min(delta, RETRY_AFTER_CAP))
        except Exception:
            return None

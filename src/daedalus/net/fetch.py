# -*- coding: utf-8 -*-
"""外网取流的**唯一咽喉**：闸 → robots → 礼貌预算 → 响应语义，全部在一处收口。

为什么要有这一层：Kiana 的真实事故是"某下载通道只过了闸、没接限速"，于是"礼貌"形同虚设；
后来又在别处出现"绕过入口校验把内网地址交给下载器"。**只要还有第二条出网路径，闸和限速就不成立。**
所以：**任何出网动作都必须经由 `Fetcher.open()`**（脚本 `tests/gates/s1_gate.py` 会扫描全仓裸请求）。

顺序（顺序本身就是设计）：
    1) **闸**（`ssrf_gate.safe_open`：协议 + 私网 + 逐跳复检）——在最前面，先拒掉不该去的地方；
    2) **robots**（按域缓存）——规则不允许就抛 `RobotsDenied`（不可重试，记录后跳过）；
    3) **礼貌预算**（`DomainLimiter`：该域并发名额 + 原子预约的礼貌间隔；可用 `Crawl-delay` 收紧）；
    4) **429/503 语义**：抛 `Throttled`，并**记到独立计数**（`note_throttled`）——
       **限流 ≠ 重试**：调用方**不得**把这种情况计入重试次数，否则"没有待处理"的退出条件永不成立。

身份标识：默认带一个**诚实**的 UA（说明是本工具、用于个人采集），不伪装浏览器；
**不发 `Accept-Encoding`**——urllib 不会自动解压，发了就会拿到一段读不懂的字节流（这是个真坑）。
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from daedalus.net.robots import RobotsCache, RobotsDenied
from daedalus.net.ssrf_gate import BlockedError, RedirectLoopError, safe_open
from daedalus.obs.metrics import METRICS

__all__ = ["Fetcher", "Throttled", "RobotsDenied", "BlockedError"]

logger = logging.getLogger(__name__)


class Throttled(Exception):
    """被限流（429/503）。**不计入重试次数**，按"独立计数 + 退避"处理。"""

    def __init__(self, status: int, wait: float | None = None, url: str = ""):
        self.status = int(status)
        self.wait = wait
        self.url = url
        super().__init__(f"被限流 HTTP {status}（Retry-After={wait}）: {str(url)[:100]}")


class Fetcher:
    """出网唯一咽喉。构造一次，全局复用（限速状态与 robots 缓存都在它身上）。"""

    def __init__(self, *, limiter=None, robots: RobotsCache | None = None,
                 opener=safe_open, user_agent: str | None = None,
                 respect_robots: bool = True, timeout: float = 15.0,
                 extra_headers: dict | None = None):
        self._limiter = limiter
        self._opener = opener
        self._respect_robots = bool(respect_robots)
        self._timeout = float(timeout)
        self._extra_headers = dict(extra_headers or {})
        self._ua = user_agent or self._default_ua()
        # robots 缓存需要一个"能出网的东西"，但它自己只能通过本对象取——见下方 `open` 的
        # `skip_robots` 与重入守卫：**取 robots.txt 这件事本身不能再过一遍 robots**。
        self._robots = robots
        self._calls = 0
        self._tls = threading.local()      # 标记"当前线程正在取 robots.txt"

    @staticmethod
    def _default_ua() -> str:
        try:
            from daedalus import VERSION
        except Exception:                       # pragma: no cover
            VERSION = "0"
        return f"Daedalus/{VERSION} (personal data collector; respects robots.txt)"

    # ── 出网唯一入口 ──────────────────────────────────────────────
    def open(self, url: str, method: str = "GET", headers: dict | None = None,
             timeout: float | None = None, *, skip_robots: bool = False):
        """取流。`skip_robots=True` **只给 robots 缓存自己用**（见下）。

        ⚠️ 这一条是 S9 用 CLI 端到端跑出来的**严重 bug**：`RobotsCache._fetch` 调的是本方法，
        而本方法又先查 robots——于是"取 robots.txt"这个请求自己又要过 robots，形成**纯递归的
        规则查询**（一个网络包都没发出去），最终每个首次访问的域都被判成"完全禁止"。
        两个守卫：
          1) `skip_robots=True`：robots 缓存显式声明"我在取规则，别再查规则"；
          2) `self._tls.in_robots` 重入守卫：**即使有人忘了传**，正在取 robots 的线程也不会再递归
             （防御性——这类"自己吃自己"的循环必须由机制挡住，不能靠调用方记得）。
        注意：跳过的只是**robots 规则**这一层；SSRF 闸与限速**照旧生效**（唯一咽喉没破）。
        """
        u = str(url or "")
        host = self._host_of(u)
        in_robots = bool(getattr(self._tls, "in_robots", False))

        # ① robots（规则层；不可重试）
        if self._respect_robots and self._robots is not None and not skip_robots and not in_robots:
            try:
                self._robots.check(u)
            except RobotsDenied:
                METRICS.inc("net.robots_denied")
                raise

        hdrs = {"User-Agent": self._ua}
        hdrs.update(self._extra_headers)
        hdrs.update(headers or {})

        # ② 礼貌预算（名额 + 原子预约的间隔）
        limiter = self._limiter
        if limiter is None:
            return self._do_open(u, method, hdrs, timeout)
        with limiter.slot(host):
            return self._do_open(u, method, hdrs, timeout, host=host)

    def open_for_robots(self, url: str, *, timeout: float | None = None):
        """**给 robots 缓存专用**的取流口（过闸、过限速，但不再查 robots 规则）。"""
        self._tls.in_robots = True
        try:
            return self.open(url, timeout=timeout, skip_robots=True)
        finally:
            self._tls.in_robots = False

    def _do_open(self, url: str, method: str, headers: dict, timeout: float | None, host: str = ""):
        self._calls += 1
        METRICS.inc("net.requests")
        t0 = time.monotonic()
        try:
            resp = self._opener(url, method=method, headers=headers,
                                timeout=timeout if timeout is not None else self._timeout)
        except BlockedError:
            # 被 SSRF 闸拦下：**不可重试**，且必须在指标里看得见（不然"为什么全失败"无从查起）
            METRICS.inc("net.blocked")
            raise
        finally:
            METRICS.observe("net.connect", time.monotonic() - t0)
        status = int(getattr(resp, "status", 0) or 0)
        if status in (429, 503):
            wait = None
            if self._limiter is not None:
                wait = self._limiter.retry_after_seconds(getattr(resp, "headers", {}) or {})
                self._limiter.note_throttled(host, wait)     # 独立计数（**不是**重试计数）
            METRICS.inc("net.throttled")
            try:
                resp.close()
            except Exception:
                pass
            raise Throttled(status, wait, url)
        if self._limiter is not None and host:
            self._limiter.note_success(host)
        return resp

    # ── 只判定不取流（给调度/预览用）────────────────────────────────
    def is_allowed(self, url: str) -> tuple[bool, str]:
        """返回 (是否允许, 原因)。用于入队前拦截，避免把注定失败的 URL 排进前沿。"""
        u = str(url or "")
        if not u.startswith(("http://", "https://")):
            return False, "协议非法"
        from daedalus.net.ssrf_gate import is_private_url
        if is_private_url(u):
            return False, "SSRF 闸拦截（私网/保留地址）"
        if self._respect_robots and self._robots is not None and not self._robots.allowed(u):
            return False, "robots.txt 不允许"
        return True, "ok"

    def crawl_delay(self, url: str) -> float | None:
        """该域声明的 Crawl-delay（非标准扩展，交给限速策略决定是否采用）。"""
        return self._robots.crawl_delay(url) if self._robots is not None else None

    @staticmethod
    def _host_of(url: str) -> str:
        try:
            return (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
        except Exception:
            return ""

    def stats(self) -> dict:
        return {"calls": self._calls, "respect_robots": self._respect_robots,
                "limiter": self._limiter.stats() if self._limiter is not None else None,
                "robots": self._robots.stats() if self._robots is not None else None}


def build_fetcher(user_agent: str | None = None, per_domain_concurrency: int = 5,
                  per_domain_qps: float = 1.0, respect_robots: bool = True,
                  extra_headers: dict | None = None) -> Fetcher:
    """按工程默认值组装一个咽喉（限速 + robots 缓存 + 闸）。"""
    from daedalus.core.rate_limiter import DomainLimiter
    limiter = DomainLimiter(per_domain_concurrency=per_domain_concurrency,
                            per_domain_qps=per_domain_qps)
    f = Fetcher(limiter=limiter, respect_robots=respect_robots,
                user_agent=user_agent, extra_headers=extra_headers)
    f._robots = RobotsCache(fetcher=f, user_agent="*")       # 打破循环：robots 用同一个咽喉取
    return f

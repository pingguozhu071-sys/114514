# -*- coding: utf-8 -*-
"""环境①直连网络：一次"取一个 URL"该有的全部规矩

它**不是**一个新的出网通道——真正的出网只在 `net/fetch.py`（唯一咽喉：闸 + robots + 礼貌预算）。
本模块负责咽喉之上的那层"取流业务规矩"：

  1) **超时**：连接 5s / 读取 15s（默认，可配）；
  2) **重试**：瞬时失败重试（默认 3 次），退避 = `min(上限, 基数·2^n) × 抖动(1±jitter)`；
     —— **被限流不在这里重试**：`Throttled` 直接上抛信号，交给任务层按"独立计数 + 退避"处理
        （限流 ≠ 重试，这条在本工程是硬规矩）；
  3) **响应体上限 5MB**：超过就**打标** `too_big` 并跳过解析（内存峰值可控）；
  4) **编码探测顺序**：响应头 charset → BOM → HTML meta → 兜底 utf-8（并说明"凭什么这么判"）；
  5) **缓存 + 条件请求**：有缓存就带上 `If-None-Match`/`If-Modified-Since`；`304` → **复用旧内容**并刷新时间；
  6) **Cookie**：从加密 jar 取；取不到时给出**可读原因**（不许静默变成一次莫名其妙的 403）；
  7) **代理只走环境变量**（`HTTP_PROXY`/`HTTPS_PROXY`）——不提供"随便填个代理"的接口；
  8) 每次取流都**产出证据**（状态码/头/体积/是否命中缓存/是否 304），供路由使用。
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

from daedalus.core.evidence import Evidence, from_decision, from_response
from daedalus.net.fetch import BlockedError, RobotsDenied, Throttled
from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["NetEnvironment", "NetResult", "DEFAULT_TIMEOUT", "DEFAULT_MAX_BODY"]

DEFAULT_TIMEOUT = (5.0, 15.0)        # (连接, 读取)
DEFAULT_MAX_BODY = 5 << 20           # 5MB：超过即打标 too_big、不进解析
DEFAULT_RETRIES = 3
BACKOFF_BASE, BACKOFF_CAP, JITTER = 0.5, 8.0, 0.5

_CHARSET_HEADER_RE = re.compile(r"charset\s*=\s*['\"]?([\w\-]+)", re.IGNORECASE)
_CHARSET_META_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*['"]?([\w\-]+)""", re.IGNORECASE)
_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


@dataclass
class NetResult:
    ok: bool
    status: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    final_url: str = ""
    encoding: str = ""
    encoding_how: str = ""
    from_cache: bool = False
    not_modified: bool = False
    too_big: bool = False
    truncated: bool = False
    attempts: int = 1
    throttled: bool = False
    retry_after: float | None = None
    blocked: bool = False
    robots_denied: bool = False
    error: str = ""
    elapsed: float = 0.0
    cookie_note: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.body)


class NetEnvironment:
    """直连网络环境（拿 URL → NetResult）。"""

    def __init__(self, fetcher, cache=None, cookies=None, *, max_body: int = DEFAULT_MAX_BODY,
                 timeout: tuple[float, float] = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES,
                 backoff_base: float = BACKOFF_BASE, backoff_cap: float = BACKOFF_CAP,
                 jitter: float = JITTER, sleep=time.sleep):
        self.fetcher = fetcher
        self.cache = cache
        self.cookies = cookies
        self.max_body = int(max_body)
        self.timeout = (float(timeout[0]), float(timeout[1]))
        self.retries = int(retries)
        self.backoff_base = float(backoff_base)
        self.backoff_cap = float(backoff_cap)
        self.jitter = float(jitter)
        self._sleep = sleep                     # 注入点：门禁里不需要真睡

    # ── 代理与身份 ───────────────────────────────────────────────
    @staticmethod
    def proxy_from_env() -> dict:
        """代理**只**从环境变量读（`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`）。"""
        return {k: v for k, v in ((k, os.environ.get(k)) for k in
                                 ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                                  "http_proxy", "https_proxy", "all_proxy")) if v}

    # ── 主入口（指标包一层，不改内部逻辑）──────────────────────────
    def get(self, url: str, *, headers: dict | None = None, method: str = "GET",
            conditional: bool = True, max_body: int | None = None) -> NetResult:
        res = self._get_inner(url, headers=headers, method=method,
                              conditional=conditional, max_body=max_body)
        # 观测挂在这里而不是散在各 return 点上：**只写一次，覆盖所有出口**
        METRICS.inc("net.requests_env")
        METRICS.observe("net.latency", res.elapsed)
        if res.size:
            METRICS.inc("net.bytes", res.size)
            METRICS.count_throughput(bytes_added=res.size)
        if res.from_cache:
            METRICS.inc("net.cache_hits")
        if res.not_modified:
            METRICS.inc("net.not_modified")
        if res.too_big:
            METRICS.inc("net.too_big")
        if res.throttled:
            METRICS.inc("net.throttled_env")
        if res.blocked:
            METRICS.inc("net.blocked_env")
        if res.robots_denied:
            METRICS.inc("net.robots_denied_env")
        if not res.ok and not (res.throttled or res.blocked or res.robots_denied):
            METRICS.inc("net.failed")
        return res

    def _get_inner(self, url: str, *, headers: dict | None = None, method: str = "GET",
                   conditional: bool = True, max_body: int | None = None) -> NetResult:
        t0 = time.monotonic()
        cap = int(max_body if max_body is not None else self.max_body)
        hdrs = dict(headers or {})
        entry = self.cache.get(url) if (self.cache is not None and conditional) else None

        # 未过期且配置了 TTL → 直接命中（默认 ttl=0：一律走条件请求，确保"未变"有服务端确认）
        if entry is not None and self.cache is not None and not entry.stale(self.cache.ttl):
            return NetResult(ok=True, status=entry.status, body=entry.body, headers=entry.headers,
                             final_url=url, from_cache=True,
                             elapsed=time.monotonic() - t0,
                             evidence=[from_decision("ok", "cache_hit",
                                                     "缓存未过期，直接复用", stage="direct")])
        if entry is not None and conditional:
            hdrs.update(entry.conditional_headers())

        # Cookie（运行态钥匙：原样发送）
        cookie_note = ""
        if self.cookies is not None and "Cookie" not in hdrs:
            cookie_header = self.cookies.header_for(url)
            if cookie_header:
                hdrs["Cookie"] = cookie_header
            elif len(self.cookies) > 0:
                cookie_note = self.cookies.missing_reason(url)
                logger.info("Cookie 不可用：%s", cookie_note)

        attempt, last_error = 0, ""
        while attempt < max(1, self.retries):
            attempt += 1
            try:
                resp = self.fetcher.open(url, method=method, headers=hdrs, timeout=self.timeout[1])
                try:
                    status = int(getattr(resp, "status", 0) or 0)
                    rheaders = {str(k).lower(): v for k, v in (getattr(resp, "headers", {}) or {}).items()}
                    # ① 304：复用缓存（"未变"是一等结果）
                    if status == 304 and entry is not None and self.cache is not None:
                        self.cache.touch(url, rheaders)
                        return NetResult(ok=True, status=entry.status, body=entry.body,
                                         headers=entry.headers, final_url=url, from_cache=True,
                                         not_modified=True, attempts=attempt,
                                         elapsed=time.monotonic() - t0,
                                         evidence=[from_decision("ok", "not_modified",
                                                                 "服务端 304：内容未变，复用缓存",
                                                                 stage="direct")])
                    body, truncated = self._read_capped(resp, cap)
                    ev: list[Evidence] = [from_response(status, rheaders, size=len(body),
                                                        final_url=getattr(resp, "url", url) or url,
                                                        stage="direct", decision="capture")]
                    too_big = len(body) >= cap and truncated
                    if too_big:
                        ev.append(from_decision("large_object", "skip_parse",
                                                f"响应体超过上限（{cap} 字节）→ 打标 too_big，跳过解析",
                                                stage="direct", cap=cap, size=len(body)))
                    enc, how = self.decode_encoding(body, rheaders)
                    if self.cache is not None and 200 <= status < 300 and not too_big:
                        self.cache.store(url, status, rheaders, body)
                    return NetResult(ok=200 <= status < 300, status=status, body=body,
                                     headers=rheaders, final_url=getattr(resp, "url", url) or url,
                                     encoding=enc, encoding_how=how, too_big=too_big,
                                     truncated=truncated, attempts=attempt,
                                     elapsed=time.monotonic() - t0, cookie_note=cookie_note,
                                     evidence=ev)
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
            except Throttled as e:
                # **不在这里重试**：交给任务层（独立计数 + 退避）
                return NetResult(ok=False, throttled=True, retry_after=e.wait, status=e.status,
                                 error=f"被限流：{e}", attempts=attempt,
                                 elapsed=time.monotonic() - t0, cookie_note=cookie_note,
                                 evidence=[from_decision("throttled", "backoff",
                                                         f"被限流 HTTP {e.status}"
                                                         f"（Retry-After={e.wait}）",
                                                         stage="direct")])
            except RobotsDenied as e:
                return NetResult(ok=False, robots_denied=True, error=f"robots 不允许：{e}",
                                 attempts=attempt, elapsed=time.monotonic() - t0,
                                 evidence=[from_decision("policy_denied", "stop",
                                                         f"robots 不允许：{e}", stage="direct")])
            except BlockedError as e:
                return NetResult(ok=False, blocked=True, error=f"被闸拦下：{e}", attempts=attempt,
                                 elapsed=time.monotonic() - t0,
                                 evidence=[from_decision("policy_denied", "stop",
                                                         f"SSRF 闸拦截：{e}", stage="direct")])
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt >= max(1, self.retries):
                    break
                delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
                # 抖动用**普通随机**是刻意的（安全扫描会提示"随机数不安全"）：
                # 这里要的是"错开重试时刻"，不是密码学随机；用 secrets 反而浪费系统熵。
                delay *= 1.0 + random.uniform(-self.jitter, self.jitter)
                logger.info("第 %d 次失败（%s），%.2fs 后重试", attempt, type(e).__name__, delay)
                self._sleep(max(0.0, delay))
        return NetResult(ok=False, error=f"重试 {attempt} 次后仍失败：{last_error}",
                         attempts=attempt, elapsed=time.monotonic() - t0, cookie_note=cookie_note,
                         evidence=[from_decision("transient_failure", "retry",
                                                 f"瞬时失败：{last_error}", stage="direct")])

    # ── 读体（带上限）─────────────────────────────────────────────
    def _read_capped(self, resp, cap: int, chunk: int = 64 * 1024) -> tuple[bytes, bool]:
        buf = bytearray()
        while len(buf) <= cap:
            blk = resp.read(chunk)
            if not blk:
                return bytes(buf), False
            buf.extend(blk)
        return bytes(buf[:cap]), True          # 截断（≥cap 就算超限）

    # ── 编码探测（顺序写死）───────────────────────────────────────
    def decode_encoding(self, body: bytes, headers: dict) -> tuple[str, str]:
        """返回 `(编码, 凭什么这么判)`。顺序：响应头 → BOM → HTML meta → 兜底 utf-8。"""
        ctype = str((headers or {}).get("content-type") or "")
        m = _CHARSET_HEADER_RE.search(ctype)
        if m:
            return m.group(1).lower(), "响应头 charset"
        for bom, enc in _BOMS:
            if body.startswith(bom):
                return enc, "BOM"
        m2 = _CHARSET_META_RE.search((body or b"")[:4096])
        if m2:
            return m2.group(1).decode("ascii", "ignore").lower(), "HTML meta charset"
        return "utf-8", "兜底"

    def decode(self, result: NetResult) -> tuple[str, str]:
        """按探测结果解码成文本（解不出就 replace——**不抛异常**）。"""
        enc = result.encoding or "utf-8"
        try:
            return result.body.decode(enc, "replace"), enc
        except LookupError:
            return result.body.decode("utf-8", "replace"), "utf-8（未知编码，兜底）"

    def stats(self) -> dict:
        return {"proxies_from_env": sorted(self.proxy_from_env().keys()),
                "max_body": self.max_body, "timeout": self.timeout, "retries": self.retries,
                "cache": self.cache.stats() if self.cache is not None else None,
                "fetcher": self.fetcher.stats() if self.fetcher is not None else None}

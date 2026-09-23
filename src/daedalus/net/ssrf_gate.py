# -*- coding: utf-8 -*-
"""SSRF 取流闸（线程模型就绪的同步实现，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/url_utils.py`，经《新工程开工包》重写为同步版；
本文件在 Daedalus 内的移植改动（其余保持原样，注释里的坑全部保留）：
  1) 判定缓存加锁 + 容量超限按"最旧优先"淘汰（原实现是无锁 dict、满了整体 clear）；
  2) DNS 解析失败的放行/拒绝改为**可配置**（`set_dns_fail_open`），默认保持放行（防误伤）；
  3) `BlockedError` / `RedirectLoopError` 的语义与错误分类对接放在调用侧（见 core/router，S6）。

────────────────────────────────────────────────────────────────
为什么必须有它（Kiana 上**实测复现过**的三类事故）
  1) 只在入口校验不够：
     页面里的 <img src="http://attacker/a.jpg"> 回 302 指向 169.254.169.254，
     底层 HTTP 库默认自动跟随重定向 → **内网返回的正文被当作图片正常落盘**。
  2) 私网判定不完整：
     标准库 `ip.is_private` **不含** CGNAT(100.64.0.0/10) 与 6to4 中继段(192.88.99.0/24)；
     IP 还写字面量变体：2130706433 / 0x7f000001 / 127.1 / 0177.0.0.1 —— 都要按 IP 语义判。
  3) 判定结果永久缓存 → DNS rebinding：
     域名先解析到公网（缓存"放行"），随后改指内网，缓存仍放行 → 闸失效。
     **判定结果必须带 TTL**（本实现默认 90 秒）。

────────────────────────────────────────────────────────────────
用法（在 Daedalus 里，它应当是**唯一**外网取流入口）

    from daedalus.net.ssrf_gate import safe_open, BlockedError

    try:
        with safe_open(url, timeout=10) as resp:
            data = resp.read()
    except BlockedError as e:      # 被闸拦下 → 不可重试，记录并跳过
        ...
    except Exception as e:         # 传输失败 → **应该重试**
        ...

语义约定（照抄，别改）
    * BlockedError          = 被闸拦下 / 协议非法      → 不可重试
    * RedirectLoopError     = 重定向超过上限           → **可重试**（Kiana 曾把它静默当 3xx 返回，
                                                           结果任务永久失效且零错误记录）
    * 其它异常               = 网络/传输问题            → 可重试
    * 绝不把传输异常吞成"被拦"：那会让一次网络抖动被当成永久失败（Kiana 踩过）。
"""

from __future__ import annotations

import ipaddress
import logging
import re as _re
import socket as _socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

__all__ = [
    "BlockedError", "RedirectLoopError", "is_private_url", "safe_open",
    "safe_filename", "safe_dirname", "set_dns_fail_open", "cache_stats",
]

# ── 私网判定的补充段 ───────────────────────────────────────────────
# Python 的 ip.is_private 不覆盖这两段，必须显式判（Kiana 曾被外部评估专门点出漏 CGNAT）
_EXTRA_BLOCKED_V4 = (
    (1681915904, 1686110207),   # 100.64.0.0/10   CGNAT（RFC 6598，运营商级 NAT）
    (3227017984, 3227018239),   # 192.88.99.0/24  6to4 中继（RFC 7526 已废弃，常用作隧道绕过）
)

# 判定缓存的 TTL（秒）。**不要设成无限**——见文件头第 3 条事故。
_HOST_CACHE_TTL = 90.0
_HOST_CACHE_MAX = 2048
_HOST_CACHE: dict[str, tuple[bool, float]] = {}

# 移植改动 1：缓存加锁（多线程下 dict 读写在"满 → clear"时与读者竞争）
_CACHE_LOCK = threading.RLock()

# 移植改动 2：DNS 解析失败的处置，默认放行（原语义）；更严格的场景可改为 fail-closed
_DNS_FAIL_OPEN = True

_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class BlockedError(Exception):
    """被 SSRF 闸拦下 / 协议非法。**不可重试**，记录后跳过。"""


class RedirectLoopError(RuntimeError):
    """重定向超过上限。**可重试**（这是传输层问题，不是"被拦"）。"""


def set_dns_fail_open(fail_open: bool) -> None:
    """DNS 解析失败时：True=放行（默认，防内网 DNS 抖动打死全部任务）；False=拒绝（更严格）。"""
    global _DNS_FAIL_OPEN
    _DNS_FAIL_OPEN = bool(fail_open)


def cache_stats() -> dict:
    """只读快照，供观测面（obs/metrics）使用。"""
    with _CACHE_LOCK:
        return {"entries": len(_HOST_CACHE), "max": _HOST_CACHE_MAX, "ttl": _HOST_CACHE_TTL}


# ══════════════════════════════════════════════════════════════════
# 主机名规范化与私网判定
# ══════════════════════════════════════════════════════════════════
def _normalize_host(host: str) -> str:
    """小写 → 去 IPv6 zone id → 去尾部点。

    三件事都会影响黑名单命中：
      * `fe80::1%eth0`（zone id）：不剥掉就无法与 `fe80::1` 判等
      * `printer.local.`（FQDN 尾点）：与 `printer.local` 等价，剥掉才拦得住
    """
    h = (host or "").strip().lower()
    h = h.split("%", 1)[0]        # zone id（IPv6 链路本地常用）
    return h.rstrip(".")          # FQDN 尾点


def _v4_in_blocked_ranges(ip_int: int) -> bool:
    return any(lo <= ip_int <= hi for lo, hi in _EXTRA_BLOCKED_V4)


def _any_private(infos) -> bool:
    """getaddrinfo 结果里只要有一个私网/保留地址就算私网。"""
    for info in infos:
        try:
            raw = str(info[4][0]).split("%", 1)[0]      # 去掉 zone id
            ip = ipaddress.ip_address(raw)
            if ip.is_private or ip.is_loopback or ip.is_link_local \
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return True
            if ip.version == 4 and _v4_in_blocked_ranges(int(ip)):
                return True
        except Exception:
            return True     # 解析不出来 → 按风险处理
    return False


def _cache_get(host: str):
    with _CACHE_LOCK:
        ent = _HOST_CACHE.get(host)
        if not ent:
            return None
        priv, ts = ent
        if (time.monotonic() - ts) > _HOST_CACHE_TTL:
            _HOST_CACHE.pop(host, None)
            return None
        return priv


def _cache_put(host: str, priv: bool) -> bool:
    with _CACHE_LOCK:
        if len(_HOST_CACHE) >= _HOST_CACHE_MAX:
            # 移植改动 1：按"最旧优先"淘汰（原实现是整体 clear，会把所有仍在 TTL 内的判定一起丢掉）
            oldest = sorted(_HOST_CACHE.items(), key=lambda kv: kv[1][1])[: max(1, _HOST_CACHE_MAX // 8)]
            for k, _ in oldest:
                _HOST_CACHE.pop(k, None)
        _HOST_CACHE[host] = (priv, time.monotonic())
    return priv


def _resolve_host_private(host: str) -> bool:
    """域名 DNS 校验：解析到私网 → True。解析失败 → 取决于 `set_dns_fail_open`（默认放行）。

    注意：**解析失败放行**是有意的取舍（内网 DNS 异常时不应把整个采集打死）；
    更严格的场景调用 `set_dns_fail_open(False)` 改为 fail-closed。
    """
    hit = _cache_get(host)
    if hit is not None:
        return hit
    try:
        infos = _socket.getaddrinfo(host, None, proto=_socket.IPPROTO_TCP)
        return _cache_put(host, _any_private(infos))
    except _socket.gaierror:
        return _cache_put(host, not _DNS_FAIL_OPEN)


def is_private_url(url: str, dns_check: bool = True) -> bool:
    """True = 危险（拒绝）。覆盖：非 http(s) 协议、私网/环回/链路本地/保留/组播、
    本地域名后缀、IP 字面量变体、域名解析到私网（带 TTL 缓存）。"""
    try:
        p = urllib.parse.urlparse(str(url or ""))
        if p.scheme not in ("http", "https"):
            return True
        host = _normalize_host(p.hostname or "")
        if not host:
            return True
        if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
            return True

        # ① IP 字面量：交给系统解析器按 IP 语义判（覆盖十进制/十六进制/八进制/段数变体）
        try:
            infos = _socket.getaddrinfo(host, None, flags=_socket.AI_NUMERICHOST,
                                        proto=_socket.IPPROTO_TCP)
            return _cache_put(host, _any_private(infos))
        except _socket.gaierror:
            pass    # 是域名而不是 IP 字面量 → 走 ②

        # ② 域名：DNS 校验（带 TTL，防 rebinding）
        return _resolve_host_private(host) if dns_check else False
    except Exception:
        return True     # 判定失败按风险处理


# ══════════════════════════════════════════════════════════════════
# 带逐跳校验的同步取流
# ══════════════════════════════════════════════════════════════════
class Response:
    """统一响应：status / headers / url / read() / close()。

    为什么自己包一层：urllib 在 3xx/4xx 时抛的 HTTPError 与正常响应接口不一致，
    且**不带最终 URL**——统一包装后调用方不用写两套分支。
    """

    __slots__ = ("status", "headers", "url", "_fp")

    def __init__(self, status: int, headers, url: str, fp=None):
        self.status = int(status)
        self.headers = headers or {}
        self.url = url
        self._fp = fp

    def read(self, n: int = -1) -> bytes:
        return self._fp.read(n) if self._fp is not None else b""

    def close(self) -> None:
        try:
            if self._fp is not None:
                self._fp.close()
        except Exception:
            pass
        finally:
            self._fp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁掉自动跟随：重定向必须由我们自己逐跳处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ── 每线程独立会话（checklist I1）──────────────────────────────────
# 为什么是 thread-local 而不是模块级共享一个 opener：
#   ① **不共享会话对象**：多线程共用一个 OpenerDirector 时，任何将来加进去的"会话态"
#      （连接复用、认证处理器、自定义 handler 的缓存）都会变成跨线程共享可变状态——
#      这正是"同一条共享状态不得被两种执行模型直接操作"要防的东西；
#   ② 线程各持自己的 opener：一个线程出问题（handler 抛异常）不会污染别的线程；
#   ③ 仍然**不是连接池**：urllib 每次请求自建连接、用完即关——本工程刻意不做"共享大连接池"
#      （那会绕过 DNS 复核与礼貌预算）。
_TLS = threading.local()


def _opener_for_thread():
    """取当前线程的 opener（没有就建一个）。**绝不跨线程共享**。"""
    op = getattr(_TLS, "opener", None)
    if op is None:
        op = urllib.request.build_opener(_NoRedirect())
        _TLS.opener = op
    return op


def _open_once(url: str, method: str, headers: dict, timeout: float) -> Response:
    req = urllib.request.Request(url, headers=headers, method=str(method).upper())
    try:
        fp = _opener_for_thread().open(req, timeout=timeout)
        return Response(getattr(fp, "status", 200), dict(fp.headers), url, fp)
    except urllib.error.HTTPError as e:
        # 3xx/4xx/5xx 都包装成响应交给上层判定（3xx 会被逐跳循环消费）
        return Response(e.code, dict(e.headers or {}), url, e)
    # 其它异常（超时/连接失败）**原样抛出** → 调用方按"可重试"处理


def safe_open(url: str, method: str = "GET", headers: dict | None = None,
              timeout: float = 15, max_hops: int = 10) -> Response:
    """带逐跳 SSRF 校验的同步取流。

    * 入口校验 → 关闭自动重定向 → 每跳复检落点 → 超过 max_hops 抛 RedirectLoopError
    * 中间跳的响应**必须关闭**（流式下不关会泄漏连接）
    """
    if not url or not str(url).startswith(("http://", "https://")):
        raise BlockedError(f"协议非法: {str(url)[:80]}")
    if is_private_url(url):
        raise BlockedError(f"SSRF 拦截（入口）: {str(url)[:80]}")

    hdrs = dict(headers or {})
    cur, hops = str(url), 0
    while True:
        resp = _open_once(cur, method, hdrs, timeout)
        if resp.status not in _REDIRECT_CODES:
            resp.url = cur
            return resp

        loc = resp.headers.get("Location") or resp.headers.get("location") or ""
        resp.close()                      # ← 中间跳必须关闭
        if not loc:
            return Response(resp.status, resp.headers, cur)   # 无 Location 的 3xx：交调用方

        nxt = urllib.parse.urljoin(cur, loc)
        if hops >= max_hops:
            raise RedirectLoopError(f"重定向超过 {max_hops} 跳: {cur[:80]}")
        if is_private_url(nxt):
            raise BlockedError(f"SSRF 拦截（第 {hops + 1} 跳落点）: {nxt[:80]}")
        cur, hops = nxt, hops + 1


# ══════════════════════════════════════════════════════════════════
# Windows 安全文件名/目录名
# ══════════════════════════════════════════════════════════════════
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, max_len: int = 80) -> str:
    """清洗成 Windows 合法文件名：非法字符/控制字符、保留名、结尾点与空格、长度。"""
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))
    s = s.strip().rstrip(". ")
    if s.split(".", 1)[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return (s or "_unnamed")[:max_len]


def safe_dirname(name: str, max_len: int = 80) -> str:
    """目录名：在文件名基础上**禁止 `..` 穿越**（netloc 含反斜杠可构造真实穿越）。"""
    s = safe_filename(name, max_len).replace("..", "_")
    return "_" + s if s in (".", "..") else s

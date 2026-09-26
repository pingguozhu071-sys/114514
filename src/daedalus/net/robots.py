# -*- coding: utf-8 -*-
"""robots.txt 解析与按域缓存（RFC 9309 语义）

规则（照 RFC 9309，不要"大致差不多"）：
  * **2xx** → 按内容解析规则；
  * **4xx**（含 404）→ 视为"不存在" → **允许**抓取；
  * **5xx** → 视为"暂时不可用" → **完全禁止**（保守，等它恢复再说）；
  * **网络错误/超时** → 同样按**完全禁止**处理（不可达即不可信）；
  * 重定向 → 跟随（上限 5 跳），仍然重定向则按"不可用"处理。

匹配语义：
  * 只在**同一 user-agent 组**内比较（组：`User-agent:` 行开始的块；`*` 是兜底组）；
  * **最长匹配优先**；长度相同时 **Allow 优先**（更宽松者胜）；
  * 支持 `*`（通配）与 `$`（结尾锚）；
  * 未命中任何规则 → **允许**。

`Crawl-delay` 是**非标准**扩展（RFC 9309 明确不定义），这里只**解析并暴露**，
由限速策略决定是否采用（默认按工程的礼貌预算走）。

缓存：按域缓存（默认 24h）。**缓存必须有过期**——站点改规则后我们要跟得上。
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse

from daedalus.net.ssrf_gate import BlockedError

__all__ = ["RobotsRules", "RobotsCache", "parse_robots", "RobotsDenied"]

logger = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 3600.0
MAX_REDIRECTS = 5


class RobotsDenied(Exception):
    """被 robots.txt 拒绝（**不是**传输失败，不可重试；记录后跳过）。"""


class RobotsRules:
    """一个 robots.txt 的解析结果（按 user-agent 组）。"""

    __slots__ = ("groups", "crawl_delay", "source", "status")

    def __init__(self, groups: dict[str, list[tuple[bool, str]]],
                 crawl_delay: float | None = None, source: str = "", status: int = 0):
        self.groups = groups                 # {ua_lower: [(allow?, pattern), ...]}
        self.crawl_delay = crawl_delay       # 非标准；只暴露
        self.source = source
        self.status = status

    # ── 匹配 ──────────────────────────────────────────────────────
    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        ua = (user_agent or "").lower()
        best = None
        for key, rules in self.groups.items():
            if key == "*":
                continue
            if key and key in ua:                      # UA 组名是 UA 字符串的子串（RFC 语义）
                if best is None or len(key) > len(best):
                    best = key
        if best is not None:
            return self.groups[best]
        return self.groups.get("*", [])

    def allows(self, path: str, user_agent: str = "*") -> bool:
        """给一条 **path**（含查询，不含域名）判是否允许。未命中任何规则 → 允许。"""
        target = str(path or "/")
        rules = self._rules_for(user_agent)
        best_len, best_allow = -1, True
        for allow, pattern in rules:
            if _pattern_matches(pattern, target):
                plen = len(pattern.rstrip("$"))
                if plen > best_len or (plen == best_len and allow and not best_allow):
                    best_len, best_allow = plen, allow
        return best_allow if best_len >= 0 else True


def _pattern_matches(pattern: str, target: str) -> bool:
    """robots 的路径模式：`*` 通配、`$` 结尾锚（其余按字面量）。"""
    p = str(pattern or "")
    if not p:
        return False
    end_anchor = p.endswith("$")
    if end_anchor:
        p = p[:-1]
    parts = [re.escape(x) for x in p.split("*")]
    rx = "^" + ".*".join(parts) + ("$" if end_anchor else "")
    try:
        return re.search(rx, target) is not None
    except re.error:
        return False


def parse_robots(text: str, source: str = "", status: int = 200) -> RobotsRules:
    """解析 robots.txt 文本。解析失败不抛异常——按"空规则"（允许）处理并记录。"""
    groups: dict[str, list[tuple[bool, str]]] = {}
    crawl_delay: float | None = None
    current: list[str] = []
    last_was_ua = False
    try:
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, _, value = line.partition(":")
            field = field.strip().lower()
            value = value.strip()
            if field == "user-agent":
                key = value.lower()
                # **一组 User-agent 行**（连续多行）共用同一组规则；遇到非 UA 行后，
                # 下一个 UA 行就是**新组**——这里必须重置，否则后一组的规则会污染前一组
                # （本工程门禁 B1 抓到过：`Disallow: /` 泄漏进了 `*` 组，导致"未命中应允许"失效）
                if not last_was_ua:
                    current = []
                current.append(key)
                groups.setdefault(key, [])
                last_was_ua = True
            elif field in ("allow", "disallow"):
                last_was_ua = False
                if not current:
                    continue                    # 没有 User-agent 行前缀 → 忽略（RFC 要求显式组）
                allow = field == "allow"
                for key in current:
                    groups.setdefault(key, []).append((allow, value or "/"))
            elif field == "crawl-delay":
                last_was_ua = False
                try:
                    crawl_delay = float(value)
                except Exception:
                    pass
            else:
                last_was_ua = False
    except Exception as e:                       # pragma: no cover - 解析器自身绝不致命
        logger.warning("robots 解析异常（按空规则处理）: %s", e)
    return RobotsRules(groups, crawl_delay=crawl_delay, source=source, status=status)


class RobotsCache:
    """按域的 robots.txt 缓存。**5xx/不可达 → 完全禁止**；4xx → 允许（RFC 9309）。"""

    def __init__(self, fetcher=None, ttl: float = DEFAULT_TTL, user_agent: str = "*"):
        self._fetcher = fetcher                  # 需要 fetcher.open(url)（唯一咽喉）
        self._ttl = float(ttl)
        self._ua = user_agent
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[RobotsRules, float, str]] = {}   # host -> (rules, ts, status)
        self._blocked: dict[str, float] = {}     # 明确禁止的 host -> 到期时间

    # ── 取/缓存 ───────────────────────────────────────────────────
    def _get_rules(self, host: str, scheme: str) -> RobotsRules:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(host)
            if hit and (now - hit[1]) <= self._ttl:
                return hit[0]
        url = f"{scheme or 'https'}://{host}/robots.txt"
        rules = self._fetch(url, host)
        with self._lock:
            self._cache[host] = (rules, time.monotonic(), str(rules.status))
        return rules

    def _fetch(self, url: str, host: str) -> RobotsRules:
        if self._fetcher is None:
            return RobotsRules({}, source=url, status=0)          # 没有取流器 → 视为无规则
        try:
            # **必须走 `open_for_robots`**：取 robots.txt 这件事本身不能再过一遍 robots 规则，
            # 否则就是"自己吃自己"的纯递归（S9 用 CLI 端到端跑出来的严重 bug：
            # 一个网络包都没发，却把每个首次访问的域都判成"完全禁止"）。
            opener = getattr(self._fetcher, "open_for_robots", None) or self._fetcher.open
            resp = opener(url, timeout=10)
            status = int(getattr(resp, "status", 0) or 0)
            with resp:
                body = resp.read(512 * 1024)                      # robots 不该很大
            if 200 <= status < 300:
                return parse_robots(body.decode("utf-8", "replace"), source=url, status=status)
            if 400 <= status < 500:
                logger.info("robots %s 返回 %s → 视为不存在（允许）", host, status)
                return RobotsRules({}, source=url, status=status)
            # 5xx / 3xx / 其它 → 不可用
            logger.warning("robots %s 返回 %s → 按**完全禁止**处理", host, status)
            return RobotsRules({"*": [(False, "/")]}, source=url, status=status)
        except BlockedError as e:
            logger.warning("robots %s 被闸拦下（%s）→ 按完全禁止处理", host, e)
            return RobotsRules({"*": [(False, "/")]}, source=url, status=-1)
        except Exception as e:
            logger.warning("robots %s 取不到（%s）→ 按完全禁止处理", host, type(e).__name__)
            return RobotsRules({"*": [(False, "/")]}, source=url, status=-2)

    # ── 对外 ─────────────────────────────────────────────────────
    def allowed(self, url: str) -> bool:
        """这条 URL 是否被 robots 允许。（URL 本身应已过闸；这里只管规则。）"""
        try:
            p = urllib.parse.urlparse(str(url or ""))
            host = (p.hostname or "").lower()
            if not host:
                return False
            rules = self._get_rules(host, p.scheme)
            path = p.path or "/"
            if p.query:
                path = f"{path}?{p.query}"
            return rules.allows(path, self._ua)
        except Exception as e:                   # 判不出来就保守拒绝
            logger.warning("robots 判定异常（按禁止处理）: %s", e)
            return False

    def check(self, url: str) -> None:
        """不允许就抛 `RobotsDenied`（供取流咽喉调用）。"""
        if not self.allowed(url):
            raise RobotsDenied(f"robots.txt 不允许: {str(url)[:120]}")

    def allowed_cached(self, url: str) -> tuple[bool, bool]:
        """**只用内存缓存**判定 robots，绝不发网络请求。返回 `(是否允许, 是否有缓存规则)`。

        给 `collect --dry-run` 用（`cli.py` 的自证是「network_calls 实测为 0」）——
        真实事故：dry-run 里调了 `Fetcher.is_allowed()` → 现场抓 robots.txt →
        被自己的断言「dry-run 竟然出网了」拦下（台账 B20-1）。没有缓存规则时
        返回 `(True, False)`：调用方（dry-run 的计划展示）必须**如实标注「未判定」**，
        不许假装判过；真跑时仍会走完整的 robots 流程（含取流）。
        """
        try:
            p = urllib.parse.urlparse(str(url or ""))
            host = (p.hostname or "").lower()
            if not host:
                return False, True
            now = time.monotonic()
            with self._lock:
                hit = self._cache.get(host)
                if hit and (now - hit[1]) <= self._ttl:
                    path = p.path or "/"
                    if p.query:
                        path = f"{path}?{p.query}"
                    return hit[0].allows(path, self._ua), True
        except Exception as e:                   # 缓存判定出错：按"没有缓存"处理，不伪装
            logger.debug("robots 缓存判定失败（按未缓存处理）: %s", e)
        return True, False

    def crawl_delay(self, url: str) -> float | None:
        """该域声明的 `Crawl-delay`（非标准，仅暴露给限速策略）。"""
        try:
            p = urllib.parse.urlparse(str(url or ""))
            host = (p.hostname or "").lower()
            if not host:
                return None
            return self._get_rules(host, p.scheme).crawl_delay
        except Exception:
            return None

    def stats(self) -> dict:
        with self._lock:
            return {"hosts_cached": len(self._cache), "ttl": self._ttl,
                    "statuses": {h: s for h, (_, _, s) in
                                 ((k, v) for k, v in self._cache.items())}}

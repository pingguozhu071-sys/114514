# -*- coding: utf-8 -*-
"""Cookie 导入与使用（个人使用场景：**你自己有权访问的会话**）

支持两种来源（都是常见导出格式，自己导出自己的）：
  * **Netscape `cookies.txt`**：`domain \t flag \t path \t secure \t expiration \t name \t value`
    （`#HttpOnly_` 前缀行也认；`#` 开头是注释）；
  * **JSON**：`[{"name","value","domain","path","expires","secure","httpOnly"}, ...]`
    （浏览器扩展 / CDP 导出常见形状；也接受 `{"cookies": [...]}` 包裹）。

三条纪律（与 `docs/11` 一致）：
  1) **绝不入包、不入库明文、不进日志**：落盘一律走 DPAPI 密文（`privacy/secrets`），
     本模块**不提供**明文落盘选项；`summary()` 只输出计数，不输出值。
  2) **运行态不脱敏**：请求时按规则原样发送（抹掉就等于换了个身份，直接 403）。
  3) **失效要明确报错**：过期/无匹配域时给出可读原因，**不许静默变成 403**
     （`missing_reason(url)`）。

匹配规则按 RFC 6265 的核心部分：域匹配（host-only vs 域 cookie）、路径前缀、
`Secure` 仅 https、过期跳过；同域多条按**路径从长到短**排序后拼接。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse

from daedalus.privacy import secrets

__all__ = ["CookieEntry", "CookieJar", "parse_netscape", "parse_json"]

logger = logging.getLogger(__name__)

NETSCAPE_FIELDS = 7


class CookieEntry:
    __slots__ = ("domain", "name", "value", "path", "expires", "secure", "host_only")

    def __init__(self, name: str, value: str, domain: str, path: str = "/",
                 expires: float = 0.0, secure: bool = False, host_only: bool = True):
        self.name = str(name)
        self.value = str(value)
        self.domain = str(domain or "").lstrip(".").lower()
        self.path = str(path or "/")
        self.expires = float(expires or 0.0)      # 0 = 会话 cookie（不过期）
        self.secure = bool(secure)
        self.host_only = bool(host_only)

    def expired(self, now: float | None = None) -> bool:
        if self.expires <= 0:
            return False                           # 会话 cookie
        return self.expires < (now if now is not None else time.time())

    def matches(self, host: str, path: str, secure: bool) -> bool:
        host = (host or "").lower()
        if self.secure and not secure:
            return False
        if self.host_only:
            if host != self.domain:
                return False
        else:
            if not (host == self.domain or host.endswith("." + self.domain)):
                return False
        if not str(path or "/").startswith(self.path):
            return False
        return True

    def to_public(self) -> dict:
        """对外可见的部分（**不含 value**）。"""
        return {"domain": self.domain, "name": self.name, "path": self.path,
                "expires": self.expires, "secure": self.secure, "host_only": self.host_only}


def parse_netscape(text: str) -> list[CookieEntry]:
    """解析 Netscape cookies.txt（含 `#HttpOnly_` 行；`#` 其它行是注释）。"""
    out: list[CookieEntry] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < NETSCAPE_FIELDS:
            parts = line.split()                   # 有些导出用空格分隔
        if len(parts) < NETSCAPE_FIELDS:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:NETSCAPE_FIELDS]
        try:
            exp = float(expires)
        except Exception:
            exp = 0.0
        out.append(CookieEntry(name=name, value=value, domain=domain, path=path,
                               expires=exp, secure=str(secure).upper() == "TRUE",
                               host_only=not str(domain).startswith(".") and not http_only))
    return out


def parse_json(obj) -> list[CookieEntry]:
    """解析 JSON 导出（列表 / `{"cookies": [...]}` / 单对象）。"""
    data = obj
    if isinstance(obj, str):
        data = json.loads(obj)
    if isinstance(data, dict):
        data = data.get("cookies", data.get("data", data))
    if isinstance(data, dict):
        data = [data]
    out: list[CookieEntry] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        try:
            exp = item.get("expires", item.get("expirationDate", 0)) or 0
            if isinstance(exp, str):
                exp = float(exp or 0)
            dom = str(item.get("domain", "") or "")
            out.append(CookieEntry(
                name=item.get("name", ""), value=item.get("value", ""),
                domain=dom, path=item.get("path", "/"), expires=float(exp or 0),
                secure=bool(item.get("secure", False)),
                host_only=not dom.startswith(".")))
        except Exception as e:
            logger.warning("跳过一条无法解析的 cookie 项: %s", e)
    return out


class CookieJar:
    """会话 jar：导入 → 加密落盘 → 按 URL 取 Cookie 头。"""

    def __init__(self, entries: list[CookieEntry] | None = None, name: str = "cookies"):
        self.name = str(name)
        self._entries: list[CookieEntry] = list(entries or [])
        self._imported_at = time.time()

    # ── 导入/落盘 ─────────────────────────────────────────────────
    def import_file(self, path) -> int:
        """按扩展名猜格式导入。返回导入条数（**不改动、不删除**源文件）。"""
        from pathlib import Path
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        entries = parse_json(text) if p.suffix.lower() in (".json",) else parse_netscape(text)
        self._entries.extend(entries)
        logger.info("导入 cookie %d 条（来自 %s）", len(entries), p.name)
        return len(entries)

    def add(self, entry: CookieEntry) -> None:
        self._entries.append(entry)

    def entries(self) -> list[CookieEntry]:
        """只读副本（**含 value**，仅限本进程内使用；不要拿去打日志或导出）。"""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(list(self._entries))

    def save(self) -> bool:
        """加密落盘（DPAPI）。**失败返回 False，由调用方如实告知用户**（不谎报加密）。"""
        payload = [dict(e.to_public(), value=e.value) for e in self._entries]
        ok = secrets.save_secret_json(self.name, {"entries": payload}, kind="cookies")
        if not ok:
            logger.warning("cookie 加密落盘失败（本机可能不支持 DPAPI）——**未写明文**")
        return ok

    @classmethod
    def load(cls, name: str = "cookies") -> "CookieJar":
        data = secrets.load_secret_json(name)
        if not data:
            return cls(name=name)
        entries = []
        for item in data.get("entries", []) or []:
            try:
                entries.append(CookieEntry(name=item.get("name", ""), value=item.get("value", ""),
                                           domain=item.get("domain", ""), path=item.get("path", "/"),
                                           expires=item.get("expires", 0),
                                           secure=item.get("secure", False),
                                           host_only=item.get("host_only", True)))
            except Exception:
                continue
        return cls(entries=entries, name=name)

    # ── 使用 ─────────────────────────────────────────────────────
    def remove_expired(self) -> int:
        """剔除过期项（**有记录**，不静默）。返回剔除条数。"""
        now = time.time()
        before = len(self._entries)
        self._entries = [e for e in self._entries if not e.expired(now)]
        gone = before - len(self._entries)
        if gone:
            logger.info("剔除已过期 cookie %d 条（剩余 %d 条）", gone, len(self._entries))
        return gone

    def header_for(self, url: str) -> str:
        """按 URL 拼 Cookie 头；无可用项返回空串（调用方应配合 `missing_reason` 报错）。"""
        try:
            p = urllib.parse.urlparse(str(url or ""))
            host = (p.hostname or "").lower()
            path = p.path or "/"
            secure = p.scheme == "https"
            now = time.time()
            usable = [e for e in self._entries
                      if not e.expired(now) and e.matches(host, path, secure)]
            usable.sort(key=lambda e: len(e.path), reverse=True)      # 长路径优先（RFC 6265）
            return "; ".join(f"{e.name}={e.value}" for e in usable)
        except Exception as e:
            logger.warning("拼 Cookie 头失败: %s", e)
            return ""

    def missing_reason(self, url: str) -> str:
        """为什么没 Cookie？（**把"失效"说清楚**，别让它变成一次莫名其妙的 403）"""
        try:
            p = urllib.parse.urlparse(str(url or ""))
            host = (p.hostname or "").lower()
            same_domain = [e for e in self._entries if host == e.domain or host.endswith("." + e.domain)]
            if not self._entries:
                return "jar 为空：还没导入任何 cookie"
            if not same_domain:
                return f"{host} 没有匹配的 cookie（jar 里有 {len(self._entries)} 条，涉及 {len(self.domains())} 个域）"
            alive = [e for e in same_domain if not e.expired()]
            if not alive:
                return f"{host} 的 {len(same_domain)} 条 cookie **全部已过期**，需要重新导出导入"
            return f"{host} 有 {len(alive)} 条可用 cookie（若仍 403，可能是会话被服务端失效）"
        except Exception as e:
            return f"无法判断（{type(e).__name__}）"

    def domains(self) -> set[str]:
        return {e.domain for e in self._entries}

    def summary(self) -> dict:
        """只输出计数与域列表，**不含任何 cookie 值**（可以安全打日志）。"""
        now = time.time()
        expired = sum(1 for e in self._entries if e.expired(now))
        return {"total": len(self._entries), "expired": expired,
                "domains": sorted(self.domains()),
                "imported_at": self._imported_at}

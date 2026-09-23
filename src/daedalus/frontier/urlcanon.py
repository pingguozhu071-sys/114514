# -*- coding: utf-8 -*-
"""URL 规范化（≥12 条规则）与指纹

为什么必须有：**同一个页面会以几十种写法出现**（尾斜杠、大小写、跟踪参数、排序不同的 query、
`www.` 前缀、默认端口、锚点、`?utm_*`……）。不规范化就会重复抓、重复入库，
去重与"未变"判定全部失效。

规则清单（`RULES`，可核对、可扩充；门禁会断言条数）：
   1) 只接受 http/https（其它一律 None）；
   2) scheme 小写；
   3) host 小写、去尾部点、去 IPv6 zone id；
   4) 去默认端口（http:80 / https:443）；
   5) 去空路径 → `/`；
   6) 合并重复斜杠（路径内 `//` → `/`）；
   7) 去 fragment（`#...`）；
   8) query 参数**按名字排序**（同名多值保持原序）；
   9) 去掉跟踪参数（`utm_*`/`gclid`/`fbclid`/`spm`/`from`/`share_*`/`ref` 等，可配）；
  10) 去空值参数（`a=&b=1` → `b=1`）；
  11) 路径百分号编码规范化（只保留必要的转义）；
  12) 去掉会话型参数（`sid`/`sessionid`/`phpsessid` 等——它们每次不同，留着会让去重失效）；
  13) `www.` 前缀可选归一（默认**保留**：有些站 www 与非 www 内容不同；需要时打开 `strip_www`）。

**不做**的事情：不猜测语义（不把 `/a?b=c` 改写成 `/a/c`）、不跟随重定向（那是取流层的事）。
"""

from __future__ import annotations

import hashlib
import re
import urllib.parse

__all__ = ["canonicalize", "url_fingerprint", "RULES", "DROP_PARAM_PREFIXES", "DROP_PARAMS"]

RULES = (
    "只接受 http/https", "scheme 小写", "host 小写/去尾点/去 zone id", "去默认端口",
    "空路径→/", "合并重复斜杠", "去 fragment", "query 按名排序", "去跟踪参数",
    "去空值参数", "百分号编码规范化", "去会话型参数", "www 可选归一",
    # ↓ 以下两条是"**刻意不做**归一"的显式声明（免得后人"顺手"改掉）：
    "尾斜杠不归一（/a 与 /a/ 可能是不同资源）",
    "签名参数不丢（token/sig 不是跟踪参数；丢了会 403）",
    "不跟随重定向、不做语义改写（那是取流层的事）",
)

DROP_PARAM_PREFIXES = ("utm_", "spm", "share_", "hmsr", "hmpl", "hmcu", "hmkw", "hmci")
DROP_PARAMS = frozenset({
    "gclid", "fbclid", "msclkid", "yclid", "dclid", "igshid", "mc_eid", "mc_cid",
    "from", "ref", "referer", "referrer", "src", "source", "share_token", "spm_id_from",
    "sid", "sessionid", "session_id", "phpsessid", "jsessionid", "asp.net_sessionid",
    "_ga", "_gl", "timestamp", "_t", "cachebust", "cb",
})

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MULTI_SLASH = re.compile(r"/{2,}")
_UNRESERVED = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/]")


def _normalize_host(host: str) -> str:
    h = (host or "").strip().lower()
    h = h.split("%", 1)[0]           # IPv6 zone id
    return h.rstrip(".")             # FQDN 尾点


def _pct_normalize(path: str) -> str:
    """把不必转义的百分号编码还原（`%7E` → `~`），其余保持大写十六进制。"""
    def repl(m: re.Match) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if _UNRESERVED.match(ch) else "%" + m.group(1).upper()
    return re.sub(r"%([0-9A-Fa-f]{2})", repl, path or "")


def canonicalize(url: str, *, strip_www: bool = False, drop_params: frozenset | None = None,
                 keep_query: bool = True) -> str | None:
    """规范化 URL；不合规（协议/主机缺失）返回 `None`。**不抛异常**。"""
    try:
        raw = str(url or "").strip()
        if not raw:
            return None
        p = urllib.parse.urlsplit(raw)
        scheme = (p.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = _normalize_host(p.hostname or "")
        if not host:
            return None
        if strip_www and host.startswith("www.") and host.count(".") >= 2:
            host = host[4:]
        # 默认端口：urllib 的 netloc 里可能带端口，用 port 属性判
        try:
            port = p.port
        except ValueError:
            return None
        netloc = host
        if p.username or p.password:
            userinfo = urllib.parse.quote(p.username or "", safe="")
            if p.password:
                userinfo += ":" + urllib.parse.quote(p.password or "", safe="")
            netloc = f"{userinfo}@{netloc}"
        if port and port != _DEFAULT_PORTS.get(scheme):
            netloc = f"{netloc}:{port}"
        path = _MULTI_SLASH.sub("/", p.path or "/") or "/"
        path = _pct_normalize(path)
        query = ""
        if keep_query and p.query:
            pairs = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            drops = drop_params if drop_params is not None else DROP_PARAMS
            kept = [(k, v) for k, v in pairs
                    if v != "" and k not in drops
                    and not any(k.startswith(pre) for pre in DROP_PARAM_PREFIXES)]
            kept.sort(key=lambda kv: (kv[0], kv[1]))
            query = urllib.parse.urlencode(kept, doseq=True)
        return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return None


def url_fingerprint(url: str, *, strip_www: bool = False) -> str | None:
    """规范 URL 的短指纹（16 hex）——**去重键**与队列主键用。"""
    canon = canonicalize(url, strip_www=strip_www)
    if canon is None:
        return None
    return hashlib.sha256(canon.encode("utf-8", "ignore")).hexdigest()[:16]

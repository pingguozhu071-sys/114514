# -*- coding: utf-8 -*-
"""脱敏原语（线程模型无关，纯同步，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/sanitizer.py`，经《新工程开工包》精简重写，
在 Daedalus 内保持实现不变；仅新增：**引擎业务字段并入白名单**（媒体/流/站点适配器产出的字段名），
以免"新字段里的 token 从新字段漏出去"（Kiana 当初只脱了正文就是这个原因）。
按数据类型的**开关**不在本文件里做——那是 S1 的策略层（`SanitizationPolicy`，见 docs/11）。

────────────────────────────────────────────────────────────────
三条必须记住的边界（Kiana 用血换的）
  1) 日志脱敏的过滤器要挂在 **handler** 上，不是挂在 root logger 上。
     Python logging 的传播只调用祖先的 **handler**，**不调用**祖先 logger 的 filter
     —— 挂错位置 = 子 logger 的日志**完全没有脱敏**（Kiana 长期如此且无人察觉）。
     本文件只管"脱什么"，"挂哪里"见 `logging_sanitizer.py`。
  2) **导出/落盘的副本要整条记录脱敏**：只脱正文会漏掉 URL 里的 token、
     图片链接（签名 CDN）、作者、结构化实体（`sanitize_record`）。
  3) **运行态存储的 URL 不要脱敏**：队列表里的 URL 是"稍后还要再请求一次"的钥匙，
     把签名参数抹成 [REDACTED] → 续爬/续下 403、状态更新匹配不到行、任务永久卡死。
     也就是：脱敏要按"这数据给谁看"分层，不能一刀切。
  4) **原始层（raw artifact）永不脱敏**：原始字节是事实层，脱了就不能重放（S3 捕获面写死）。

────────────────────────────────────────────────────────────────
用法
    from daedalus.obs.sanitize import sanitize_url, sanitize_text, sanitize_record

    log.info("fetch %s", sanitize_url(url))          # 给人看的
    rec_safe = sanitize_record(rec)                   # 导出/落盘副本（不改原对象）
    queue.store(url)                                  # 给程序用的：**原样存**
"""

from __future__ import annotations

import re

__all__ = [
    "sanitize_url", "sanitize_text", "sanitize_headers", "sanitize_proxy",
    "sanitize_record", "sanitize_credentials", "find_emails",
]

# URL 里的敏感**参数值**：不纳入过于通用的 code/uid（会误伤正常业务参数）。
# ⚠️ 值字符类**必须在空白与引号/尖括号处停下**——否则 `[^&#]+` 会一路吃到下一个 `&` 或串尾，
#    把 URL 之后的正文一起抹掉（例："token=SECRET 联系138…" 会把后半句也变成 [REDACTED]）。
#    这是本工程自建门禁（`tests/gates/s1_gate.py` C2）抓出来的**真实缺陷**，参考实现里有这个洞。
_URL_SECRET_KEYS = (
    r"token|key|auth|session|sid|password|passwd|access_token|refresh_token|api_key|apikey"
    r"|api-key|sig|sign|signature|secret|ticket|csrf|nonce|code|jwt|assertion|saml|bearer"
    r"|credential|oauth|id_token|sessionid|jsessionid|phpsessid"
)
# 分隔符要认三种（**安全自审补的**）：`?a=1&token=X`（标准）· `?a=1;token=X`（分号风格，
# 老站点与某些网关在用，原来会漏）· `#token=X`（fragment 里的参数，原来也会漏）。
_URL_SECRET_RE = re.compile(
    rf"([?&;](?:{_URL_SECRET_KEYS})=)[^\s&#\"'<>;]+",
    re.IGNORECASE)
_URL_FRAG_SECRET_RE = re.compile(
    rf"([#&;](?:{_URL_SECRET_KEYS})=)[^\s&#\"'<>;]+",
    re.IGNORECASE)
# URL 里的 userinfo（`https://user:pass@host/`）——**安全自审发现的真缺口**：
# 带凭据的直链很常见（Basic Auth / 预签名 URL），原来完全不脱敏，日志与导出里会明文留下口令。
# 两种写法都要认：`user:pass@` 与裸 `user@`。
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^/@\s:]{1,256}:[^/@\s]{1,256})@")
_URL_BARE_USER_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)([^/@:\s]{1,256})@")

SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "proxy-authorization",
    "x-api-key", "x-auth-token", "x-csrf-token", "x-session-id",
}

# 隐私文本：**量词必须有上界**——无上界的 `[A-Za-z0-9._%+-]+@` 遇到 1MB 连续合法字符
# 会对每个起点向后扫到串尾 → O(n²) 卡死 worker（Kiana 修过一次，v2.18 的 O(n²) 版本
# 曾在别处复活）。上界按 RFC：local ≤64、domain ≤255。
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+-])")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IP_RE = re.compile(
    r"(?<![.\d])(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d{2}|[1-9]?\d)){3}"
    r"(?![.\d])")


def sanitize_url(url: str) -> str:
    """抹掉 URL 里敏感参数的值（保留参数名与结构，便于看懂是哪个参数被抹了）。

    覆盖四类（③④ 是**安全自审**补的缺口）：
      ① query 参数值（`?token=` / `&token=` / `;token=`）——保留参数名；
      ② fragment 里的参数（`#token=`）；
      ③ **userinfo 凭据**（`https://user:pass@host/` → `https://[REDACTED]@host/`）；
      ④ 一次性凭据键（`code=` 这类 OAuth 授权码：拿到就能换 token）。
    """
    try:
        s = str(url or "")
        s = _URL_SECRET_RE.sub(r"\1[REDACTED]", s)
        s = _URL_FRAG_SECRET_RE.sub(r"\1[REDACTED]", s)
        if "@" in s:
            s = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", s)
            s = _URL_BARE_USER_RE.sub(r"\1[REDACTED]@", s)
        return s
    except Exception:
        return url


def sanitize_credentials(text: str) -> str:
    """抹掉**文本里的凭据形态**（不受任何开关影响——T1 的硬要求是"日志无敏感"）。

    三类（安全自审补的缺口：原来日志出口只认 URL 参数/手机号/邮箱/IP，
    而 header 转储与异常消息里的 token 会明文落盘）：
      ① `Bearer <token>` / `Basic <base64>`；
      ② header 行：`Authorization:` / `Cookie:` / `Set-Cookie:` / `X-Api-Key:` …
      ③ `key=value` 形态的敏感键（`token=`/`sid=`/`api_key=`…，不含 `code`/`key` 这类
         在普通日志里太常见的词，见 `_TEXT_KV_SECRET_KEYS` 的注释）。

    ⚠️ **顺序有意为之**：先 scheme（`Bearer X`）再 header 行——反过来 header 行会把
    `Bearer` 当成值吃掉，剩下裸 token 谁也认不出来（自审实测踩过）。
    """
    t = str(text or "")
    if "earer" in t or "asic " in t:
        t = _TEXT_SCHEME_SECRET_RE.sub(r"\1 [REDACTED]", t)
    if ":" in t or "=" in t:
        t = _TEXT_HEADER_SECRET_RE.sub(r"\1: [REDACTED]", t)
        t = _TEXT_KV_SECRET_RE.sub(r"\1=[REDACTED]", t)
    return t


def sanitize_text(text: str) -> str:
    """脱敏文本中的**隐私**（手机号 / 邮箱 / IP）。**不管凭据**——那是 `sanitize_credentials`。

    中文边界坑：不能只用 `\\b`——在"汉字↔数字"之间词边界不成立（`\\w` 含 CJK），
    "联系13812345678或" 会漏脱敏。故用纯数字边界 `(?<!\\d)` / `(?!\\d)`。
    """
    t = str(text or "")
    t = _PHONE_RE.sub("[手机号]", t)
    if "@" in t:
        t = _EMAIL_RE.sub("[邮箱]", t)
    return _IP_RE.sub("[IP]", t)


def find_emails(text: str) -> list[str]:
    """查找（不脱敏）。**复用同一份有上界的正则**，避免各模块各写一份再分叉。"""
    try:
        return _EMAIL_RE.findall(str(text or ""))
    except Exception:
        return []


def sanitize_headers(headers: dict) -> dict:
    """脱敏敏感 HTTP 头（响应头里的 Set-Cookie 落库前必过）。"""
    try:
        return {k: ("[REDACTED]" if str(k).lower() in SENSITIVE_HEADERS else v)
                for k, v in (headers or {}).items()}
    except Exception:
        return dict(headers or {})


def sanitize_proxy(proxy: str) -> str:
    """代理串里的 user:pass@ 凭据（日志过滤器覆盖不到代理串，要单独脱）。"""
    try:
        from urllib.parse import urlsplit
        s = urlsplit(str(proxy or ""))
        if s.username or s.password:
            port = f":{s.port}" if s.port else ""
            return f"{s.scheme}://***@{s.hostname or ''}{port}"
        return proxy
    except Exception:
        return proxy


# ══════════════════════════════════════════════════════════════════
# 记录级脱敏（导出/落盘副本专用）
# ══════════════════════════════════════════════════════════════════
_URLISH_KEYS = frozenset({
    "url", "final_url", "canonical", "canonical_url", "link", "permalink", "href", "src",
    "srcset", "poster", "next_url", "next", "image", "img", "images", "video", "videos",
    "audio", "source", "thumbnail", "thumb", "cover", "avatar", "author_url", "site_url",
    "referer", "referrer", "location", "download_url", "file_url",
    # ↓ Daedalus 新增（媒体/流/站点适配器会产出这些键，不并入会从新字段漏 token）
    "media_url", "stream_url", "feed_url", "manifest_url", "playlist_url", "segment_url",
    "key_url", "raw_url", "artifact_url", "resolved_url", "redirect_url", "origin_url",
})
_TEXTISH_KEYS = frozenset({
    "title", "description", "author", "site_name", "summary", "keywords", "caption",
    "alt", "name", "username", "nickname", "label", "headline", "subtitle",
    # ↓ Daedalus 新增
    "byline", "publisher", "channel", "transcript", "lyrics", "comment", "content_text",
})
# 这些容器里的**任意**字符串都按文本脱敏（结构化数据的 key 不可枚举）
_DEEP_TEXT_CONTAINERS = frozenset({"entities", "json_ld", "structured", "metadata", "extra"})
_REC_MAX_DEPTH = 6

# ── 文本出口的"凭据形态"（安全自审补的缺口）───────────────────────
# 自审发现：日志出口原来只认 URL 里的参数、手机号、邮箱、IP——而**自由文本里的 header 转储**
# （`Authorization: Bearer X` / `Cookie: sid=X`）会原样落盘。真实触发路径是把请求头/响应头
# 或异常消息记进日志（调试时最常见），一旦发生就是明文凭据泄漏。这里补三类：
#   ① header 行：`Authorization:` / `Cookie:` / `Set-Cookie:` / `X-Api-Key:` …
#   ② 认证方案：`Bearer <token>` / `Basic <base64>`
#   ③ `key=value` 形态的敏感键（不含 URL 分隔符也认）
_TEXT_HEADER_SECRET_RE = re.compile(
    r"(?i)\b(proxy-authorization|authorization|set-cookie|cookie|x-api-key|x-auth-token"
    r"|x-csrf-token|x-session-id|api-key|apikey)\b\s*[:=]\s*(?:bearer|basic)?\s*[^\s,;\"']+")
_TEXT_SCHEME_SECRET_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
# 注意：这里**刻意不含** `key`/`auth`/`code`——它们在普通日志里太常见（`exit code=1`、
# `key=value` 的字典打印），收进来会把日志涂满 [REDACTED]，反而看不清问题。
# URL 语境下它们仍然会被 URL 规则脱敏（那里 `code=` 基本就是 OAuth 授权码）。
_TEXT_KV_SECRET_KEYS = (
    r"token|access_token|refresh_token|id_token|api_key|apikey|password|passwd|secret"
    r"|session|sessionid|jsessionid|phpsessid|csrf|nonce|signature|sig|ticket|jwt|sid"
    r"|credential|private_key|secret_key|oauth_token"
)
_TEXT_KV_SECRET_RE = re.compile(rf"(?i)\b({_TEXT_KV_SECRET_KEYS})\s*=\s*[^\s&#\"'<>;]+")

# 键名命中这些 → **整个值抹掉**（不是"再过一遍文本脱敏"：它本身就是凭据，
# 里面没有任何值得保留的结构）。安全自审补的：原来只按 header 名抹，
# 结构化字段里叫 `cookie`/`token`/`sid` 的键会漏。
_SECRET_KEYS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie", "cookies", "set_cookie",
    "token", "access_token", "refresh_token", "id_token", "oauth_token", "bearer",
    "api_key", "apikey", "api-key", "x-api-key", "x-auth-token", "x-csrf-token",
    "password", "passwd", "pass", "secret", "secret_key", "private_key", "credential",
    "session", "session_id", "sessionid", "jsessionid", "phpsessid", "sid", "csrf", "nonce",
    "signature", "sig", "ticket", "jwt", "assertion",
}) | SENSITIVE_HEADERS


def sanitize_record(data, *, max_depth: int = _REC_MAX_DEPTH):
    """返回一条记录的**脱敏副本**（不改动原对象；异常一律退回原值）。

    * 像 URL 的值（键名命中 _URLISH_KEYS 或以 http(s):// 开头）→ sanitize_url
    * 文本键与结构容器 → sanitize_text
    * 深度封顶防病态嵌套；非 dict/list 原样返回

    ⚠️ 只用于**导出/落盘副本**。运行态（队列表、下载队列）**不要**过这里，见文件头第 3 条。
    ⚠️ 原始层（raw artifact）**永远不要**过这里，见文件头第 4 条。
    """
    if not isinstance(data, (dict, list)):
        return data

    def _one(key, val, depth, deep_text=False):
        if depth > max_depth:
            return val
        lk = str(key or "").lower()
        # 键名本身就是凭据 → **整个值抹掉**（含嵌套容器里的全部字符串）
        if lk in _SECRET_KEYS:
            return _redact_all(val, depth)
        if isinstance(val, dict):
            return {k: _one(k, v, depth + 1, deep_text) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_one(key, v, depth + 1, deep_text) for v in val]
        if isinstance(val, str) and val:
            if val.startswith(("http://", "https://")) or lk in _URLISH_KEYS:
                return sanitize_url(val)
            if deep_text or lk in _TEXTISH_KEYS:
                return sanitize_text(val)
        return val

    def _redact_all(val, depth):
        """把一棵子树里的所有标量抹掉（键名已是凭据，值里没有值得保留的结构）。"""
        if isinstance(val, dict):
            return {k: _redact_all(v, depth + 1) for k, v in val.items()}
        if isinstance(val, (list, tuple)):
            return [_redact_all(v, depth + 1) for v in val]
        return "[REDACTED]" if val not in (None, "") else val

    try:
        if isinstance(data, list):
            return [_one(None, v, 1) for v in data]
        return {k: _one(k, v, 1, str(k or "").lower() in _DEEP_TEXT_CONTAINERS)
                for k, v in data.items()}
    except Exception:
        return data

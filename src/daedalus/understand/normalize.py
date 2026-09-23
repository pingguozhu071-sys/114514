# -*- coding: utf-8 -*-
"""归一化：把各解析器的产物**收成一种形状**

规约（简单、可预期，别加花样）：
  * 字段固定为 `FIELDS` 里那几个；缺的补 `""`/`[]`/`None`，**不猜**；
  * 文本统一：去零宽字符、合并空白、去首尾；超长裁剪（上限见 `MAX_TEXT`）；
  * 时间统一成 **epoch 秒**（`to_epoch` 认 epoch 数字 / ISO8601 / RFC822 三种常见形态），
    认不出来就 `None`——**不编造时间**；
  * 链接去重且**顺序保留**；
  * 额外字段塞进 `extra`，不混进固定字段（需要什么字段就显式加进 `FIELDS`）。
"""

from __future__ import annotations

import email.utils
import logging
import re
import time

logger = logging.getLogger(__name__)

__all__ = ["normalize_record", "to_epoch", "FIELDS", "MAX_TEXT"]

FIELDS = ("url", "title", "text", "published_at", "author", "site", "tags", "links",
          "parser", "parser_version", "format", "extra")

MAX_TEXT = 2_000_000
MAX_TITLE = 500

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
_LINES_RE = re.compile(r"\n{3,}")

_TIME_KEYS = ("published_at", "updated_at", "created_at", "pubdate", "published",
              "lastmod", "date", "datetime", "time")


def to_epoch(value) -> float | None:
    """把常见时间形态转成 epoch 秒；认不出返回 None（**不编造**）。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:              # 看起来是毫秒
            v /= 1000.0
        return v if v > 0 else None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return to_epoch(int(s))
    try:
        dt = email.utils.parsedate_to_datetime(s)      # RFC822（RSS 常见）
        if dt is not None:
            return dt.timestamp()
    except Exception:
        pass
    try:                                               # ISO8601（补 Z）
        import datetime as _dt
        iso = s.replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(iso).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return time.mktime(time.strptime(s[:19], fmt))
        except Exception:
            continue
    return None


def _clean(text, limit: int) -> str:
    t = str(text or "").translate(_ZERO_WIDTH)
    t = _WS_RE.sub(" ", t)
    t = "\n".join(ln.strip() for ln in t.splitlines())
    return _LINES_RE.sub("\n\n", t).strip()[:limit]


def normalize_record(rec: dict) -> dict:
    """归一到固定形状（不改原对象；未知字段进 `extra`）。"""
    rec = dict(rec or {})
    # 时间字段**大小写不敏感**（RSS 用 pubDate、Atom 用 updated、sitemap 用 lastmod）
    lowered = {str(k).lower(): v for k, v in rec.items()}
    published = None
    for key in _TIME_KEYS:
        if lowered.get(key) not in (None, ""):
            published = to_epoch(lowered[key])
            break
    links = []
    for item in (rec.get("links") or []):
        s = str(item or "").strip()
        if s.startswith(("http://", "https://")) and s not in links:
            links.append(s)
    tags = [str(t).strip() for t in (rec.get("tags") or []) if str(t or "").strip()]
    extra = {k: v for k, v in rec.items()
             if k not in FIELDS and k not in _TIME_KEYS and k not in ("ok", "error")}
    out = {
        "url": str(rec.get("url") or ""),
        "title": _clean(rec.get("title"), MAX_TITLE),
        "text": _clean(rec.get("text") or rec.get("summary") or "", MAX_TEXT),
        "published_at": published,
        "author": _clean(rec.get("author"), MAX_TITLE),
        "site": _clean(rec.get("site"), MAX_TITLE),
        "tags": tags[:200],
        "links": links[:20_000],
        "parser": str(rec.get("parser") or ""),
        "parser_version": int(rec.get("parser_version") or 0),
        "format": str(rec.get("format") or ""),
        "extra": extra,
    }
    return out

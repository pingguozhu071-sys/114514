# -*- coding: utf-8 -*-
"""订阅与站点地图解析器：RSS / Atom / Sitemap（**用正则，不启用 XML 实体**）

为什么用正则而不是 XML 解析器（《开工说明》§3.4-3 与《01》§7 的经验）：
  * RSS/Sitemap 的结构极浅（`<item>`/`<entry>`/`<url>` 里几个字段），正则够用；
  * **正则天然没有 XXE / billion laughs（实体膨胀）攻击面**——不用 XML 解析器就没有实体展开；
  * 代价是"畸形 XML 的鲁棒性差"，所以这里再加一道：**见到 DOCTYPE/ENTITY 直接拒绝**，
    并给出可读原因（宁可不解析，也不引入实体解析面）。
  若将来确实要用 XML 解析器：**解析前必须拒绝 DOCTYPE 与外部实体**（这条写进注释，别忘）。
"""

from __future__ import annotations

import re

from daedalus.understand.registry import ParserSpec

__all__ = ["SPEC", "parse_feed", "parse_sitemap"]

VERSION = 1

_ITEM_RE = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_LOC_RE = re.compile(r"<url\b[^>]*>(.*?)</url>", re.IGNORECASE | re.DOTALL)
_TAG = lambda name: re.compile(  # noqa: E731 - 小工具，局部使用
    rf"<{name}\b[^>]*>(.*?)</{name}>", re.IGNORECASE | re.DOTALL)
_LINK_HREF_RE = re.compile(r'<link\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_DANGEROUS_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)


def _text_of(block: str, tag: str) -> str:
    m = _TAG(tag).search(block or "")
    if not m:
        return ""
    raw = m.group(1)
    cd = _CDATA_RE.search(raw)
    if cd:
        raw = cd.group(1)
    return re.sub(r"<[^>]+>", " ", raw).strip()[:2000]


def _strip_ns(tag: str) -> str:
    return tag.split(":", 1)[-1] if ":" in tag else tag


def parse_feed(data: bytes, meta: dict) -> dict:
    text = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")
    if _DANGEROUS_RE.search(text[:4096]):
        return {"ok": False, "error": "含 DOCTYPE/ENTITY：为避免实体解析面，拒绝对该文档做 XML 解析"}
    items = []
    for m in _ITEM_RE.finditer(text):
        block = m.group(2)
        link = _text_of(block, "link")
        if not link:
            href = _LINK_HREF_RE.search(block)
            link = href.group(1).strip() if href else ""
        items.append({
            "title": _text_of(block, "title"),
            "link": link,
            "published": _text_of(block, "pubDate") or _text_of(block, "updated")
                         or _text_of(block, "published"),
            "summary": _text_of(block, "description") or _text_of(block, "summary")
                       or _text_of(block, "content"),
        })
    if not items:
        return {"ok": False, "error": "订阅里没有任何 <item>/<entry>（空订阅或格式不符）"}
    feed_title = _text_of(text, "title")
    return {"ok": True, "kind": "feed", "feed_title": feed_title[:300],
            "items": items[:5000], "item_count": len(items)}


def parse_sitemap(data: bytes, meta: dict) -> dict:
    text = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")
    if _DANGEROUS_RE.search(text[:4096]):
        return {"ok": False, "error": "含 DOCTYPE/ENTITY：拒绝对该文档做 XML 解析（避免实体解析面）"}
    urls = []
    for m in _LOC_RE.finditer(text):
        block = m.group(1)
        loc = _text_of(block, "loc")
        if not loc:
            continue
        urls.append({"loc": loc, "lastmod": _text_of(block, "lastmod"),
                     "changefreq": _text_of(block, "changefreq"),
                     "priority": _text_of(block, "priority")})
    # 站点地图索引（sitemapindex）里是 <sitemap><loc>
    sub = [_text_of(b, "loc") for b in _TAG("sitemap").findall(text)]
    sub = [s for s in sub if s]
    if not urls and not sub:
        return {"ok": False, "error": "站点地图里没有 <url>/<sitemap> 条目"}
    return {"ok": True, "kind": "sitemap", "urls": urls[:50_000], "url_count": len(urls),
            "sub_sitemaps": sub[:5000], "sub_count": len(sub)}


def parse_feed_any(data: bytes, meta: dict) -> dict:
    """RSS/Atom 与 Sitemap 混着喂：按内容自己挑。"""
    text = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")[:4096].lower()
    if "<urlset" in text or "<sitemapindex" in text:
        return parse_sitemap(data, meta)
    return parse_feed(data, meta)


SPEC = ParserSpec(name="feed_sitemap", version=VERSION,
                  accepts=("rss", "atom", "sitemap", "sitemap_index", "xml"),
                  parse=parse_feed_any, order=20,
                  note="正则解析（无 XXE 面）；见 DOCTYPE/ENTITY 直接拒绝")

# -*- coding: utf-8 -*-
"""HTML 解析器：标题 / 正文文本 / 链接 / meta / JSON-LD

要点：
  * 用 **lxml**（C 层，快；GIL 下短任务与下载线程交错好）；
  * 链接**转绝对地址**（相对链接没有任何用处）；去掉 `#fragment` 与 `javascript:`;
  * JSON-LD 优先（结构化数据最稳），失败不影响整体（**诚实降级**：这一项失败就说这一项）；
  * 不追求"正文提取算法"的最优解——先给"整页文本 + 字段"，正文精提取属于后续专题。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from daedalus.understand.registry import ParserSpec

__all__ = ["SPEC", "parse_html", "extract_links", "extract_json_ld"]

VERSION = 1

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_JSONLD_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r'<meta[^>]+(?:name|property)\s*=\s*["\']([^"\']+)["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE)


def _clean_text(text: str) -> str:
    t = _WS_RE.sub(" ", str(text or "")).replace("\xa0", " ")
    lines = [ln.strip() for ln in t.splitlines()]
    return _BLANK_RE.sub("\n\n", "\n".join(ln for ln in lines if ln)).strip()


def extract_links(html: str, base_url: str = "") -> list[str]:
    """抽链接并转绝对地址（去 fragment / javascript / mailto）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', html or "", re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href) if base_url else href
        absolute = urllib.parse.urldefrag(absolute)[0]
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def extract_json_ld(html: str) -> list[Any]:
    """抽 `<script type="application/ld+json">` 里的结构化数据（尽力而为，不抛）。"""
    out: list[Any] = []
    for m in _JSONLD_RE.finditer(html or ""):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except Exception:
            # 常见脏数据：尾逗号、单引号——试着修一次；再不行就跳过（并让调用方看到"少了一项"）
            fixed = re.sub(r",\s*([}\]])", r"\1", raw)
            try:
                out.append(json.loads(fixed))
            except Exception:
                continue
    return out


def parse_html(data: bytes, meta: dict) -> dict:
    html = (data or b"").decode(meta.get("encoding") or "utf-8", "replace")
    base_url = str(meta.get("url") or "")
    title, text, links = "", "", []
    meta_map: dict[str, str] = {}
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(html)
        texts = doc.xpath("//title/text()")
        title = _clean_text(texts[0]) if texts else ""
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.getparent().remove(bad)
        body = doc.body if doc.body is not None else doc
        text = _clean_text(body.text_content() if body is not None else "")
        for el in doc.xpath("//a[@href]"):
            href = el.get("href")
            if not href:
                continue
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            absolute = urllib.parse.urljoin(base_url, href) if base_url else href
            links.append(urllib.parse.urldefrag(absolute)[0])
    except Exception as e:
        # lxml 不可用或 HTML 太脏：退回正则（**诚实降级**，并说明降级了）
        if not title:
            m = _TITLE_RE.search(html or "")
            title = _clean_text(m.group(1)) if m else ""
        text = _clean_text(re.sub(r"<[^>]+>", " ", html or ""))
        links = extract_links(html, base_url)
        meta_map["parser_degraded"] = f"lxml 不可用或解析失败：{type(e).__name__}"
    for name, content in _META_RE.findall(html or ""):
        meta_map.setdefault(name.strip().lower(), content.strip()[:2000])
    links = list(dict.fromkeys(links))
    if not title and not text:
        return {"ok": False, "error": "HTML 里既没有 title 也没有正文（空壳页？）"}
    return {"ok": True, "title": title[:500], "text": text[:2_000_000],
            "text_length": len(text), "links": links[:5000], "meta": meta_map,
            "json_ld": extract_json_ld(html)}


SPEC = ParserSpec(name="html_text", version=VERSION, accepts=("html",),
                  parse=parse_html, order=10, note="lxml 优先，失败降级正则")

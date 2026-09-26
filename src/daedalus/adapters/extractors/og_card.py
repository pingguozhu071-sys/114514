# -*- coding: utf-8 -*-
"""示例提取器：把页面里的 **og:/twitter:/meta/JSON-LD** 归一成一张「卡片」

为什么挑这个场景当示例：
  * **离线可测**：只吃**已经捕获到本地**的 HTML 字节，一个网络请求都不发；
  * **通用**：绝大多数站点都会声明 `og:*`（分享卡片），不需要登录、不需要站点知识；
  * **有用**：`title / description / image / site_name / canonical / lang / ld_types`
    是站点结构的最小集——真正的站点提取器要做的，就是把这张卡片映射成自己的字段。

**它不是某个站点的适配器**（`docs/05`：不把站点当架构）：`match` 只看**证据**里的格式，
不绑域名。站点专有提取器应当把域名**精确**写死（`hostname in (...)`），
模糊包含会把别人的页面当自己的，而且错得很安静。

**不出网**：本模块不导入任何取流组件；从 HTML 里捞出来的 URL 一律先过
`classify_urls()`（离线判定：只留 http(s)、拒绝内网）——**只分类，不取**。
"""

from __future__ import annotations

import json
import re
import urllib.parse

from daedalus.adapters.extractors import ExtractorSpec, classify_urls

__all__ = ["SPEC", "extract", "match", "meta_props"]

VERSION = 1

_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)")
_LANG_RE = re.compile(r"<html\b[^>]*\blang\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_JSONLD_RE = re.compile(r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                        re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")


def _unquote(value: str) -> str:
    v = str(value or "").strip()
    if len(v) >= 2 and v[:1] == v[-1:] and v[:1] in ("\"", "'"):
        return v[1:-1].strip()
    return v


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def _parse_attrs(tag: str) -> dict[str, str]:
    """把标签里的 `k=v` 抠出来（引号可有可无）。只求够用且不炸，不追 HTML 规范。"""
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag or ""):
        out.setdefault(m.group(1).lower(), _unquote(m.group(2)))
    return out


def meta_props(html: str) -> dict[str, str]:
    """收集 `<meta>` 的 名称→内容（`og:` / `twitter:` / 普通 `name` 都在这里）。"""
    out: dict[str, str] = {}
    for tag in _META_RE.findall(html or ""):
        attrs = _parse_attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").strip().lower()
        content = (attrs.get("content") or "").strip()
        if key and content:
            out.setdefault(key, content[:2000])
    return out


def _first_group(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text or "")
    return _unquote(m.group(1)) if m else ""


def _canonical(html: str) -> str:
    for tag in _LINK_RE.findall(html or ""):
        attrs = _parse_attrs(tag)
        if (attrs.get("rel") or "").strip().lower() == "canonical" and attrs.get("href"):
            return attrs["href"][:2000]
    return ""


def _json_ld_types(html: str) -> list[str]:
    """`<script type="application/ld+json">` 里的 `@type`（站点结构最直接的一格）。"""
    out: list[str] = []
    for raw in _JSONLD_RE.findall(html or ""):
        try:
            obj = json.loads((raw or "").strip())
        except Exception:
            continue         # 脏 JSON-LD 跳过：上游 html_text 会把它当「少了一项」如实记下
        for node in (obj if isinstance(obj, list) else [obj]):
            if not isinstance(node, dict):
                continue
            t = node.get("@type")
            for name in (t if isinstance(t, list) else [t]):
                if isinstance(name, str) and name.strip() and name.strip() not in out:
                    out.append(name.strip()[:80])
    return out


def match(url: str, evidence: dict) -> bool:
    """认领条件：**http(s) 的 HTML 页面**（本示例是通用提取器，所以不绑域名）。

    站点专有提取器应当在这里做**精确**判定，例如：

        host = urllib.parse.urlparse(url).hostname or ""
        return host in ("site.example",) and _looks_like_html(evidence)

    证据里没有格式声明时**故意不认领**：宁可走空态，也不猜——猜错会把别人的页面当自己的。
    """
    try:
        scheme = urllib.parse.urlparse(str(url or "")).scheme.lower()
    except Exception:
        return False
    if scheme not in ("http", "https"):
        return False
    ev = dict(evidence or {})
    fmt = str(ev.get("format") or "").strip().lower()
    ctype = str(ev.get("content_type") or ev.get("mime") or "").lower()
    return fmt == "html" or "html" in ctype


def extract(raw: dict) -> dict:
    """从已捕获的 HTML 里取卡片字段（同构返回；**不发任何网络请求**）。"""
    html = str(raw.get("text") or "")
    blob = raw.get("bytes")
    if not html and isinstance(blob, (bytes, bytearray)):
        enc = str((raw.get("meta") or {}).get("encoding") or "utf-8")
        html = bytes(blob).decode(enc, "replace")
    if not html.strip():
        return {"ok": False, "error": "没有可读的 HTML（text 与 bytes 都是空的）"}

    props = meta_props(html)
    canonical = _canonical(html)
    lang = _clean(_first_group(_LANG_RE, html))
    ld_types = _json_ld_types(html)
    title = _clean(props.get("og:title") or props.get("twitter:title")
                   or _first_group(_TITLE_RE, html))
    desc = _clean(props.get("og:description") or props.get("twitter:description")
                  or props.get("description") or "")
    image = _clean(props.get("og:image") or props.get("twitter:image") or "")
    site_name = _clean(props.get("og:site_name") or "")
    page_url = _clean(props.get("og:url") or canonical)

    page = classify_urls([page_url, canonical])
    img = classify_urls([image])
    if not any((title, desc, image, site_name, lang, canonical, ld_types)):
        return {"ok": False, "error": "页面里没有 og:/meta/JSON-LD 字段（没有卡片可提取）"}
    return {
        "ok": True, "kind": "og_card", "extractor_version": VERSION,
        "title": title[:500], "description": desc[:1000], "site_name": site_name[:200],
        "lang": lang[:40], "canonical": (page["kept"][0] if page["kept"] else ""),
        "image": (img["kept"][0] if img["kept"] else ""),
        "ld_types": ld_types[:20], "meta_fields": sorted(props)[:80],
        # 丢掉的 URL 也要能被看见（安全判定不是「静默过滤」）
        "urls_dropped": (page["dropped"] + img["dropped"])[:20],
        "note": "字段来自 og:/twitter:/meta/JSON-LD；URL 只做「留 http(s)、拒内网」分类，未取",
    }


SPEC = ExtractorSpec(name="og_card", match=match, extract=extract, order=50,
                     note="通用（不绑域名）：og:/twitter:/meta/JSON-LD → 卡片字段；离线，只吃本地字节")

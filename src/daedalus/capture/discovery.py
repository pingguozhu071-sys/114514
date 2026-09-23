# -*- coding: utf-8 -*-
"""发现链（多源 Discovery Fusion）：从"已捕获的东西"里产生**新任务候选**

《可以.txt》§十的要求：发现不等于"把 `<a href>` 抓出来"，而是多源融合——
HTML 链接 / Sitemap / RSS·Atom / 内嵌 JSON / Canonical / 分页 / 文档引用 / 媒体清单。

本模块只做三件事（**不做出网**、不写库）：
    发现候选 → **规范化**（`frontier/urlcanon.py`）→ **策略过滤**（站内站外 + 闸 + robots）
调用方拿到 `accepted` 列表后自己去 `frontier.enqueue(...)`。

"站内/站外"是**显式判定**而不是随手丢弃：站外链接默认只记不追（可配 `same_site_only=False`）。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass

from daedalus.frontier.urlcanon import canonicalize

logger = logging.getLogger(__name__)

__all__ = ["DiscoveredResource", "Discovery"]


@dataclass(frozen=True)
class DiscoveredResource:
    url: str                     # 已规范化
    kind: str                    # html_link|feed_item|sitemap_url|embedded_json|canonical|media|document
    source: str                  # 从哪个产物里发现的（URL 或 artifact sha）
    detail: str = ""
    same_site: bool = True


class Discovery:
    """多源发现融合器。"""

    def __init__(self, *, same_site_only: bool = True, base_hosts: tuple[str, ...] = (),
                 fetcher=None, max_per_source: int = 5000):
        self.same_site_only = bool(same_site_only)
        self.base_hosts = tuple(h.lower() for h in base_hosts)
        self.fetcher = fetcher                     # 可选：用于闸/robots 过滤
        self.max_per_source = int(max_per_source)

    # ── 各来源 ───────────────────────────────────────────────────
    def _mk(self, raw_url: str, kind: str, source: str, detail: str = "",
            base_url: str = "") -> DiscoveredResource | None:
        absolute = urllib.parse.urljoin(base_url, raw_url) if base_url else raw_url
        canon = canonicalize(absolute, strip_www=True)
        if not canon:
            return None
        host = (urllib.parse.urlsplit(canon).hostname or "").lower()
        if self.base_hosts:
            same = any(host == h or host.endswith("." + h) for h in self.base_hosts)
        else:
            base_host = (urllib.parse.urlsplit(base_url).hostname or "").lower() if base_url else ""
            same = bool(base_host) and (host == base_host or host.endswith("." + base_host))
        return DiscoveredResource(url=canon, kind=kind, source=source[:300],
                                  detail=detail[:200], same_site=same)

    def from_html(self, links: list[str], *, base_url: str = "", source: str = "") -> list[DiscoveredResource]:
        out = []
        for href in (links or [])[: self.max_per_source]:
            r = self._mk(href, "html_link", source or base_url, base_url=base_url)
            if r:
                out.append(r)
        return out

    def from_feed(self, items: list[dict], *, base_url: str = "",
                  source: str = "") -> list[DiscoveredResource]:
        out = []
        for it in (items or [])[: self.max_per_source]:
            link = str((it or {}).get("link") or "")
            if not link:
                continue
            r = self._mk(link, "feed_item", source or base_url,
                         detail=str((it or {}).get("title") or "")[:120], base_url=base_url)
            if r:
                out.append(r)
        return out

    def from_sitemap(self, urls: list[dict] | list[str], *, source: str = "") -> list[DiscoveredResource]:
        out = []
        for it in (urls or [])[: self.max_per_source]:
            loc = it.get("loc") if isinstance(it, dict) else it
            if not loc:
                continue
            r = self._mk(str(loc), "sitemap_url", source, base_url="")
            if r:
                out.append(r)
        return out

    def from_embedded_json(self, payload, *, base_url: str = "",
                           source: str = "") -> list[DiscoveredResource]:
        """从内嵌 JSON（页面 state / JSON-LD）里挖链接。

        为什么值得单独一路：现代页面常把数据塞在 `window.__INITIAL_STATE__` / JSON-LD 里，
        DOM 里根本没有 `<a>`；漏了这条会漏掉大量真实资源。
        """
        found: list[str] = []
        def walk(node, depth=0):
            if depth > 8 or len(found) >= self.max_per_source:
                return
            if isinstance(node, dict):
                for v in node.values():
                    walk(v, depth + 1)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    walk(v, depth + 1)
            elif isinstance(node, str) and node.startswith(("http://", "https://")):
                found.append(node)
        walk(payload)
        out = []
        for u in found:
            r = self._mk(u, "embedded_json", source or base_url, base_url=base_url)
            if r:
                out.append(r)
        return out

    # ── 统一入口 ─────────────────────────────────────────────────
    def from_parse(self, parsed: dict, *, url: str = "", source: str = "") -> list[DiscoveredResource]:
        """按解析结果的形状自动分派（HTML / 订阅 / Sitemap / JSON）。"""
        parsed = dict(parsed or {})
        src = source or url
        out: list[DiscoveredResource] = []
        if isinstance(parsed.get("links"), list):
            out += self.from_html(parsed["links"], base_url=url, source=src)
        if isinstance(parsed.get("items"), list):
            out += self.from_feed(parsed["items"], base_url=url, source=src)
        if isinstance(parsed.get("urls"), list):
            out += self.from_sitemap(parsed["urls"], source=src)
        if isinstance(parsed.get("sub_sitemaps"), list):
            out += self.from_sitemap(parsed["sub_sitemaps"], source=src)
        for key in ("json_ld", "extra", "flat"):
            if parsed.get(key):
                out += self.from_embedded_json(parsed[key], base_url=url, source=src)
        return out

    # ── 策略过滤 ─────────────────────────────────────────────────
    def filter_policy(self, resources: list[DiscoveredResource]) -> tuple[list[DiscoveredResource], list[dict]]:
        """规范化之后再过两道：站内/站外 + 闸与 robots。

        返回 `(accepted, rejected)`；`rejected` 里带**可读原因**（要进台账/日志，不许静默丢）。
        """
        accepted: list[DiscoveredResource] = []
        rejected: list[dict] = []
        seen: set[str] = set()
        for r in resources:
            if r.url in seen:
                rejected.append({"url": r.url, "reason": "同一批内重复"})
                continue
            seen.add(r.url)
            if self.same_site_only and not r.same_site:
                rejected.append({"url": r.url, "reason": "站外链接（默认只记不追）"})
                continue
            if self.fetcher is not None:
                ok, why = self.fetcher.is_allowed(r.url)
                if not ok:
                    rejected.append({"url": r.url, "reason": f"策略拒绝：{why}"})
                    continue
            accepted.append(r)
        return accepted, rejected

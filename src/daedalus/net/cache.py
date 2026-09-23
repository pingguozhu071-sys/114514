# -*- coding: utf-8 -*-
"""HTTP 缓存 + 条件请求（**省流量**，不是"事实层"）

与原始层的分工（别混）：
  * **原始层**（`capture/rawstore.py`）：事实层，内容寻址、永不删、可重放；
  * **本缓存**：优化层，按 URL 存"上次响应"，用 `If-None-Match` / `If-Modified-Since`
    发条件请求；`304` 时**复用旧内容并刷新时间**（这就是"未变"的来源之一）。

三条纪律（Kiana 的坑）：
  1) **必须有数量上限**——只写不删是磁盘慢性泄漏；淘汰按"最久未用"。
  2) **过期不等于作废**：条目带 `etag/last_modified`，过期后**发条件请求**而不是直接重下。
  3) **URL 是运行态钥匙**：命中判定用哈希，元数据里的 URL **不脱敏**；
     但**打日志/上报**时用 `sanitize_url()`（"给谁看"分层）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["HttpCache", "CacheEntry"]

_KEEP_HEADERS = ("etag", "last-modified", "content-type", "content-encoding", "vary",
                 "cache-control", "expires")


@dataclass
class CacheEntry:
    url: str
    status: int
    headers: dict
    body: bytes
    fetched_at: float
    hits: int = 0

    def stale(self, ttl: float) -> bool:
        return ttl <= 0 or (time.time() - self.fetched_at) > float(ttl)

    def conditional_headers(self) -> dict:
        """条件请求头：让服务端有机会回 304（省一次全量传输）。"""
        out = {}
        etag = self.headers.get("etag") or self.headers.get("ETag")
        lm = self.headers.get("last-modified") or self.headers.get("Last-Modified")
        if etag:
            out["If-None-Match"] = str(etag)
        if lm:
            out["If-Modified-Since"] = str(lm)
        return out


class HttpCache:
    """按 URL 的磁盘缓存（上限 + LRU 淘汰 + 条件请求）。"""

    def __init__(self, root, max_entries: int = 20_000, ttl: float = 0.0,
                 compress_min: int = 1024):
        self.root = pathlib.Path(root) / "http_cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_entries = int(max_entries)
        self.ttl = float(ttl)              # 0 = 不把"新鲜度"当命中条件，一律走条件请求
        self.compress_min = int(compress_min)

    # ── 键与路径 ─────────────────────────────────────────────────
    @staticmethod
    def key_for(url: str) -> str:
        return hashlib.sha256(str(url or "").encode("utf-8", "ignore")).hexdigest()[:32]

    def _paths(self, url: str) -> tuple[pathlib.Path, pathlib.Path]:
        k = self.key_for(url)
        return self.root / f"{k}.bin.gz", self.root / f"{k}.meta.json"

    # ── 读 ───────────────────────────────────────────────────────
    def get(self, url: str) -> CacheEntry | None:
        body_p, meta_p = self._paths(url)
        if not (body_p.exists() or body_p.with_suffix("").exists()) or not meta_p.exists():
            return None
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            raw_p = body_p if body_p.exists() else body_p.with_suffix("")
            blob = raw_p.read_bytes()
            body = gzip.decompress(blob) if raw_p.suffix == ".gz" else blob
            return CacheEntry(url=meta.get("url", url), status=int(meta.get("status", 0)),
                              headers=dict(meta.get("headers") or {}),
                              body=body, fetched_at=float(meta.get("fetched_at", 0)),
                              hits=int(meta.get("hits", 0)))
        except Exception as e:
            logger.warning("缓存读取失败（当作未命中）：%s", e)
            return None

    # ── 写 ───────────────────────────────────────────────────────
    def store(self, url: str, status: int, headers: dict, body: bytes,
              fetched_at: float | None = None) -> None:
        body_p, meta_p = self._paths(url)
        try:
            blob = gzip.compress(body) if len(body) >= self.compress_min else body
            final_p = body_p if len(body) >= self.compress_min else body_p.with_suffix("")
            tmp = final_p.with_suffix(final_p.suffix + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, final_p)
            keep = {k: v for k, v in (headers or {}).items() if k.lower() in _KEEP_HEADERS}
            meta = {"url": url, "status": int(status), "headers": keep,
                    "fetched_at": float(fetched_at if fetched_at is not None else time.time()),
                    "size": len(body), "hits": 0}
            meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            self._enforce_cap()
        except Exception as e:
            logger.warning("缓存写入失败（不影响主流程）：%s", e)

    def touch(self, url: str, fresh_headers: dict | None = None) -> None:
        """304 时调用：刷新时间（可选刷新 ETag 等），复用旧内容。"""
        body_p, meta_p = self._paths(url)
        if not meta_p.exists():
            return
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            meta["fetched_at"] = time.time()
            meta["hits"] = int(meta.get("hits", 0)) + 1
            for k, v in (fresh_headers or {}).items():
                if k.lower() in _KEEP_HEADERS:
                    meta.setdefault("headers", {})[k.lower()] = v
            meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning("缓存刷新失败：%s", e)

    def _enforce_cap(self) -> None:
        """数量上限 + 按"最久未用（fetched_at 最旧）"淘汰。"""
        metas = list(self.root.glob("*.meta.json"))
        if len(metas) <= self.max_entries:
            return
        entries = []
        for p in metas:
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                entries.append((float(m.get("fetched_at", 0)), p))
            except Exception:
                entries.append((0.0, p))
        entries.sort()
        drop_n = len(entries) - self.max_entries + max(1, self.max_entries // 10)
        for _ts, p in entries[:drop_n]:
            stem = p.name[: -len(".meta.json")]
            for cand in (self.root / f"{stem}.bin.gz", self.root / f"{stem}.bin", p):
                try:
                    cand.unlink(missing_ok=True)
                except Exception:
                    pass

    def stats(self) -> dict:
        metas = list(self.root.glob("*.meta.json"))
        total = 0
        hits = 0
        for p in metas:
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                total += int(m.get("size", 0))
                hits += int(m.get("hits", 0))
            except Exception:
                continue
        return {"entries": len(metas), "max_entries": self.max_entries,
                "bytes": total, "revalidations": hits, "ttl": self.ttl,
                "root": str(self.root)}

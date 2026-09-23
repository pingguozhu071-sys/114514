# -*- coding: utf-8 -*-
"""原始层索引（指针表）：从 `raw_artifacts` 里按需取"哪条原始数据"

刻意与原始层分开：**数据文件与索引分离**（WARC/CDX 二十年的经验——索引只存指针，
查询量极大但极小；数据文件可以顺序写、可以压缩、可以慢慢归档）。
本模块只做查询，不读盘、不解析（读盘由 `RawStore.read` 负责）。

⚠️ 每个查询都是**内联字面量 SQL**（本机安全策略：`execute()` 第一参数必须是字面量，
连模块级常量也算动态）。所以列清单会重复出现——**这是刻意的，别"顺手"抽成常量**。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["ArtifactIndex", "ArtifactRow"]

_COLUMNS = ("sha256", "url", "size", "mime", "status", "fetched_at", "source", "session_id",
            "parent_task", "discovery_path", "path", "note")
_NO_LIMIT = 1_000_000_000          # 传大数代替"无上限"，好处是 SQL 只有一种形状


class ArtifactRow:
    """一条索引记录（字段与 `raw_artifacts` 对齐；`headers_json` 单独取，避免随手带出敏感头）。"""

    __slots__ = _COLUMNS

    def __init__(self, row):
        for c in _COLUMNS:
            setattr(self, c, row[c] if c in row.keys() else None)

    def __repr__(self):
        return f"ArtifactRow({str(self.sha256)[:12]}… {self.size}B {self.mime} {str(self.url)[:40]})"


class ArtifactIndex:
    """只读查询（走**独立读连接**——WAL 下读不阻塞写）。"""

    def __init__(self, db):
        self.db = db

    # ── 常用查询（每个方法各自内联字面量）────────────────────────
    def all_ids(self) -> list[str]:
        """所有内容 id（去重后的 sha256）。"""
        conn = self.db.connect(readonly=True)
        try:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT sha256 FROM raw_artifacts ORDER BY fetched_at").fetchall()]
        finally:
            conn.close()

    def by_content(self, sha256: str) -> list[ArtifactRow]:
        conn = self.db.connect(readonly=True)
        try:
            return [ArtifactRow(r) for r in conn.execute(
                "SELECT sha256, url, size, mime, status, fetched_at, source, session_id, "
                "parent_task, discovery_path, path, note FROM raw_artifacts WHERE sha256 = ? "
                "ORDER BY fetched_at", (sha256,)).fetchall()]
        finally:
            conn.close()

    def by_url(self, url: str) -> list[ArtifactRow]:
        conn = self.db.connect(readonly=True)
        try:
            return [ArtifactRow(r) for r in conn.execute(
                "SELECT sha256, url, size, mime, status, fetched_at, source, session_id, "
                "parent_task, discovery_path, path, note FROM raw_artifacts WHERE url = ? "
                "ORDER BY fetched_at", (url,)).fetchall()]
        finally:
            conn.close()

    def by_task(self, task_id: str) -> list[ArtifactRow]:
        conn = self.db.connect(readonly=True)
        try:
            return [ArtifactRow(r) for r in conn.execute(
                "SELECT sha256, url, size, mime, status, fetched_at, source, session_id, "
                "parent_task, discovery_path, path, note FROM raw_artifacts "
                "WHERE parent_task = ? ORDER BY fetched_at", (task_id,)).fetchall()]
        finally:
            conn.close()

    def since(self, ts: float, limit: int | None = None) -> list[ArtifactRow]:
        conn = self.db.connect(readonly=True)
        try:
            return [ArtifactRow(r) for r in conn.execute(
                "SELECT sha256, url, size, mime, status, fetched_at, source, session_id, "
                "parent_task, discovery_path, path, note FROM raw_artifacts "
                "WHERE fetched_at >= ? ORDER BY fetched_at LIMIT ?",
                (float(ts), int(limit) if limit else _NO_LIMIT)).fetchall()]
        finally:
            conn.close()

    def url_like(self, pattern: str, limit: int = 500) -> list[ArtifactRow]:
        conn = self.db.connect(readonly=True)
        try:
            return [ArtifactRow(r) for r in conn.execute(
                "SELECT sha256, url, size, mime, status, fetched_at, source, session_id, "
                "parent_task, discovery_path, path, note FROM raw_artifacts "
                "WHERE url LIKE ? ORDER BY fetched_at DESC LIMIT ?",
                (f"%{pattern}%", int(limit))).fetchall()]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self.db.connect(readonly=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM raw_artifacts").fetchone()
            return int(row[0] if row else 0)
        finally:
            conn.close()

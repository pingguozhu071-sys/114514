# -*- coding: utf-8 -*-
"""数据库连接工厂（SQLite）

三条硬规矩（漏一条就会出"静默丢数据"级别的事故）：
  1) **每个连接**都要设 PRAGMA：`journal_mode=WAL` / `synchronous=NORMAL` / `busy_timeout=30000`。
     **读连接也要设 `busy_timeout`**——否则写线程持锁时读连接会抛 `database is locked`，
     而调用方把它当成"任务被抢走"，成功的结果就被静默丢掉了（Kiana 踩过）。
  2) 结构变更走 `frontier/migrations.py`（`PRAGMA user_version` 驱动、幂等）；
     `CREATE TABLE IF NOT EXISTS` **补不了列**——新增列必须新开一个迁移版本段落。
  3) 本模块里所有 SQL **都是字符串字面量**（本机安全钩子的硬要求：动态 SQL 一律拦）。
     迁移脚本同理（见 `frontier/migrations.py` 的说明）。

写路径**不要**直接用这里的连接：写一律走 `store/writer.py` 的单写线程（批量提交）。
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3

from daedalus.frontier.migrations import apply_migrations, user_version

logger = logging.getLogger(__name__)

__all__ = ["Database"]


class Database:
    """一个 SQLite 文件的连接工厂（PRAGMA 在每个连接上设齐）。"""

    def __init__(self, path, migrate: bool = True):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._applied: list[str] = []
        if migrate:
            self.migrate()

    # ── 连接 ─────────────────────────────────────────────────────
    def connect(self, readonly: bool = False) -> sqlite3.Connection:
        """打开连接并设齐 PRAGMA（**读连接同样设置**，见文件头规矩 1）。"""
        if readonly and self.path.exists():
            conn = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro",
                                   uri=True, timeout=30.0, isolation_level=None)
        else:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # 逐条字面量（不做循环变量——动态 SQL 是被安全策略禁止的）
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception as e:                          # pragma: no cover - 极端环境
            logger.warning("PRAGMA 设置失败: %s", e)
        return conn

    # ── 迁移 ─────────────────────────────────────────────────────
    def migrate(self) -> list[str]:
        conn = self.connect()
        try:
            self._applied = apply_migrations(conn)
        finally:
            conn.close()
        return self._applied

    def user_version(self) -> int:
        conn = self.connect(readonly=True)
        try:
            return user_version(conn)
        finally:
            conn.close()

    # ── 关闭：WAL checkpoint（把 -wal 落进主库）───────────────────
    def close(self) -> dict:
        """收尾：`wal_checkpoint(TRUNCATE)` → 报告 `-wal` 是否清空。

        为什么必须做：WAL 模式下数据先写 `-wal`，进程被杀时主库可能是旧的。
        优雅关闭时 checkpoint 一次，下次打开就不必先回放（也便于用户自己拷库走）。
        **不抛异常**（关停链路上任何一步都不许把异常甩出来）：失败就如实回报。
        """
        out = {"wal_pages": None, "checkpointed": False, "why": ""}
        if not self.path.exists():
            out["why"] = "库文件不存在（还没建）"
            return out
        try:
            conn = self.connect()
        except Exception as e:
            out["why"] = f"{type(e).__name__}: {e}"
            return out
        try:
            try:
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                # (busy, log_pages, checkpointed_pages)
                if row is not None:
                    out["wal_pages"] = int(row[1])
                    out["checkpointed"] = int(row[0]) == 0
            except Exception as e:
                out["why"] = f"checkpoint 失败：{type(e).__name__}: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return out

    # ── 只读查询 ─────────────────────────────────────────────────
    def table_names(self) -> set[str]:
        conn = self.connect(readonly=True)
        try:
            return {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def stats(self) -> dict:
        conn = self.connect(readonly=True)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            return {"path": str(self.path), "journal_mode": str(mode).lower(),
                    "busy_timeout": int(bt), "user_version": user_version(conn),
                    "tables": len(self.table_names()),
                    "applied_now": list(self._applied)}
        finally:
            conn.close()

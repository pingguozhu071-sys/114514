# -*- coding: utf-8 -*-
"""迁移（SQLite）：DDL **内联字面量** + `PRAGMA user_version` 驱动 + 幂等

为什么 DDL 是"内联字面量"而不是读 `.sql` 文件：
本机的安全钩子（Mimosa）规定 `execute()`/`executescript()` 的 SQL 文本**必须是字符串字面量**——
读文件再执行属于"动态 SQL 执行面"，会被拦（而且这个规定是对的：动态执行的 SQL 无法被审查）。
于是：**DDL 就写在这里的字面量里**，`frontier/schema.sql` 仍保留作《开工包》的参考 DDL 对照。

纪律（照抄《开工包》的迁移规约）：
  1) 版本号由**迁移脚本自身**在末尾写（`PRAGMA user_version = N;`），运行器不构造任何 SQL；
  2) **幂等**：`IF NOT EXISTS` + 只加缺的列；`CREATE TABLE IF NOT EXISTS` **补不了列**，
     新增列必须新开一个版本号段落（`cur < 2` 再写一段）；
  3) 存量回填用 SQL 的 `UPDATE ... WHERE col IS NULL`，不要在应用层读出来改再写回；
  4) 迁移是**写路径**：只在启动阶段由主线程跑一次，不与单写线程并发。

列注释里几条"必须照抄"的规矩：
  * `tasks.target` / `raw_artifacts.url` 是**运行态钥匙**（稍后还要再请求一次）→ **不要脱敏**；
  * `pages.simhash` 入库前**必须 clamp63()**（SQLite INTEGER 上限 2^63−1，溢出会让整批回滚）；
  * `raw_artifacts.headers_json` 落库前过 `sanitize_headers`（受策略 `db_headers` 开关控制）；
  * 写盘失败的原始语句落死信（`store/deadletter.py`）。
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

__all__ = ["apply_migrations", "user_version", "LATEST_VERSION"]

LATEST_VERSION = 2


def user_version(conn: sqlite3.Connection) -> int:
    """读当前结构版本（字面量语句）。"""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """把结构补到最新（幂等）。返回本次实际应用的迁移名列表。"""
    applied: list[str] = []
    cur = user_version(conn)

    # ── 0001：初始结构 ────────────────────────────────────────────
    if cur < 1:
        conn.executescript("""
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,                 -- acquire|observe|derive|replay
    target            TEXT NOT NULL,                 -- **运行态钥匙：不脱敏**
    goal              TEXT DEFAULT '',
    scope             TEXT DEFAULT '',
    idempotency_key   TEXT NOT NULL UNIQUE,          -- kind|target 的哈希；防重复入队
    parent_id         TEXT,
    discovery_path    TEXT DEFAULT '',
    policy_json       TEXT DEFAULT '{}',
    resources_json    TEXT DEFAULT '{}',             -- **显式声明**的资源（缺省即拒绝）
    budget_json       TEXT DEFAULT '{}',
    state             TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,    -- 重试计数（**限流不计这里**）
    throttles         INTEGER NOT NULL DEFAULT 0,    -- 被限流的**独立**计数
    transitions       INTEGER NOT NULL DEFAULT 0,    -- 路由转移次数（有界）
    bytes_done        INTEGER NOT NULL DEFAULT 0,
    seconds_done      REAL    NOT NULL DEFAULT 0.0,
    leased_at         REAL,                          -- 租约开始（**心跳不改它**）
    lease_expires     REAL,                          -- 租约到期（心跳**续这个**）
    worker_id         TEXT DEFAULT '',
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_pick   ON tasks(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_lease  ON tasks(state, lease_expires);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);

CREATE TABLE IF NOT EXISTS task_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    at          REAL NOT NULL,
    stage       TEXT DEFAULT '',
    signal      TEXT NOT NULL,
    decision    TEXT DEFAULT '',
    reason      TEXT DEFAULT '',
    facts_json  TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_evidence_task ON task_evidence(task_id, at);

CREATE TABLE IF NOT EXISTS raw_artifacts (
    sha256        TEXT NOT NULL,                     -- 内容寻址（全长 sha256）
    url           TEXT NOT NULL,                     -- **运行态钥匙：不脱敏**
    size          INTEGER NOT NULL,
    mime          TEXT DEFAULT '',
    status        INTEGER DEFAULT 0,
    headers_json  TEXT DEFAULT '{}',                 -- 落库前过 sanitize_headers（受开关控制）
    fetched_at    REAL NOT NULL,
    source        TEXT DEFAULT '',                   -- 哪个环境/适配器拿到的
    session_id    TEXT DEFAULT '',
    parent_task   TEXT,
    discovery_path TEXT DEFAULT '',
    path          TEXT NOT NULL,                     -- 磁盘相对路径（sha256 分片目录）
    note          TEXT DEFAULT '',
    PRIMARY KEY (sha256, url)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON raw_artifacts(parent_task);
CREATE INDEX IF NOT EXISTS idx_artifacts_time ON raw_artifacts(fetched_at);

CREATE TABLE IF NOT EXISTS pages (
    url_hash       TEXT PRIMARY KEY,
    url            TEXT NOT NULL,                    -- 运行态钥匙：不脱敏
    fetched_at     REAL,
    status         INTEGER DEFAULT 0,
    content_hash   TEXT,
    simhash        INTEGER,                          -- **入库前必须 clamp63()**
    duplicate_of   TEXT,
    size           INTEGER DEFAULT 0,
    source_sha256  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_hash ON pages(content_hash);

CREATE TABLE IF NOT EXISTS extracted (
    url_hash     TEXT PRIMARY KEY,
    data_json    TEXT NOT NULL,
    parser       TEXT DEFAULT '',
    version      INTEGER DEFAULT 1,
    extracted_at REAL
);

CREATE TABLE IF NOT EXISTS downloads (
    media_url   TEXT PRIMARY KEY,                    -- **含签名直链：不脱敏**
    status      TEXT DEFAULT 'pending',
    file_path   TEXT DEFAULT '',
    progress    REAL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    file_size   INTEGER DEFAULT 0,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_downloads_pick ON downloads(status, created_at);

CREATE TABLE IF NOT EXISTS errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT,
    error_type    TEXT NOT NULL,
    error_message TEXT DEFAULT '',
    code          TEXT DEFAULT '',
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(error_type, at);

CREATE TABLE IF NOT EXISTS cooldowns (
    domain         TEXT PRIMARY KEY,
    until_epoch    REAL NOT NULL,
    tier           TEXT DEFAULT '',
    throttle_count INTEGER NOT NULL DEFAULT 0        -- 与限速器独立计数同步（重启继续休息）
);

CREATE TABLE IF NOT EXISTS robots_cache (
    host       TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    status     INTEGER DEFAULT 0,
    rules_json TEXT DEFAULT '{}',
    note       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS deadletter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    label           TEXT DEFAULT '',
    error           TEXT DEFAULT '',
    statements_json TEXT DEFAULT '[]',               -- 失败批次**原样**的 SQL 轨迹
    replayed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

PRAGMA user_version = 1;
""")
        applied.append("0001_init")
        logger.info("已应用迁移 0001_init")

    # ── 0002：中文全文检索（FTS5，**预分词**列）────────────────────
    # 为什么用"预分词 + unicode61"而不是指望 FTS5 自己分中文：
    # 内置分词器按 Unicode 词边界切，**中文整段会被当成一个词**（"数据采集引擎"搜不到"采集"）。
    # 所以入库时把正文按本工程的 `tokens_of()`（拉丁词 + **中文二元组**）切成 token 串存进
    # `tokens` 列，查询时用同一套分词构造**短语查询**——分词器与检索器用同一份逻辑，
    # 才能保证"能搜到"（这是 checklist G5 的判据）。
    # 注意：FTS 表是**派生层**，可由原始层重放重建（reparse 会顺带重建）。
    if cur < 2:
        conn.executescript("""
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    url_hash UNINDEXED,
    url      UNINDEXED,
    title,
    tokens,
    tokenize = 'unicode61 remove_diacritics 2'
);

PRAGMA user_version = 2;
""")
        applied.append("0002_pages_fts")
        logger.info("已应用迁移 0002_pages_fts（中文全文检索）")

    # 后续迁移照此追加：if cur < 2: conn.executescript("""... PRAGMA user_version = 2;""")

    return applied

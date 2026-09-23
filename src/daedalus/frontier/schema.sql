-- ═══════════════════════════════════════════════════════════════════
-- ⚠️ 本文件是《新工程开工包》的**参考 DDL**（保留用于对照与自测）。
--    引擎实际使用的规范 DDL 在 `frontier/migrations.py`（**内联字面量**：本机安全策略要求
--    `execute()/executescript()` 的 SQL 文本必须是字符串字面量，不许动态执行读来的脚本）。
--    表更多：tasks / task_evidence / raw_artifacts / robots_cache / deadletter / cooldowns 等。
--    **两者冲突时以 migrations.py 为准。**
-- ═══════════════════════════════════════════════════════════════════
-- 新工程队列表参考 DDL（SQLite / WAL）
-- 来源：Kiana Vnext Plus v2.19.8 的 kiana_vnext_plus/frontier.py，精简且去掉业务耦合。
--
-- 用法：新工程第一次启动时执行本文件；之后每次结构变更，**追加迁移语句**
--       （见文件末尾"迁移规约"），不要改已发布的历史语句。
--
-- 连接建立时必须执行的 PRAGMA（写在这里是为了别忘）：
--     PRAGMA journal_mode = WAL;        -- 读写不互相阻塞（单文件多线程的关键）
--     PRAGMA synchronous  = NORMAL;     -- WAL 下的常用取舍：掉电可能丢最后若干事务
--     PRAGMA busy_timeout = 30000;      -- **必设**：漏设会把"数据库锁冲突"误当成
--                                       -- "任务被别人抢走"，从而静默丢弃成功结果
-- ═══════════════════════════════════════════════════════════════════

-- ── 待抓队列 ─────────────────────────────────────────────────────
-- 关键设计：状态 + 租约 + 重试 + **独立的限流计数**
--   * status: pending / leased / done / retry / dead
--   * leased_at + lease_expires: 租约。**心跳续的是 lease_expires，不是 leased_at**
--     （改后者会破坏"租约是否还是我的"这个 CAS 闭环）
--   * throttle_count: 被限流的**独立**计数。限流**不递增** retry_count
--     ——否则任务永驻重试、主循环"没有待处理"的退出条件永不成立（Kiana 真实事故）
CREATE TABLE IF NOT EXISTS frontier (
    url_hash        TEXT PRIMARY KEY,          -- 规范化 URL 的哈希
    normalized_url  TEXT NOT NULL,             -- ⚠️ 运行态钥匙：**不要脱敏**（脱了续爬 403）
    domain          TEXT NOT NULL,
    depth           INTEGER DEFAULT 0,
    priority        INTEGER DEFAULT 5,
    status          TEXT    DEFAULT 'pending',
    scheduled_at    REAL,                      -- 允许"延迟到某时刻再抓"
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 3,
    leased_at       REAL,
    lease_expires   REAL,
    worker_id       TEXT,
    parent_hash     TEXT,                      -- 来源页（便于回溯与站点地图）
    throttle_count  INTEGER DEFAULT 0,
    created_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_frontier_pick   ON frontier (status, priority, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_frontier_domain ON frontier (domain, status);

-- ── 抓取结果（每次实际请求一条）──────────────────────────────────
CREATE TABLE IF NOT EXISTS pages (
    url_hash       TEXT PRIMARY KEY,
    status_code    INTEGER,
    content_length INTEGER,
    fetch_time     REAL,
    headers        TEXT,        -- ⚠️ 落库前必须过 sanitize_headers（Set-Cookie 等）
    content_hash   TEXT,        -- 精确指纹
    simhash        INTEGER,     -- ⚠️ 入库前必须 clamp63()：SQLite INTEGER 上限 2^63-1，
                                --    溢出会让**批量提交整批回滚**（数据静默丢失）
    duplicate_of   TEXT,        -- 近似重复指向的 url_hash
    fetched_at     REAL
);

-- ── 抽取结果 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS extracted (
    url_hash  TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,    -- ⚠️ 这里的副本应已过 sanitize_record；运行态 URL 除外
    updated_at REAL
);

-- ── 错误档案（聚合分析用，不要把错误只写进日志）─────────────────
CREATE TABLE IF NOT EXISTS errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash      TEXT,
    error_type    TEXT,         -- 分类（超时/闸拦/解析/限流/…）
    error_message TEXT,
    platform      TEXT,
    code          TEXT,         -- 平台错误码（若有）
    ts            REAL
);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors (error_type, ts);

-- ── 下载队列（媒体）──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS downloads (
    media_url   TEXT PRIMARY KEY,   -- ⚠️ 含签名的直链：**不要脱敏**（脱了下载 403）
    domain      TEXT,
    status      TEXT DEFAULT 'pending',   -- pending/downloading/completed/failed
    progress    REAL DEFAULT 0,
    file_path   TEXT,
    file_size   INTEGER,            -- 完成时写真实字节数（统计与校验共用）
    fail_count  INTEGER DEFAULT 0,
    created_at  REAL,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_downloads_pick ON downloads (status, created_at);

-- ── 域冷却 + 运行期设置 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cooldowns (
    domain      TEXT PRIMARY KEY,
    until_ts    REAL,
    reason      TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ═══════════════════════════════════════════════════════════════════
-- 迁移规约（Kiana 用 PRAGMA user_version，简单可靠）
--   1) 读 `PRAGMA user_version` 得到当前版本号 V；
--   2) 若 V < 目标版本，**按顺序**执行 V+1、V+2… 的迁移语句块；
--   3) 每个块结束时 `PRAGMA user_version = <新版本>`；
--   4) 迁移必须**幂等**且带**存量数据回填**（新增列要给老行补默认值）。
--
-- ⚠️ 两个必踩的坑：
--   * `CREATE TABLE IF NOT EXISTS` **补不了新列** —— 加列必须走迁移脚本；
--   * 不要在迁移里用"读出来 → 在应用层改 → 写回"，大表会很慢且可能中断；
--     用 SQL 直接 `UPDATE ... WHERE new_col IS NULL` 回填。
--
-- 示例（把这一块作为 v2 迁移，新工程里按需改）：
--   ALTER TABLE pages ADD COLUMN rendered INTEGER DEFAULT 0;
--   UPDATE pages SET rendered = 0 WHERE rendered IS NULL;
--   PRAGMA user_version = 2;
-- ═══════════════════════════════════════════════════════════════════

-- ── 写库模式（与线程模型配套）────────────────────────────────────
-- * **单写线程 + 批提交**：每 N 行（建议 500–2000）或每 1 秒 flush 一次。
--   SQLite 的写是串行资源，多线程写只会抢锁；批提交才是提速点。
-- * 批量提交前**逐条钳值与清洗**（63 位、脱敏），一条坏数据会拖垮整批。
-- * 提交失败时把原始语句落到死信文件（`deadletter.jsonl`）——**不要静默丢数据**。
-- * 读取用独立连接（WAL 下读不阻塞写），同样要设 busy_timeout。

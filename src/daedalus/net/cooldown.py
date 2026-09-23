# -*- coding: utf-8 -*-
"""域冷却的持久化：**重启后继续休息**

为什么必须有：Kiana 的冷却状态只活在内存里，重启即忘——于是"被拦截 → 降速休息"在重启后
立刻失效，任务马上又去撞同一堵墙。库里有 `cooldowns` 表，正好承接这件事。

两条方向：
  * `load_cooldowns(db, limiter)`：启动时把"还在冷却期"的域灌回限速器（`limiter.restore`）；
  * `save_cooldowns(db, limiter)`：运行期（或退出时）把当前冷却状态落库（`limiter.snapshot`）。

⚠️ 所有 SQL 都是**内联字面量**（本机安全策略）；参数一律绑定。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

__all__ = ["save_cooldowns", "load_cooldowns", "purge_expired"]


def save_cooldowns(db, limiter, tier: str = "") -> int:
    """把限速器里"还在休息"的域写进 `cooldowns` 表（UPSERT）。返回写入条数。"""
    snap = limiter.snapshot()
    if not snap:
        return 0
    now = time.time()
    conn = db.connect()
    try:
        n = 0
        for domain, st in snap.items():
            rest = float(st.get("rest_for", 0.0) or 0.0)
            if rest <= 0:
                continue
            conn.execute(
                "INSERT INTO cooldowns (domain, until_epoch, tier, throttle_count) "
                "VALUES (?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
                "until_epoch=excluded.until_epoch, tier=excluded.tier, "
                "throttle_count=excluded.throttle_count",
                (str(domain), now + rest, str(tier), int(st.get("throttle_count", 0) or 0)))
            n += 1
        return n
    finally:
        conn.close()


def load_cooldowns(db, limiter) -> int:
    """启动时把仍在冷却期的域灌回限速器。返回恢复条数。"""
    conn = db.connect(readonly=True)
    try:
        rows = conn.execute(
            "SELECT domain, until_epoch, throttle_count FROM cooldowns WHERE until_epoch > ?",
            (time.time(),)).fetchall()
    finally:
        conn.close()
    state = {}
    for r in rows:
        rest = float(r["until_epoch"]) - time.time()
        if rest > 0:
            state[r["domain"]] = {"rest_for": rest,
                                  "throttle_count": int(r["throttle_count"] or 0)}
    n = limiter.restore(state) if state else 0
    if n:
        logger.info("从库里恢复了 %d 个域的冷却状态（重启后继续休息）", n)
    return n


def purge_expired(db) -> int:
    """清理已过期的冷却记录（表不该无限增长）。返回删除条数。"""
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM cooldowns WHERE until_epoch <= ?", (time.time(),))
        return int(cur.rowcount or 0)
    finally:
        conn.close()

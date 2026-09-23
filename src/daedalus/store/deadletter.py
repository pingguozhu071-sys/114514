# -*- coding: utf-8 -*-
"""死信：写盘失败的**原始语句**原样落盘，可事后取证与人工修复。

为什么必须有它：Kiana 上出现过"整批写入失败被静默丢弃"——数据没了还没人知道。
凡是"写不进去"的，要么进死信文件（`.jsonl`），要么进死信表（`deadletter`），**不许静默**。

⚠️ **不做自动重放**（设计选择，不是偷懒）：
  * 记录下来的语句带**当时那一刻**的参数值，自动重放**不幂等**（会重复写入、或撞唯一约束）；
  * 在本进程里执行"历史 SQL 字符串"本身就是个动态执行面——不该有。
  所以 `export_sql()` 把语句导出成一个 `.sql` 文件，**人工核对后手动执行**。

落盘形态（每行一条 JSON）：
    {"at": 1699999999.0, "label": "frontier.commit", "error": "IntegrityError: ...",
     "statements": ["UPDATE tasks SET ...", "INSERT INTO raw_artifacts ..."]}
`statements` 是**失败那一刻连接上执行过的 SQL 轨迹**（由 writer 用 trace 回调抓取）。
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

__all__ = ["DeadLetter", "DeadLetterRecord"]

# 注意：这里的 SQL **不抽成模块级常量**。本工程的安全钩子（与本仓库 tools/lint.py 的
# `dynamic-sql` 规则）要求 `execute()` 的第一个实参是**字符串字面量**——抽成常量会让
# "参数化语句"与"拼出来的语句"在形状上无法区分，钩子只能一律拦下。
# 代价是这一行在下面出现两次；换来的是**结构上不可能出现动态 SQL**。


@dataclass
class DeadLetterRecord:
    label: str
    error: str
    statements: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({"at": self.at, "label": self.label, "error": self.error,
                           "statements": list(self.statements)}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "DeadLetterRecord":
        d = json.loads(text or "{}")
        return cls(label=d.get("label", ""), error=d.get("error", ""),
                   statements=list(d.get("statements") or []), at=d.get("at", 0.0))


class DeadLetter:
    """死信落盘（JSONL，追加式）+ 导出可人工执行的 `.sql`。"""

    def __init__(self, path=None, db=None):
        self.path = pathlib.Path(path) if path else None
        self.db = db                                   # 可选：同时写进 deadletter 表

    # ── 写 ───────────────────────────────────────────────────────
    def write(self, label: str, error: str, statements: list[str],
              conn=None) -> DeadLetterRecord:
        """记一条死信。

        `conn`：**单写线程请务必传自己的连接**——它在批事务里，另开连接会被
        `database is locked` 卡住 `busy_timeout`（30 秒），把整条写路径拖停。
        （本工程的门禁 C2 就是这么把这个坑抓出来的。）不传则用 `self.db` 自己开连接
        （只适合写线程之外的调用点）。
        """
        rec = DeadLetterRecord(label=label, error=str(error), statements=list(statements or []))
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fp:
                    fp.write(rec.to_json() + "\n")
            except Exception as e:                     # 死信本身也不能把进程带崩
                logger.error("死信落盘失败（%s）：%s", self.path, e)
        if conn is not None:
            try:
                conn.execute("INSERT INTO deadletter(at, label, error, statements_json) "
                             "VALUES (?,?,?,?)",
                             (rec.at, rec.label, rec.error,
                              json.dumps(rec.statements, ensure_ascii=False)))
            except Exception as e:
                logger.error("死信入库失败（写线程连接）：%s", e)
        elif self.db is not None:
            try:
                c2 = self.db.connect()
                try:
                    c2.execute("INSERT INTO deadletter(at, label, error, statements_json) "
                               "VALUES (?,?,?,?)",
                               (rec.at, rec.label, rec.error,
                                json.dumps(rec.statements, ensure_ascii=False)))
                finally:
                    c2.close()
            except Exception as e:
                logger.error("死信入库失败：%s", e)
        logger.warning("死信 +1（%s）：%s", label, str(error)[:160])
        return rec

    # ── 读 / 导出 ────────────────────────────────────────────────
    def read(self, limit: int | None = None) -> list[DeadLetterRecord]:
        if self.path is None or not self.path.exists():
            return []
        out: list[DeadLetterRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(DeadLetterRecord.from_json(line))
            except Exception:
                continue
        return out[:limit] if limit else out

    def export_sql(self, dest, limit: int | None = None) -> tuple[int, int]:
        """把死信里的语句导出成 `.sql`（**供人工核对后手动执行**）。

        返回 (导出语句条数, 记录条数)。每条前带一行注释标明来源与错误，便于对照。
        """
        dest = pathlib.Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        recs = self.read(limit)
        n_stmt = 0
        lines = ["-- Daedalus 死信导出（人工核对后手动执行；自动重放不幂等，故不自动做）",
                 f"-- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"-- 记录条数: {len(recs)}", ""]
        for i, rec in enumerate(recs, 1):
            lines.append(f"-- #{i} label={rec.label} error={rec.error[:120]}")
            for sql in rec.statements:
                lines.append(f"{sql.rstrip().rstrip(';')};")
                n_stmt += 1
            lines.append("")
        dest.write_text("\n".join(lines), encoding="utf-8")
        logger.info("死信已导出：%s（%d 条语句 / %d 条记录）", dest, n_stmt, len(recs))
        return n_stmt, len(recs)

    def count(self) -> int:
        return len(self.read())

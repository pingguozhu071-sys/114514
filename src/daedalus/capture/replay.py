# -*- coding: utf-8 -*-
"""离线重放（`reparse`）：**用新解析器把历史原始数据重新解释一遍**，不重新联网

这是"事实不会丢"的兑现方式（《可以.txt》§三）：今天不会解析的格式，明天写了解析器，
把原始层扫一遍就行——不需要回访站点。

三条硬要求：
  1) **绝不联网**：本模块**不导入**任何取流组件（`net/` 下的东西一个都不碰），
     `tests/gates/s3_gate.py` 会扫描这一点；
  2) **不写坏原始层**：重放只读原始字节、只写派生记录（原始层是不可变的）；
  3) **可增量**：支持按时间 / 父任务 / URL 片段过滤，也支持只重放"上次解析失败"的。

去重与增量（本模块与 `capture`、`understand` 的接缝）：
  * **精确**：内容哈希 = 地面真值（`content_hash`）；同 URL 同哈希 → `unchanged`；
    同 URL 不同哈希 → `updated`；新哈希 → `new`；
  * **近似**：`simhash` + 汉明距离（入库前 `clamp63`），命中就把新记录标 `duplicate_of`；
  * **"未变"是一等结果**：不是"没抓到"，要在台账里记 `unchanged`。
"""

from __future__ import annotations

import logging
import time

from daedalus.frontier.dedup import NEAR_DUP_THRESHOLD, clamp63, content_hash, hamming, simhash64
from daedalus.understand.ledger import ChangeLedger
from daedalus.understand.normalize import normalize_record
from daedalus.understand.quality import check as quality_check

logger = logging.getLogger(__name__)

__all__ = ["Reparser", "dedup_verdict"]


def dedup_verdict(new_hash: str, new_sim: int, known: dict[str, dict],
                  near_threshold: int = NEAR_DUP_THRESHOLD) -> tuple[str, str | None, str]:
    """判定"这条内容相对已知内容是什么状态"。

    `known`：`{content_hash: {"simhash": int, "url": str}}`（本工程用它做精确+近似两级判定）。

    返回 `(状态, duplicate_of, 原因)`；状态取自台账五态（这里只会给 new/unchanged/updated 之一）。
    """
    if new_hash in known:
        prev = known[new_hash]
        if str(prev.get("url") or "") == "":
            return "unchanged", new_hash, "内容哈希与已有记录相同"
        return "unchanged", new_hash, f"内容哈希与已有记录相同（{str(prev.get('url'))[:60]}）"
    for h, info in known.items():
        prev_sim = info.get("simhash")
        if prev_sim is None:
            continue
        d = hamming(int(new_sim), clamp63(int(prev_sim)))
        if d <= int(near_threshold):
            return ("new", h, f"近似重复（与 {str(info.get('url'))[:60]} 汉明距离 {d}）")
    return "new", None, "内容哈希未见过"


class Reparser:
    """离线重放器：原始层 → （探测 → 解析 → 归一 → 质量闸）→ 派生记录。"""

    def __init__(self, store, registry, index, ledger: ChangeLedger | None = None,
                 min_score: float | None = None):
        self.store = store                 # capture.rawstore.RawStore
        self.registry = registry           # understand.registry.ParserRegistry
        self.index = index                 # capture.index.ArtifactIndex
        self.ledger = ledger or ChangeLedger()
        self.min_score = min_score

    # ── 主入口 ───────────────────────────────────────────────────
    def run(self, *, since: float | None = None, parent_task: str | None = None,
            url_like: str | None = None, limit: int | None = None,
            deliver=None, known: dict[str, dict] | None = None) -> dict:
        """重放一批原始数据。`deliver(record, artifact_row)` 由调用方决定"落到哪"。

        **一份原始数据可能产出多条记录**：订阅（RSS/Atom）与站点地图天然是"一批条目"，
        这里会按条目展开（每条约一条记录），而不是硬塞成"一个页面"。
        """
        if parent_task:
            rows = self.index.by_task(parent_task)
        elif url_like:
            rows = self.index.url_like(url_like, limit=limit or 500)
        else:
            rows = self.index.since(since if since is not None else 0.0, limit=limit)
        stats = {"scanned": 0, "bytes": 0, "parsed_ok": 0, "parsed_failed": 0,
                 "records": 0, "accepted": 0, "rejected": 0, "delivered": 0,
                 "states": {}, "started_at": time.time()}
        known = dict(known or {})

        for row in rows:
            stats["scanned"] += 1
            try:
                blob = self.store.read(row.sha256)
            except Exception as e:
                stats["parsed_failed"] += 1
                self.ledger.record("failed", url=row.url, detail=f"原始层读取失败：{e}")
                continue
            stats["bytes"] += len(blob)

            meta = {"url": row.url, "content_type": row.mime, "status": row.status}
            out = self.registry.parse(blob, meta=meta, url=row.url)
            if not out.get("ok"):
                stats["parsed_failed"] += 1
                self.ledger.record("failed", url=row.url,
                                   detail=str(out.get("error"))[:200])
                continue
            stats["parsed_ok"] += 1

            # 展开：订阅/站点地图 → 每条目一条记录；页面/JSON/文本 → 单条
            candidates = list(_expand(out, row))
            stats["records"] += len(candidates)
            for raw_rec in candidates:
                rec = normalize_record(dict(raw_rec, url=raw_rec.get("url") or row.url,
                                            parser=out.get("parser", ""),
                                            parser_version=out.get("parser_version", 0),
                                            format=out.get("format", "")))
                q = quality_check(rec, min_score=self.min_score) if self.min_score is not None \
                    else quality_check(rec)
                if not q["accepted"]:
                    # **拒收 ≠ 重试**：记为 failed（附可读原因），并视为"已处理"
                    stats["rejected"] += 1
                    self.ledger.record("failed", url=rec["url"] or row.url,
                                       detail=f"质量闸拒收（{q['score']}）："
                                              f"{'; '.join(q['reasons'])[:160]}")
                    continue
                stats["accepted"] += 1

                ch = content_hash(rec.get("text") or rec.get("title") or "")
                sim = clamp63(simhash64(rec.get("text") or ""))
                state, dup_of, why = dedup_verdict(ch, sim, known)
                record = dict(rec, content_hash=ch, simhash=sim, duplicate_of=dup_of,
                              quality=q["score"], quality_reasons=q["reasons"],
                              source_sha256=row.sha256, **({"parent_task": row.parent_task}
                                                           if row.parent_task else {}))
                if deliver is not None:
                    try:
                        deliver(record, row)
                        stats["delivered"] += 1
                    except Exception as e:
                        self.ledger.record("failed", url=rec["url"] or row.url,
                                           detail=f"投递失败：{e}")
                        continue
                known[ch] = {"simhash": sim, "url": rec["url"] or row.url}
                self.ledger.record(state, url=rec["url"] or row.url, detail=why)
                stats["states"][state] = stats["states"].get(state, 0) + 1
        stats["elapsed"] = time.time() - stats["started_at"]
        return stats


def _expand(out: dict, row) -> list[dict]:
    """把一次解析的产物**展开成若干条候选记录**。

    * 订阅（items）→ 每条约一条；`url` 用条目的 link；
    * 站点地图（urls）→ 每条 loc 一条；
    * 其它（页面/JSON/文本）→ 整条算一条。
    """
    items = out.get("items")
    if isinstance(items, list) and items:
        return [{"url": it.get("link") or "", "title": it.get("title") or "",
                 "text": it.get("summary") or it.get("title") or "",
                 "published_at": it.get("published") or "", "site": out.get("feed_title", "")}
                for it in items]
    urls = out.get("urls")
    if isinstance(urls, list) and urls:
        return [{"url": it.get("loc") or "", "title": it.get("loc") or "",
                 "text": " ".join(str(it.get(k) or "") for k in ("lastmod", "changefreq", "priority")).strip(),
                 "published_at": it.get("lastmod") or ""} for it in urls]
    return [{k: v for k, v in out.items() if k not in ("ok", "error")}]

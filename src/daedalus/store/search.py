# -*- coding: utf-8 -*-
"""中文全文检索（FTS5 + **预分词**）

为什么是"预分词"而不是让 FTS5 自己分：
    FTS5 内置的 `unicode61` 按 Unicode 词边界切，**中文整段会被当成一个词**——
    「数据采集引擎」这行文本，用「采集」是搜不到的（这正是 checklist G5 要验的场景）。
    所以入库时用本工程的 `tokens_of()`（拉丁词 + **中文二元组**）把正文切成 token 串存进
    `pages_fts.tokens`；查询时用**同一套分词**构造短语查询。分词器与检索器共用一份逻辑，
    才能保证"输入中文词能命中"。

三条纪律：
  * FTS 表是**派生层**：可由原始层重放重建（`reparse` 会重写它），所以它坏了不算数据丢失；
  * 索引写入与 `pages` 写入**在同一个事务**里（都走单写线程的连接），不会出现"页在索引不在"；
  * 索引失败**不带塌调用方**（原始数据与 `pages` 才是真相），但必须留痕（记 warning）。
"""

from __future__ import annotations

import logging

from daedalus.frontier.dedup import tokens_of

logger = logging.getLogger(__name__)

__all__ = ["index_page", "search", "delete_page", "SearchHit"]


class SearchHit(dict):
    """一条命中（dict 子类：直接当 JSON 序列化用）。"""


def _token_blob(*parts: str, max_tokens: int = 40000) -> str:
    """把若干段文本切成 FTS 用的 token 串（空格分隔，按序保留二元组顺序）。"""
    toks: list[str] = []
    for p in parts:
        if p:
            toks.extend(tokens_of(str(p), max_tokens=max_tokens))
    return " ".join(toks[:max_tokens])


def index_page(conn, *, url_hash: str, url: str = "", title: str = "",
               text: str = "") -> bool:
    """写入/更新索引（**必须在写线程的连接上调用**，与 `pages` 的写同一事务）。

    FTS5 没有主键，所以先按 `url_hash` 删旧行再插（等价于 upsert）。
    """
    try:
        conn.execute("DELETE FROM pages_fts WHERE url_hash = ?", (str(url_hash),))
        conn.execute(
            "INSERT INTO pages_fts (url_hash, url, title, tokens) VALUES (?,?,?,?)",
            (str(url_hash), str(url)[:1000], _token_blob(title), _token_blob(text)))
        return True
    except Exception as e:
        logger.warning("全文索引写入失败（不影响原始层）：%s", e)
        return False


def delete_page(conn, *, url_hash: str) -> None:
    conn.execute("DELETE FROM pages_fts WHERE url_hash = ?", (str(url_hash),))


def _match_expr(query: str) -> str:
    """把用户查询变成 FTS5 的 MATCH 表达式。

    做法：用**同一套分词**切开，再用短语查询（`"a b c"`）——二元组必须按序相邻才算命中，
    精度比"任意 token 的 OR" 高得多；多个词之间用 `AND` 连接（多词查询要求全部出现）。
    """
    groups: list[str] = []
    for word in str(query or "").split():
        toks = tokens_of(word)
        if not toks:
            continue
        quoted = " ".join(t.replace('"', '""') for t in toks)
        groups.append(f'"{quoted}"')
    return " AND ".join(groups)


def search(conn, query: str, *, limit: int = 20, offset: int = 0) -> list[SearchHit]:
    """检索（返回 `[{url_hash, url, title, score}]`）。

    查不到就返回空列表（**不是错误**）；查询串分词后为空也返回空（不猜）。
    """
    expr = _match_expr(query)
    if not expr:
        return []
    lim = max(1, min(int(limit), 200))
    off = max(0, int(offset))
    try:
        rows = conn.execute(
            "SELECT url_hash, url, title, bm25(pages_fts) AS score FROM pages_fts "
            "WHERE pages_fts MATCH ? ORDER BY score LIMIT ? OFFSET ?",
            (expr, lim, off)).fetchall()
    except Exception as e:
        logger.warning("全文检索失败（%s）：%s", query, e)
        return []
    return [SearchHit(url_hash=r["url_hash"], url=r["url"], title=r["title"] or "",
                      score=round(float(r["score"] or 0.0), 4)) for r in rows]


# ── 为什么这里**没有** `rebuild_from_pages()` ──────────────────────
# 曾经写过一个"从 pages 重建索引"的函数，然后发现它是个**错误设计**：
# `pages` 里只有 URL 与指纹、**没有正文**（正文在原始层），所以"清空索引再从 pages 重建"=
# 把全文检索能力**清掉**（实测重建后一条都搜不到）。正确的重建路径只有一条：
#   **从原始层重扫**——`daedalus reparse` 会用当前解析器重新解析原始字节，
#   并把新的正文重新索引（`core/app.py` 的 deliver 分支里调 `index_page`）。
# 所以：索引是派生层，重建它靠重放，不靠 pages 表。

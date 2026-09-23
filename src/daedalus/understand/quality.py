# -*- coding: utf-8 -*-
"""质量闸：空壳页 / 垃圾页**拒收**，而不是让它们污染下游

两条与"重试"划清界限的语义（都是 Kiana 的教训）：
  * **拒收 ≠ 重试**：质量不合格是"这页就长这样"，重试一百次也一样 →
    拒收后要把该 URL **标记为已处理**（否则它会反复进队，把队列堵死）；
  * **拒收要有原因**：返回 `reasons` 列表（可读），并计入变更台账的
    `failed`（解析失败）而不是"没抓到"。

评分（0–1，默认阈值 `0.12`，可配）：
    * 正文长度（400 字以上给满分）—— 0.45
    * 必填字段齐全度（title / url / text 至少两样）—— 0.35
    * 空壳惩罚（标题+正文都极短、或正文只有导航词）—— 最多扣 0.4
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_MIN_SCORE", "score", "check", "looks_like_shell"]

DEFAULT_MIN_SCORE = 0.12
_BOILERPLATE = ("登录", "注册", "首页", "导航", "菜单", "javascript", "请开启", "enable javascript",
                "cookie", "跳转中", "验证中", "404", "not found", "访问被拒绝", "access denied")


def looks_like_shell(text: str, title: str = "") -> bool:
    """是不是空壳：正文极短，或只是导航/验证/错误页的套路词。"""
    t = (text or "").strip()
    if len(t) >= 400:
        return False
    low = (t + " " + (title or "")).lower()
    hits = sum(1 for w in _BOILERPLATE if w in low)
    return hits >= 2 or (len(t) < 80 and hits >= 1) or not t


def score(record: dict, text: str | None = None) -> tuple[float, list[str]]:
    """返回 `(分数, 原因列表)`；分数越高越像"有内容的页面"。"""
    rec = dict(record or {})
    body = str(text if text is not None else (rec.get("text") or ""))
    title = str(rec.get("title") or "")
    url = str(rec.get("url") or "")
    reasons: list[str] = []

    length_score = min(1.0, len(body) / 400.0) * 0.45
    if len(body) < 100:
        reasons.append(f"正文过短（{len(body)} 字）")

    present = [bool(title.strip()), bool(body.strip()), bool(url.strip())]
    completeness = (sum(present) / 3.0) * 0.35
    if sum(present) < 2:
        reasons.append(f"必填字段不足（title={bool(title)}, text={bool(body)}, url={bool(url)}）")

    penalty = 0.0
    if looks_like_shell(body, title):
        penalty = 0.4
        reasons.append("疑似空壳/验证页/错误页（套路词命中或正文极短）")

    total = max(0.0, length_score + completeness - penalty)
    if not reasons:
        reasons.append(f"正文 {len(body)} 字、字段齐全")
    return min(1.0, total), reasons


def check(record: dict, text: str | None = None,
          min_score: float = DEFAULT_MIN_SCORE) -> dict:
    """质量闸。返回 `{"score", "accepted", "reasons", "threshold"}`。"""
    s, reasons = score(record, text)
    accepted = s >= float(min_score)
    return {"score": round(s, 4), "accepted": accepted, "reasons": reasons,
            "threshold": float(min_score)}

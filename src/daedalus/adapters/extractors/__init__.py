# -*- coding: utf-8 -*-
"""站点提取器：`docs/08-扩展路径.md` §一「加一个来源 = 加一个文件」的**落地**

机制与解析器侧**完全同一份**（`understand.registry.discover_specs` 扫目录 → 读模块级
`SPEC`），这里不抄第二份扫描器：两份实现必然漂移，而漂移出来的那一份正是审计里
「插件面只有一半是真的」那种形状。

    # adapters/extractors/<site>.py
    def match(url, evidence) -> bool: ...        # 精确域匹配（别用模糊包含）
    def extract(raw: dict) -> dict: ...          # 同构返回 {"ok": bool, "error"?: str, ...}
    SPEC = ExtractorSpec(name="<site>", match=match, extract=extract, order=50)

`raw` 的键是契约（用 `raw_payload()` 造，见它的文档）：`url` / `bytes` / `text` / `mime` /
`meta`。`evidence` 是**上游已知的事实**（`format` / `content_type` / `status`…），
`match` 只该看它和 URL，不要在里面解析正文（那等于每次候选判定都做一次真解析）。

四条红线（`docs/08` §一、§三 与 `docs/07`）：
  1) **提取器不出网**：只吃**已经捕获到本地**的字节/文本。要再取一次远端 URL 的，必须过
     `net/ssrf_gate.py` 这唯一咽喉（逐跳闸 + 礼貌预算），不许在适配器里自己开一条出口；
  2) **从页面里捞到的 URL 是不可信输入**：先过 `classify_urls()`（离线判定：只留 http(s)、
     拒绝内网/环回/保留地址），再决定要不要取；
  3) **站点专有逻辑永远不进核心**（`docs/05`：不把站点当架构）；
  4) **空态是一等结果**：没有任何提取器认领 → `ok=False` + `reason` = `没有匹配的提取器`
     （不是 `None`、不抛异常 —— 上层据此走通用解析器）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from daedalus.understand.registry import SpecScan, discover_specs

logger = logging.getLogger(__name__)

__all__ = ["EMPTY_REASON", "EXTRACTOR_PACKAGE", "ExtractorSpec", "classify_urls",
           "load_extractors", "raw_payload", "run_extractors", "scan_extractors"]

EXTRACTOR_PACKAGE = "daedalus.adapters.extractors"
EMPTY_REASON = "没有匹配的提取器"


@dataclass(frozen=True)
class ExtractorSpec:
    """一个来源提取器：`match` 认领 URL，`extract` 从**已捕获的本地字节**里取值。

    * `match(url, evidence)` 必须**判定得出来**（精确域名/精确路径），别用模糊包含：
      模糊匹配会把别人的页面当自己的，而且错得很安静；
    * `extract(raw)` 返回**同构字典**：`{"ok": bool, "error"?: str, ...payload}`；
      一个字段都取不到时要 `ok=False` + 可读原因，不要「空成功」；
    * `order` 越小越先试（站点专有的排在前，通用的排在后）。
    """

    name: str
    match: Callable[[str, dict], bool]
    extract: Callable[[dict], dict]
    order: int = 100
    note: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("ExtractorSpec.name 不能为空（派生记录要记下是谁提取的）")
        for attr in ("match", "extract"):
            if not callable(getattr(self, attr, None)):
                raise ValueError(f"ExtractorSpec.{attr} 必须是可调用对象（{self.name}）")


def raw_payload(src: bytes | str, *, url: str = "", mime: str = "",
                meta: dict | None = None) -> dict:
    """把「一份已经捕获到本地的东西」包成 `extract(raw)` 认得的形状（**契约唯一入口**）。

    `raw` 的键（提取器可以假定它们都在）：
      * `url`：来源地址（可能为空：本地文件就没有）；
      * `bytes`：原始字节（可能 `None`：调用方只拿到了文本）；
      * `text`：文本形态（可能空串）；
      * `mime` / `meta`：捕获时记下的**声明**信息 —— 声明可能错，只当提示，不当事实。
    """
    return {"url": str(url or ""), "bytes": src if isinstance(src, bytes) else None,
            "text": src if isinstance(src, str) else "",
            "mime": str(mime or ""), "meta": dict(meta or {})}


def classify_urls(candidates) -> dict:
    """把页面里捞到的 URL 分成「可以交给咽喉去取」与「必须丢掉」两堆（**离线**判定）。

    * 只留 `http`/`https`：`javascript:`/`data:`/`file:`/`mailto:` 一律丢掉；
    * **拒绝内网**：直接用 `net/ssrf_gate.is_private_url(..., dns_check=False)` —— 复用
      唯一那道闸的判定，不抄第二份（抄出来的那份迟早与闸不一致）；
    * `dns_check=False` 是**故意**的：提取器只做分类，DNS 解析留给真正的取流路径
      （那里有 TTL 缓存与逐跳校验，`net/ssrf_gate.py` 的注释解释了为什么在那里做）。

    返回 `{"kept": [...], "dropped": [{"url":…, "why":…}, …]}` —— **丢掉的也要能被看见**。
    """
    from daedalus.net.ssrf_gate import is_private_url      # 懒加载：不改提取器的 import 面

    kept: list[str] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for raw_url in candidates or []:
        u = str(raw_url or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        if is_private_url(u, dns_check=False):
            dropped.append({"url": u[:300], "why": "非 http(s) 或内网/环回/保留地址"})
        else:
            kept.append(u[:2000])
    return {"kept": kept, "dropped": dropped}


def scan_extractors(package: str = EXTRACTOR_PACKAGE) -> SpecScan:
    """扫描提取器包（**与解析器侧同一个通用扫描器**：`discover_specs`）。"""
    scan = discover_specs(package, expect=ExtractorSpec)
    if scan.errors:
        logger.warning("提取器扫描有 %d 个模块没能注册（%s）：%s",
                       len(scan.errors), package, scan.why())
    return scan


def load_extractors(package: str = EXTRACTOR_PACKAGE) -> list[ExtractorSpec]:
    """按 `(order, name)` 给出可用提取器清单 —— **加一个文件就多一个来源**。"""
    return _ordered(spec for _module, spec in scan_extractors(package).specs)


def _ordered(specs) -> list[ExtractorSpec]:
    """统一排序口径：`order` 升序、同序按 `name`（候选顺序必须可解释）。"""
    return sorted(specs, key=lambda s: (int(s.order), str(s.name)))


def run_extractors(url: str, raw: dict, *, evidence: dict | None = None,
                   extractors: list[ExtractorSpec] | None = None,
                   package: str = EXTRACTOR_PACKAGE) -> dict:
    """按 `order` 依次找「认领这条 URL」的提取器并提取，返回**同构字典**。

    * **空态是一等结果**：一条都没认领 → `ok=False 且 reason 为 没有匹配的提取器`；
    * 认领了但都失败 → `ok=False` + `reason` + `tried`（试过哪些，可解释）；
    * `match` 自己抛异常不该塌上层：`logger.warning` 记一行，并把
      `match_errors` 放进返回值（**可见**，不是悄悄当成「不是我」）。

    `extractors` 显式给定时也**照样按 `order` 排**（调用方不用先自己排）。
    """
    ev = dict(raw.get("meta") or {})
    ev.update(evidence or {})
    target = str(url or raw.get("url") or "")
    pool = load_extractors(package) if extractors is None else _ordered(extractors)

    match_errors: list[dict] = []
    claimed: list[ExtractorSpec] = []
    for spec in pool:
        try:
            if spec.match(target, ev):
                claimed.append(spec)
        except Exception as e:
            logger.warning("提取器 %s 的 match 抛异常：%s: %s", spec.name, type(e).__name__, e)
            match_errors.append({"extractor": spec.name, "error": f"{type(e).__name__}: {e}"})
    extra = {"match_errors": match_errors} if match_errors else {}

    if not claimed:
        return {"ok": False, "reason": EMPTY_REASON, "url": target, "tried": [], **extra}

    tried: list[str] = []
    for spec in claimed:
        tried.append(spec.name)
        try:
            out = spec.extract(dict(raw, url=target))
        except Exception as e:
            logger.warning("提取器 %s 的 extract 抛异常：%s: %s", spec.name, type(e).__name__, e)
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(out, dict) and out.get("ok"):
            return dict(out, ok=True, url=target, extractor=spec.name,
                        extractor_order=int(spec.order), tried=tried, **extra)
    return {"ok": False, "reason": f"认领的提取器都失败了（试过 {tried}）",
            "url": target, "extractor": "", "tried": tried, **extra}

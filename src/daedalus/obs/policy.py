# -*- coding: utf-8 -*-
"""脱敏策略：**按数据类型分开关** + 透明 + 不谎报（落地 `docs/11-设计补充`）。

一句话：脱敏不是一个总开关，而是**多个出口各自的开关**；开关状态必须**可查**，
任何一类被关掉都要**说出来**（日志 INFO + UI 徽章），绝不静默。

七类出口（`KINDS`）与默认值：

| 类型 | 默认 | 作用面 |
|---|---|---|
| `log_url` | 脱 | 日志行里的 URL 查询参数（`?token=` 等） |
| `log_headers` | 脱 | 日志里的请求/响应头（Cookie/Authorization） |
| `log_text` | 脱 | 日志正文文本（手机号/邮箱/IP） |
| `export_record` | **不脱** | 导出副本（整条记录）——个人使用：导出要原始数据 |
| `derived_records` | **不脱** | 派生记录落库 |
| `db_headers` | 脱 | 元数据落库前的 `headers` 字段 |
| `ui_view` | **不脱** | 界面展示（你自己看） |

**原始层（raw artifact）永不脱敏，且不可配置**（`RAW_LAYER_SANITIZATION = False`）：
它是事实层，脱了就不能重放——这是本工程的第一性原则，不是开关项。
**运行态钥匙不脱**：队列表里的 URL、下载直链、Cookie **原样**保存与使用
（抹掉签名参数会让续爬/续下 403、任务永久卡死）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from daedalus.obs.sanitize import (sanitize_credentials, sanitize_headers, sanitize_record,
                                   sanitize_text, sanitize_url)

__all__ = ["SanitizationPolicy", "KINDS", "DEFAULT_TOGGLES", "RAW_LAYER_SANITIZATION",
           "KIND_LABELS"]

# 原始层不可脱敏（写死；不要做成开关）
RAW_LAYER_SANITIZATION = False

KINDS = ("log_url", "log_headers", "log_text", "export_record", "derived_records",
         "db_headers", "ui_view")

DEFAULT_TOGGLES = {
    "log_url": True, "log_headers": True, "log_text": True,
    "export_record": False, "derived_records": False,
    "db_headers": True, "ui_view": False,
}

KIND_LABELS = {
    "log_url": "日志·URL 参数",
    "log_headers": "日志·请求/响应头",
    "log_text": "日志·正文文本(手机/邮箱/IP)",
    "export_record": "导出副本",
    "derived_records": "派生记录落库",
    "db_headers": "元数据落库·headers",
    "ui_view": "界面展示",
}


@dataclass
class SanitizationPolicy:
    """按类型开关的脱敏策略。构造后即不可变使用（要改就 `with_toggle()` 返回新对象）。"""

    log_url: bool = DEFAULT_TOGGLES["log_url"]
    log_headers: bool = DEFAULT_TOGGLES["log_headers"]
    log_text: bool = DEFAULT_TOGGLES["log_text"]
    export_record: bool = DEFAULT_TOGGLES["export_record"]
    derived_records: bool = DEFAULT_TOGGLES["derived_records"]
    db_headers: bool = DEFAULT_TOGGLES["db_headers"]
    ui_view: bool = DEFAULT_TOGGLES["ui_view"]

    # ── 读取/序列化 ────────────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg: dict | None) -> "SanitizationPolicy":
        """从配置字典构造；缺项用默认值（配置键名就是 `KINDS`）。"""
        data = {}
        for k in KINDS:
            if isinstance(cfg, dict) and k in cfg:
                data[k] = bool(cfg[k])
        return cls(**data)

    def to_dict(self) -> dict:
        return asdict(self)

    def enabled(self, kind: str) -> bool:
        """**True = 这一类做脱敏**（不是"显示原文"）。

        语义只此一种，避免误读：`export_record=True` 表示"导出副本要脱敏"，
        `export_record=False` 表示"导出原样"（默认值——个人使用要原始数据）。
        """
        if kind not in KINDS:
            raise KeyError(f"未知的脱敏类型: {kind!r}（合法值：{KINDS}）")
        return bool(getattr(self, kind))

    def sanitizes(self, kind: str) -> bool:
        """`enabled` 的同义别名（语义更直白：这一类是否**脱敏**）。"""
        return self.enabled(kind)

    def with_toggle(self, kind: str, value: bool) -> "SanitizationPolicy":
        data = self.to_dict()
        data[kind] = bool(value)
        return SanitizationPolicy(**data)

    # ── 透明：可查 + 变更留痕 + 不谎报 ──────────────────────────────
    def disabled_kinds(self) -> list[str]:
        return [k for k in KINDS if not self.enabled(k)]

    def summary(self) -> dict:
        """给 CLI（`dae config show`）与 UI 徽章用的只读快照。"""
        return {
            "toggles": self.to_dict(),
            "labels": {k: KIND_LABELS[k] for k in KINDS},
            "enabled_count": sum(1 for k in KINDS if self.enabled(k)),
            "disabled": self.disabled_kinds(),
            "raw_layer_sanitized": RAW_LAYER_SANITIZATION,
            "note": "原始层永不脱敏（不可配置）；运行态 URL/Cookie 不脱敏（脱了会 403）",
        }

    def describe(self) -> str:
        """一行人类可读状态（给 UI 状态栏与日志）。"""
        off = self.disabled_kinds()
        if not off:
            return "脱敏：全部开启（7/7）"
        names = "、".join(KIND_LABELS[k] for k in off)
        return f"脱敏：{len(off)} 项已关闭（{names}）"

    def log_state(self, logger: logging.Logger | None = None) -> str:
        """启动时调用：**任何一类被关闭都要说出来**（不许静默）。返回同一行文本。"""
        line = self.describe()
        log = logger or logging.getLogger(__name__)
        if self.disabled_kinds():
            log.info(line)
        else:
            log.debug(line)
        return line

    # ── 出口：日志 ────────────────────────────────────────────────
    def scrub_log(self, text) -> str:
        """日志文本脱敏。**两层，可关性不同**：

          ① **凭据形态永远脱敏**（`sanitize_credentials`）：header 转储、`Bearer/Basic`、
             `token=/sid=/api_key=` 这类键值——**不受任何开关影响**。
             理由：T1 的硬要求是"日志无敏感"。安全自审发现原来"把 `log_url`/`log_text`
             都关掉 → token 明文进日志"是**可达配置**——那等于把红线做成了选项 ✗。
          ② 可配的两类：`log_url`（URL 参数）与 `log_text`（手机号/邮箱/IP 这类隐私）。
             这两个才归"每类型开关"管；凭据不在此列。
        """
        t = sanitize_credentials(str(text or ""))
        if self.log_url:
            t = sanitize_url(t)
        if self.log_text:
            t = sanitize_text(t)
        return t

    def scrub_log_headers(self, headers: dict) -> dict:
        return sanitize_headers(headers) if self.log_headers else dict(headers or {})

    # ── 出口：导出 / 落库 / 界面 ────────────────────────────────────
    def for_export(self, record):
        """导出副本：受 `export_record` 控制（默认不脱，个人使用要原始数据）。"""
        return sanitize_record(record) if self.export_record else record

    def for_derived(self, record):
        """派生记录落库：受 `derived_records` 控制。"""
        return sanitize_record(record) if self.derived_records else record

    def for_db_headers(self, headers: dict) -> dict:
        """元数据落库的 headers：受 `db_headers` 控制（默认脱）。"""
        return sanitize_headers(headers) if self.db_headers else dict(headers or {})

    def for_ui(self, record):
        """界面展示：受 `ui_view` 控制（默认不脱）。"""
        return sanitize_record(record) if self.ui_view else record

    # ── 出口：原始层（不可配置，永远是恒等） ────────────────────────
    @staticmethod
    def for_raw_layer(obj):
        """**原样返回**（原始层是事实层，脱了就不能重放）。

        单独留这个函数是为了让调用点**显式表达意图**，避免后人"顺手补一道闸"。
        """
        return obj

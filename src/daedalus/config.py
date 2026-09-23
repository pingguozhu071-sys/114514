# -*- coding: utf-8 -*-
"""配置：**单一来源 + 一层映射**（防"声明与行为不符"）

为什么这么写：Kiana 的头号缺陷类型是"参数解析了但没人判断、界面选项根本没连到引擎"——
根因是配置要过五六道手写键表（控件 → 接线 → 翻译表 → 引擎 cfg → 默认值表），漏一处就"能填不生效"。
本工程的对策：**只有一层映射**——`config.example.toml → 本模块的 DEFAULTS → 各组件**，
并且每个键都有**往返接线测试**（见 `tests/gates/s1_gate.py` 的 F 组）。

优先级：**显式传入的 dict > TOML 文件 > DEFAULTS**。环境变量只用于少数开关：
`DAEDALUS_DATA_ROOT`（数据根）、`DAEDALUS_PORTABLE`（便携模式）。

凭据（cookie/密钥）**绝不放在配置文件里**——一律走 `privacy/secrets`（DPAPI 密文）。
"""

from __future__ import annotations

import logging
import pathlib

from daedalus.obs.policy import DEFAULT_TOGGLES, SanitizationPolicy

logger = logging.getLogger(__name__)

__all__ = ["DEFAULTS", "load_config", "merged", "build_policy", "build_fetcher", "to_toml_text"]

DEFAULTS: dict = {
    "fetcher": {
        # 诚实 UA（说明是本工具、用于个人采集）；不要伪装浏览器
        "user_agent": None,
        # 礼貌预算：每域并发 5（上限 8）、每域 QPS 1.0（不要超过目标站 ToS 允许值）
        "per_domain_concurrency": 5,
        "per_domain_qps": 1.0,
        "respect_robots": True,
        "timeout": 15.0,
    },
    "sanitization": dict(DEFAULT_TOGGLES),
    "logging": {
        "level": "INFO",
        "dir": None,                 # 空 = 只输出到控制台；给目录则同时写文件（带轮转）
        "json": True,                # true = 结构化 JSON 行（字段稳定，喂 jq / 回归对比）
        "max_bytes": 10 * 1024 * 1024,
        "backup_count": 5,
    },
    "alerts": {
        # 告警阈值（L4）：队列积压 / 失败率 / 磁盘水位。**默认有值且可配**。
        "queue_depth_pct": 80.0,     # 队列深度占上限的百分比
        "failure_rate_pct": 20.0,    # 失败率（成功+失败里的失败占比）
        "disk_free_pct": 10.0,       # 剩余磁盘占数据盘的比例下限
        "rss_mb": 3072.0,            # 常驻内存上限（4GB 预算留出余量）
        "latency_p95_ms": 5000.0,    # 请求 p95 延迟上限
    },
    "paths": {
        "data_root": None,           # 空 = 用 privacy.secrets.data_root() 的规则
    },
    "limits": {
        # 资源计划（`core/limits.py` 的 ResourcePlan，键名一一对应）。
        # 内存预算靠 `Σ(池规模 × 单任务峰值) ≤ 4096MB` 这条不等式自证（S7 门禁断言）。
        "download_threads": 128,
        "per_domain_concurrency": 8,
        "parse_threads": 20,
        "reparse_processes": 12,
        "writer_threads": 1,
        "subprocess_slots": 4,
        "browser_contexts": 0,       # 缺省 0 = 浏览器环境未启用（缺省即拒绝）
        "browser_pages": 0,
        "queue_frontier": 200000,
        "queue_download_parse": 10000,
        "queue_parse_store": 20000,
        "queue_writer": 20000,
        "batch_rows": 1000,          # 500–2000
        "flush_interval": 1.0,       # 或每 1s
        "peak_download_mb": 8.0,
        "peak_parse_mb": 48.0,
        "peak_browser_mb": 300.0,
        "memory_budget_mb": 4096.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_config(path=None, override: dict | None = None) -> dict:
    """读 TOML（缺文件就用默认值）并与 `override` 合并。**不抛异常**：坏配置只记 warning。"""
    data: dict = {}
    if path:
        p = pathlib.Path(path)
        if p.exists():
            try:
                import tomllib
                data = tomllib.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("配置读取失败（改用默认值）: %s: %s", p, e)
        else:
            logger.info("未找到配置文件 %s，使用默认值", p)
    return _deep_merge(_deep_merge(DEFAULTS, data), override or {})


def merged(cfg: dict | None) -> dict:
    """把"可能是半截的"配置补全成完整配置。"""
    return _deep_merge(DEFAULTS, cfg or {})


def build_policy(cfg: dict | None) -> SanitizationPolicy:
    """从配置构造脱敏策略（`[sanitization]` 段的键名就是 `KINDS`）。"""
    c = merged(cfg)
    return SanitizationPolicy.from_config(c.get("sanitization") or {})


def build_fetcher(cfg: dict | None, limiter=None, robots=None):
    """从配置构造出网咽喉（唯一入口）。

    `limiter` / `robots` 可注入（测试用）；不注入就按配置现造。
    """
    from daedalus.core.rate_limiter import DomainLimiter
    from daedalus.net.fetch import Fetcher
    from daedalus.net.robots import RobotsCache
    c = merged(cfg)
    f = c["fetcher"]
    if limiter is None:
        limiter = DomainLimiter(per_domain_concurrency=int(f["per_domain_concurrency"]),
                                per_domain_qps=float(f["per_domain_qps"]))
    fetcher = Fetcher(limiter=limiter, robots=robots, user_agent=f.get("user_agent"),
                      respect_robots=bool(f["respect_robots"]), timeout=float(f["timeout"]))
    if robots is None and fetcher._respect_robots:          # noqa: SLF001 - 刻意打破循环依赖
        fetcher._robots = RobotsCache(fetcher=fetcher, user_agent="*")   # noqa: SLF001
    return fetcher


def to_toml_text(cfg: dict | None = None) -> str:
    """把完整配置渲染成带注释的 TOML（用于生成 `config.example.toml`）。"""
    c = merged(cfg)
    lines = [
        "# Daedalus 配置示例（复制成 config.toml 后修改；**凭据不要写在这里**）",
        "# 键名与 src/daedalus/config.py 的 DEFAULTS 一一对应（只有一层映射）",
        "",
        "[fetcher]",
        f"user_agent = {_t(c['fetcher']['user_agent'])}   # 空 = 用默认诚实 UA（说明是本工具）",
        f"per_domain_concurrency = {c['fetcher']['per_domain_concurrency']}   # 每域并发（建议 4–8）",
        f"per_domain_qps = {c['fetcher']['per_domain_qps']}   # 每域 QPS（建议 1–2，按目标站 ToS 调）",
        f"respect_robots = {_t(c['fetcher']['respect_robots'])}   # 关掉它请先想清楚（合规红线）",
        f"timeout = {c['fetcher']['timeout']}   # 单请求超时（秒）",
        "",
        "[sanitization]   # 每一项：true = 这一类**做脱敏**；false = 输出原文",
        "# 原始层（raw artifact）永不脱敏且不可配置（脱了就不能重放）",
        "# 运行态 URL / Cookie 不脱敏（脱了会让续爬续下 403）",
    ]
    for k in DEFAULT_TOGGLES:
        lines.append(f"{k} = {_t(c['sanitization'][k])}")
    lines += [
        "",
        "[logging]",
        f"level = {_t(c['logging']['level'])}   # DEBUG / INFO / WARNING / ERROR",
        f"dir = {_t(c['logging']['dir'])}   # 空 = 只控制台；给目录则写文件（UTF-8 + 轮转）",
        f"json = {_t(c['logging']['json'])}   # true = 结构化 JSON 行（字段稳定，可 jq / 可回归对比）",
        f"max_bytes = {c['logging']['max_bytes']}",
        f"backup_count = {c['logging']['backup_count']}",
        "",
        "[alerts]   # 告警阈值（默认有值；GUI/CLI 会据此把指标标红）",
        f"queue_depth_pct = {c['alerts']['queue_depth_pct']}   # 队列深度占上限的百分比",
        f"failure_rate_pct = {c['alerts']['failure_rate_pct']}   # 失败率上限（%）",
        f"disk_free_pct = {c['alerts']['disk_free_pct']}   # 数据盘剩余空间下限（%）",
        f"rss_mb = {c['alerts']['rss_mb']}   # 常驻内存上限（MB）",
        f"latency_p95_ms = {c['alerts']['latency_p95_ms']}   # 请求 p95 延迟上限（ms）",
        "",
        "[paths]",
        f"data_root = {_t(c['paths']['data_root'])}   # 空 = 用户级目录（环境变量 DAEDALUS_DATA_ROOT 优先）",
        "",
        "[limits]   # 资源计划：队列全有界、池显式容量、内存按算术核对（core/limits.py）",
    ]
    for k, v in (c["limits"] or {}).items():
        lines.append(f"{k} = {_t(v)}")
    lines += [
        "# 内存自证：Σ(池规模 × 单任务峰值) ≤ memory_budget_mb（改了任一数字都要重算）",
        "",
    ]
    return "\n".join(lines)


def _t(v) -> str:
    if v is None:
        return '""'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace('"', '\\"') + '"'

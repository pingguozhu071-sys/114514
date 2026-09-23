# -*- coding: utf-8 -*-
"""引擎装配（EngineApp）：把各层拼成**一台能跑的东西**，供 CLI 与 GUI 共用

为什么要有这一层：CLI 与 GUI 各写一遍装配代码，就一定会漂移（一边接了浏览器、一边没接；
一边开了预算、一边没有）。所以装配只此一处，两边都从这里拿；两边的差别只剩"怎么显示"。

它做的事（没有魔法，就是把已经各自跑通的层按固定顺序接起来）：

    配置 → 数据根 → 库（迁移） → 单写线程 → 前沿 → 原始层/索引 → 解析注册表
         → 出网咽喉（闸+robots+限速）→ 直连环境 / 制品环境 /（可选）浏览器环境
         → 路由 → 运行器 → 台账 → 下钻 → 指标/告警 → 关闭链

三条纪律：
  * **关闭只走一条路**（`shutdown()` → `ShutdownChain`），CLI 与 GUI 都一样；
  * **缺件如实告知**（浏览器/ffmpeg 缺了就是缺了，`doctor()` 里能一眼看到）；
  * **装配不偷偷出网**：构造过程只建对象，不发请求（`run_targets` 才出网）。
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time

from daedalus import about
from daedalus.core.budget import Budget
from daedalus.core.limits import ResourcePlan
from daedalus.core.lifecycle import InflightRegistry, ShutdownChain
from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["EngineApp", "RunSummary"]


class RunSummary:
    """一次运行的汇总（CLI 的 JSON 输出 / GUI 的统计卡都读它）。"""

    def __init__(self):
        self.tasks = 0
        self.states: dict[str, int] = {}
        self.reports: list[dict] = []
        self.seconds = 0.0
        self.seconds_list: list[float] = []
        self.stopped_early = False
        self.notes: list[str] = []

    def add(self, state: str, seconds: float, report: dict) -> None:
        self.tasks += 1
        self.states[state] = self.states.get(state, 0) + 1
        self.seconds_list.append(seconds)
        if len(self.reports) < 200:                    # 有界：只留最近 200 条明细
            self.reports.append(report)

    def to_dict(self) -> dict:
        return {"tasks": self.tasks, "states": dict(sorted(self.states.items())),
                "seconds": round(self.seconds, 3),
                "per_task_p50": round(self._pct(0.50), 4),
                "per_task_p95": round(self._pct(0.95), 4),
                "stopped_early": self.stopped_early, "notes": self.notes[-20:],
                "reports_n": len(self.reports)}

    def _pct(self, q: float) -> float:
        xs = sorted(self.seconds_list)
        if not xs:
            return 0.0
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    def describe(self) -> str:
        body = "、".join(f"{k} {v}" for k, v in sorted(self.states.items())) or "无任务"
        return f"{self.tasks} 个任务（{body}），耗时 {self.seconds:.2f}s"


class EngineApp:
    """一台装配好的引擎。用 `EngineApp.build()` 构造，用 `shutdown()` 收尾。"""

    def __init__(self, cfg: dict, *, worker_id: str = "app"):
        from daedalus.capture.discovery import Discovery
        from daedalus.capture.index import ArtifactIndex
        from daedalus.capture.rawstore import RawStore
        from daedalus.config import build_fetcher, build_policy, merged
        from daedalus.core.registry import ResourceRegistry
        from daedalus.core.router import Environment, Router
        from daedalus.core.runner import TaskRunner
        from daedalus.env.media import MediaEnvironment
        from daedalus.env.net import NetEnvironment
        from daedalus.frontier.frontier import Frontier
        from daedalus.obs.drilldown import Drilldown
        from daedalus.obs.logs import setup_logging
        from daedalus.obs.samplers import ProcessSampler
        from daedalus.privacy.secrets import data_root
        from daedalus.store.db import Database
        from daedalus.store.deadletter import DeadLetter
        from daedalus.store.writer import SingleWriter
        from daedalus.understand.ledger import ChangeLedger
        from daedalus.understand.registry import default_registry

        self.cfg = merged(cfg)
        self.worker_id = worker_id
        self.plan = ResourcePlan.from_config(self.cfg)
        self.policy = build_policy(self.cfg)
        self.started_at = time.time()
        self.log_state = setup_logging(self.cfg)

        root = pathlib.Path(self.cfg["paths"].get("data_root") or data_root())
        self.data_root = root
        root.mkdir(parents=True, exist_ok=True)

        self.db = Database(root / "daedalus.db")
        self.dead_letter = DeadLetter(path=root / "deadletter.jsonl", db=self.db)
        self.writer = SingleWriter(self.db, dead_letter=self.dead_letter,
                                   batch_rows=self.plan.batch_rows,
                                   flush_interval=self.plan.flush_interval,
                                   queue_max=self.plan.queue_writer).start()
        self.frontier = Frontier(self.writer, max_queue=self.plan.queue_frontier,
                                 lease_timeout=300.0)
        self.store = RawStore(root / "raw_data", self.db, self.writer)
        self.index = ArtifactIndex(self.db)
        self.registry = default_registry()
        self.ledger = ChangeLedger(root / "ledger.jsonl")
        self.drilldown = Drilldown(self.db, ledger=self.ledger)
        self.fetcher = build_fetcher(self.cfg)

        self.net_env = NetEnvironment(self.fetcher, cache=None, cookies=None)
        self.media_env = MediaEnvironment(self.fetcher, workdir=root / "media")
        self.browser_env = None
        self.router = Router(enabled_environments=(Environment.NETWORK, Environment.ARTIFACT),
                             max_transitions=self.plan.memory_budget_mb and 8)
        self.registry_res = ResourceRegistry(self._capacities())
        self.inflight = InflightRegistry()
        # 发现链默认只追站内（同域）链接；站外只记不追（避免顺手把整个互联网拉进来）
        self.discovery = Discovery(same_site_only=True, fetcher=self.fetcher)
        self.runner = TaskRunner(frontier=self.frontier, router=self.router,
                                 net_env=self.net_env, media_env=self.media_env, store=self.store,
                                 registry=self.registry_res, ledger=self.ledger,
                                 discovery=self.discovery,
                                 worker_id=worker_id, inflight=self.inflight)
        self.sampler = None
        self._closed = False

    # ── 构造与组件开关 ───────────────────────────────────────────
    @classmethod
    def build(cls, cfg: dict | None = None, *, data_root=None, worker_id: str = "app",
              enable_browser: bool = False, browser_contexts: int | None = None,
              browser_pages: int | None = None, with_sampler: bool = True,
              fetcher=None) -> "EngineApp":
        from daedalus.config import load_config, merged
        c = merged(cfg if cfg is not None else load_config(None))
        if data_root is not None:
            c["paths"] = dict(c.get("paths") or {}, data_root=str(data_root))
        app = cls(c, worker_id=worker_id)
        if fetcher is not None:
            # 注入点：门禁/基准用假咽喉跑真闭环（产品默认永远是真咽喉）
            app.fetcher = fetcher
            app.net_env.fetcher = fetcher
            app.media_env.fetcher = fetcher
        if enable_browser:
            app.enable_browser(browser_contexts, browser_pages)
        app.browser_settle = 0.5
        app.runner.browser_settle = app.browser_settle
        app.runner.browser_env = app.browser_env
        app.runner.registry = app.registry_res
        if with_sampler:
            app.start_sampler()
        return app

    def _capacities(self) -> dict:
        """资源账本：把资源计划里的池规模变成注册表容量（**缺省即拒绝**的判据来源）。"""
        return {"network": self.plan.download_threads, "thread": self.plan.parse_threads,
                "process": self.plan.reparse_processes,
                "subprocess": self.plan.subprocess_slots,
                "browser": self.plan.browser_contexts, "async": 0}

    def enable_browser(self, contexts: int | None = None, pages: int | None = None) -> dict:
        """启用浏览器环境（**显式启用**；不调用就一直是 0 = 缺省即拒绝）。"""
        from daedalus.core.router import Environment
        from daedalus.env.browser import BrowserEnvironment
        ctx_n = int(self.plan.browser_contexts if contexts is None else contexts)
        page_n = int(self.plan.browser_pages if pages is None else pages)
        ctx_n = max(1, ctx_n)
        page_n = max(1, page_n)
        self.browser_env = BrowserEnvironment(store=self.store, gate=None, robots=None,
                                             max_contexts=ctx_n, max_pages=page_n)
        self.router.enabled = (Environment.NETWORK, Environment.ARTIFACT, Environment.BROWSER)
        self.runner.browser_env = self.browser_env
        self.registry_res.register("browser", ctx_n)
        return {"browser_contexts": ctx_n, "browser_pages": page_n,
                "capability": self.browser_env.capability()}

    def start_sampler(self) -> dict:
        from daedalus.obs.samplers import ProcessSampler
        if self.sampler is None:
            self.sampler = ProcessSampler(interval=5.0, disk_path=self.data_root).start()
            # 立刻采一次：CLI 的 `metrics`/`alerts` 是**一次性**命令，
            # 不等第一次定时采样（否则刚启动就看到"RSS/磁盘未采样"的 unknown）
            self.sampler.sample_once()
        return {"started": True, "disk_path": str(self.data_root)}

    # ── 采集 ────────────────────────────────────────────────────
    def run_targets(self, urls, *, workers: int = 4, budget: Budget | None = None,
                    goal: str = "", idle_timeout: float = 300.0,
                    deadline: float | None = None) -> RunSummary:
        """把一批 URL 变成任务跑完（多线程领取直到前沿空）。

        `workers` 是**领取线程数**；每个任务内部的资源开销由资源计划与预算管着。

        ⚠️ 收工条件（S9 门禁跑出来的真 bug）：**队列空 + 没有在飞任务** 才退出。
        曾经写成"空手就等到 idle_timeout（默认 300s）"——于是 `daedalus collect URL`
        跑完最后一个任务还要**空转五分钟**才返回（用户看到的是"命令卡住"）。
        为什么不能"队列空就立刻退"：别的 worker 手上那个任务可能**发现子任务**再入队，
        提前退出会把它们漏掉。所以判据是"队列空 **且** in_flight == 0"（租约里的活都干完了）。
        """
        from daedalus.core.task import ResourceRequest, Task
        summary = RunSummary()
        b = budget or Budget.small()
        t0 = time.monotonic()
        summary.seconds = 0.0
        for u in urls or []:
            task = Task.acquire(str(u), goal=goal or "CLI 采集",
                                resources=ResourceRequest(
                                    network=1,
                                    browser=1 if (self.browser_env is not None) else 0),
                                budget=b)
            okk, why = self.frontier.enqueue(task)
            if not okk:
                summary.notes.append(f"入队失败 {u}：{why}")
        workers = max(1, int(workers))
        lock = threading.Lock()
        stop_at = (time.monotonic() + float(deadline)) if deadline else None

        def worker(wid: str) -> None:
            empty_since = time.monotonic()
            last_probe = 0.0
            while True:
                if stop_at is not None and time.monotonic() >= stop_at:
                    with lock:
                        summary.stopped_early = True
                    return
                batch = self.frontier.claim_batch(1, wid)
                if not batch:
                    # 领不到活：**先确认没有在飞任务**才收工（别的 worker 可能还在派生子任务）
                    now = time.monotonic()
                    if now - last_probe >= 0.5:
                        last_probe = now
                        try:
                            if int(self.frontier.stats().get("in_flight", 0)) == 0:
                                return
                        except Exception as e:
                            summary.notes.append(f"查在飞数失败（按继续处理）：{e}")
                    if now - empty_since > idle_timeout:
                        summary.notes.append(f"{wid} 等待 {idle_timeout:.0f}s 无新任务 → 收工")
                        return
                    time.sleep(0.1)
                    continue
                empty_since = time.monotonic()
                for task in batch:
                    t1 = time.monotonic()
                    rep = self.runner.run_one(task)
                    with lock:
                        summary.add(str(rep.final_state), time.monotonic() - t1, rep.to_dict())

        threads = [threading.Thread(target=worker, args=(f"{self.worker_id}-{i}",),
                                    name=f"dae-worker-{i}") for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        summary.seconds = time.monotonic() - t0
        METRICS.set("app.last_run_seconds", summary.seconds)
        return summary

    # ── 离线重放（先捕获后理解的兑现）────────────────────────────
    def reparse(self, *, since: float | None = None, url_like: str | None = None,
                limit: int | None = None, dry_run: bool = False) -> dict:
        """用**当前**解析器重扫历史原始数据（**不联网**）。"""
        from daedalus.capture.replay import Reparser
        rp = Reparser(self.store, self.registry, self.index, ledger=self.ledger)
        if dry_run:
            rows = (self.index.url_like(url_like, limit=limit or 500) if url_like
                    else self.index.since(since if since is not None else 0.0, limit=limit))
            return {"dry_run": True, "scanned": len(rows),
                    "network_calls": self._net_calls(),
                    "parsers": [p["name"] for p in self.registry.summary()]}
        delivered: list[dict] = []

        def deliver(record: dict, artifact) -> None:
            page = {"url_hash": hash_key(record.get("url") or artifact.url),
                    "url": record.get("url") or artifact.url,
                    "content_hash": record.get("content_hash"),
                    "simhash": record.get("simhash"), "size": len(record.get("text") or ""),
                    "source_sha256": getattr(artifact, "sha256", ""), "status": 200}

            def job(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO pages (url_hash, url, fetched_at, status, "
                    "content_hash, simhash, duplicate_of, size, source_sha256) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (page["url_hash"], page["url"], time.time(), 200, page["content_hash"],
                     page["simhash"], record.get("duplicate_of"), page["size"],
                     page["source_sha256"]))
                # 重放也要更新全文索引（否则"用新解析器重扫"之后搜不到新正文）
                from daedalus.store.search import index_page
                index_page(conn, url_hash=page["url_hash"], url=page["url"],
                           title=str(record.get("title") or "")[:500],
                           text=str(record.get("text") or "")[:200000])

            self.writer.run_now(job, label="reparse.deliver")
            delivered.append({"url": page["url"], "content_hash": page["content_hash"]})

        stats = rp.run(since=since, url_like=url_like, limit=limit, deliver=deliver)
        stats["delivered"] = len(delivered)
        stats["network_calls"] = self._net_calls()
        stats["parsers"] = [p["name"] for p in self.registry.summary()]
        return stats

    def _net_calls(self) -> int:
        try:
            return int(self.fetcher.stats().get("calls", 0))
        except Exception:
            return -1

    # ── 观测面 ──────────────────────────────────────────────────
    def metrics(self) -> dict:
        from daedalus.obs.alerts import Thresholds, evaluate
        s = METRICS.summary()
        alarms = evaluate(s, thresholds=Thresholds.from_config(self.cfg),
                          queue_limits=self.plan.queues(),
                          queue_depths={k.split("{")[0].replace("queue.depth", "").strip("{}=")
                                        or "writer": v
                                        for k, v in METRICS.gauges("queue.depth").items()})
        try:
            tasks = self.drilldown.tasks_overview(limit=50)
        except Exception as e:
            logger.debug("任务列表读取失败：%s", e)
            tasks = []
        return {"summary": s, "alerts": [{"key": a.key, "level": a.level, "message": a.message}
                                         for a in alarms],
                # 给 GUI 任务表用（含 content_hash 指纹列 → 「指纹可见」）
                "tasks": tasks,
                "fetcher": self.fetcher.stats(), "frontier": self.frontier.stats(),
                "writer": self.writer.stats(), "ledger": self.ledger.summary()}

    def doctor(self) -> dict:
        """自检：把"能不能用、缺什么、处在什么状态"一次说清（CLI `doctor` / GUI 自检页共用）。"""
        from daedalus.exec.subprocess import readiness
        from daedalus.net.ssrf_gate import cache_stats
        from daedalus.obs.samplers import disk_stats
        from daedalus.privacy.secrets import dpapi_available

        problems: list[str] = []
        warnings: list[str] = []
        try:
            disk = disk_stats(self.data_root)
        except Exception as e:
            disk, _ = {}, problems.append(f"磁盘水位读不到：{e}")
        if disk.get("free_pct") is not None and disk["free_pct"] < 10:
            warnings.append(f"数据盘剩余 {disk['free_pct']:.1f}%（<10%）")
        if not dpapi_available():
            problems.append("本机不支持 DPAPI：凭据无法加密保存（相关功能不可用）")
        tools = readiness(("ffmpeg", "ffprobe", "aria2c", "yt-dlp"))
        if tools["missing"]:
            warnings.append("缺少可选工具：" + "、".join(tools["missing"])
                            + "（对应能力被禁用，不会静默降级）")
        db = self.db.stats()
        if db["journal_mode"] != "wal":
            problems.append(f"journal_mode 不是 wal（{db['journal_mode']}）")
        if int(db["busy_timeout"]) < 30000:
            warnings.append(f"busy_timeout 偏小（{db['busy_timeout']}）")
        cap = {"available": False, "reason": "未启用（资源计划里 browser_contexts=0）"}
        try:
            from daedalus.env.browser import probe_browser
            cap = probe_browser("chromium").to_dict()
        except Exception as e:
            cap = {"available": False, "reason": f"{type(e).__name__}: {e}"}
        try:
            self.plan.validate()
        except Exception as e:
            problems.append(f"资源计划不自洽：{e}")
        pol = self.policy.summary() if hasattr(self.policy, "summary") else {}
        out = {
            "about": about(),
            "paths": {"data_root": str(self.data_root), "db": str(self.db.path),
                      "raw": str(self.store.root), "logs": self.log_state.get("file")},
            "disk": disk,
            "db": db,
            "plan": self.plan.to_dict(),
            "memory_arithmetic": self.plan.memory_arithmetic(),
            "sanitization": pol,
            "logging": self.log_state,
            "tools": tools,
            "browser": cap,
            "secrets": {"dpapi": dpapi_available()},
            "net": {"dns_cache": cache_stats()},
            "metrics": METRICS.summary(),
            "problems": problems,
            "warnings": warnings,
        }
        out["ok"] = not problems
        return out

    def search(self, query: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
        """中文全文检索（FTS5 + 预分词）。查不到返回空列表（不是错误）。"""
        from daedalus.store.search import search as _search
        conn = self.db.connect(readonly=True)
        try:
            return list(_search(conn, query, limit=limit, offset=offset))
        finally:
            conn.close()

    def export_jsonl(self, limit: int = 10000) -> str:
        return self.drilldown.export_jsonl(limit=limit)

    # ── 收尾 ────────────────────────────────────────────────────
    def shutdown(self) -> dict:
        """关闭链（幂等）。浏览器/驱动也在这里收干净。"""
        if self._closed:
            return {"ok": True, "note": "已关闭（重复调用）"}
        self._closed = True
        extra = []
        if self.browser_env is not None:
            extra.append(("browser_close", self.browser_env.close, 20.0))
        try:
            from daedalus.env.browser import shutdown_playwright
            extra.append(("playwright_stop", shutdown_playwright, 20.0))
        except Exception:
            pass
        rep = ShutdownChain(inflight=self.inflight).run(
            writer=self.writer, db=self.db, sampler=self.sampler, extra=extra)
        return rep.to_dict()

    def __enter__(self) -> "EngineApp":
        return self

    def __exit__(self, *exc) -> bool:
        self.shutdown()
        return False


def hash_key(url: str) -> str:
    """`pages.url_hash` 的键（与任务幂等键同源：`kind|target`）。"""
    from daedalus.core.task import make_idempotency_key
    return make_idempotency_key("acquire", str(url or ""))

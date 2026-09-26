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
from daedalus.obs.logs import log_event

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
        # 本次运行**实际**批准的 worker 容量（经计划对照 + 注册表裁决后的值）。
        # 它不是「调用方要的数」——要的就是这个区别：容量是被裁过的，且要看得见。
        self.workers = 0

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
                "workers": int(self.workers), "reports_n": len(self.reports)}

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
        # 资源账本要在**建执行面之前**就位：媒体环境（HLS 分片并发）与子进程执行面
        # （ffmpeg 槽位）都要从它取额度。「登记了但没人读」正是自有审计抓到的形态
        # （`subprocess_slots` / 自建 HLS 线程池），所以把它变成装配顺序上的必答题：
        # 环境先拿到同一个 `registry_res`，容量才有唯一的出处。
        self.registry_res = ResourceRegistry(self._capacities())

        self.net_env = NetEnvironment(self.fetcher, cache=None, cookies=None)
        self.media_env = MediaEnvironment(self.fetcher, workdir=root / "media",
                                          registry=self.registry_res)
        self.browser_env = None
        self.router = Router(enabled_environments=(Environment.NETWORK, Environment.ARTIFACT),
                             max_transitions=self.plan.memory_budget_mb and 8)
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
            # 发现链也要一起换：它拿 `is_allowed` 做策略过滤，漏掉它就会出现
            # 「门禁说是离线，但发现新链接时仍然去问真咽喉（可能拉 robots.txt）」——
            # 假栈必须**到处都是假的**，否则「离线跑」这句话不成立。
            if getattr(app, "discovery", None) is not None:
                app.discovery.fetcher = fetcher
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
        """资源账本：把资源计划里的池规模变成注册表容量（**缺省即拒绝**的判据来源）。

        ⚠️ 这里**一一对应**，没有「计划里写了但注册表没有」的科目——否则那条计划就是装饰。
        `hls_segments` 是「环境内部的并发科目」（任务不声明它，但会撞上它），
        它不经 `require()` 而经 `registry.gate()` 生效（见 `adapters/hls.py`）。
        """
        return {"network": self.plan.download_threads, "thread": self.plan.parse_threads,
                "process": self.plan.reparse_processes,
                "subprocess": self.plan.subprocess_slots,
                "hls_segments": self.plan.hls_segments,
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
    def worker_capacity(self, workers: int) -> int:
        """把调用方要的 worker 数**对照资源计划**核一遍，返回可用容量。

        三条不许（自有审计：「执行资源面写好了没通电」）：
          * workers 是**下载线程**的一部分，过去裸起 `threading.Thread` × workers，
            与 `ResourcePlan.download_threads` 没有任何强制关系——现在超过计划值就**报错**
            （`PlanViolation`），不静默截断、也不默默超发；
          * 容量必须**经注册表取**（`registry.require`）：未登记 / 容量 0 → `ResourceDenied`
            （缺省即拒绝）——注册表与计划不一致时，以注册表为准；
          * 零/负 worker 是非法调用，不做「自动兜底成 1」这种静默修正。
        """
        from daedalus.core.limits import PlanViolation
        from daedalus.core.task import ResourceRequest
        want = int(workers)
        planned = int(self.plan.download_threads)
        if want <= 0:
            raise PlanViolation(
                f"workers={want} 非法：worker 数必须为正（不做静默兜底成 1 这种修正）")
        if want > planned:
            raise PlanViolation(
                f"workers={want} 超过资源计划 plan.download_threads={planned}："
                f"worker 就是下载线程，超发会让「Σ(池规模×单任务峰值) ≤ 内存预算」这条自证失效。"
                f"要更多并发请提高 [limits].download_threads，或把 workers 调小（不许静默截断）")
        self.registry_res.require(ResourceRequest(network=want))      # 注册表裁决
        return min(want, int(self.registry_res.capacity("network")))

    def run_targets(self, urls, *, workers: int = 4, budget: Budget | None = None,
                    goal: str = "", idle_timeout: float = 300.0,
                    deadline: float | None = None,
                    stop_event: "threading.Event | None" = None) -> RunSummary:
        """把一批 URL 变成任务跑完（多线程领取直到前沿空）。

        `workers` 是**领取线程数**（= 下载线程），**必须 ≤ `plan.download_threads`**，
        并且要过注册表的容量裁决（`worker_capacity()`）——超了就是 `PlanViolation`，
        不静默截断。承载它的池是 `exec/pools.py` 的 `ManagedPool`（有界提交 + 优雅关闭），
        **不是**裸 `threading.Thread` 数组（自有审计的原话：那样写等于「资源计划没通电」）。
        每个任务内部的资源开销由资源计划与预算管着。
        `stop_event` 是**可选中止**：界面上的「停止」把它 set 上，worker 在下一轮领取前退出
        （`summary.stopped_early=True`）。**已领走的租约不丢**——要么跑完、要么按租约超时
        回到队列，下次继续；中止不等于丢任务。

        ⚠️ 收工条件（S9 门禁跑出来的真 bug）：**队列空 + 没有在飞任务** 才退出。
        曾经写成「空手就等到 idle_timeout（默认 300s）」——于是 `daedalus collect URL`
        跑完最后一个任务还要**空转五分钟**才返回（用户看到的是「命令卡住」）。
        为什么不能「队列空就立刻退」：别的 worker 手上那个任务可能**发现子任务**再入队，
        提前退出会把它们漏掉。所以判据是「队列空 **且** in_flight == 0」（租约里的活都干完了）。
        """
        from daedalus.core.task import ResourceRequest, Task
        from daedalus.exec.pools import ManagedPool
        summary = RunSummary()
        b = budget or Budget.small()
        # 容量先核（在任何入队/起线程之前）：核不过就**什么都不做**地报错
        workers = self.worker_capacity(workers)
        summary.workers = workers
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
                # 界面的「停止」：下一轮领取前退出。**已经领走的租约不丢**——
                # 要么跑完，要么按租约超时回到队列，下次接着做（中止不是「丢任务」）。
                if stop_event is not None and stop_event.is_set():
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

        # 池名固定为 targets：观测面（门禁/指标）靠它读「worker 容量真的生效了」
        pool = ManagedPool("targets", workers, registry=self.registry_res,
                          capacity_name="network", queue_max=max(8, workers * 2)).start()
        futs = []
        for i in range(workers):
            fut, why = pool.submit(worker, f"{self.worker_id}-{i}",
                                   task_id=f"{self.worker_id}-{i}")
            if fut is None:
                summary.notes.append(f"worker {i} 提交失败：{why}")
            else:
                futs.append(fut)
        if not futs:
            pool.shutdown(drain=False)
            raise RuntimeError("一个 worker 都没提交成功：拒绝「零线程静默返回」这种假成功")
        pool.shutdown(drain=True)          # 等所有 worker 自然收工（= 原来的 join）
        for f in futs:                     # 池化后 worker 的异常不会再被吞进 stderr
            try:
                f.result()
            except Exception as e:
                summary.notes.append(f"worker 异常：{type(e).__name__}: {e}")
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
        # 界面语言（**装完语言对不对**这件事要能自己在目标机器上查）：
        # 顺序 设置 > 安装器选择（exe 同级 install.marker 的 lang=）> 系统 UI 语言 > en-US
        try:
            from daedalus.ui.i18n import (detect_system_locale, read_installer_locale,
                                          resolve_locale)
            inst = ""
            try:
                from daedalus.ui.settings import SettingsStore
                inst = str(SettingsStore(self.data_root, autosave=False).get("locale") or "")
            except Exception:
                inst = ""
            ui_locale = resolve_locale(inst)
            if inst in ("zh-CN", "ja-JP", "en-US"):
                src_of = "用户设置"
            elif read_installer_locale():
                src_of = "安装器选择"
            elif detect_system_locale() == ui_locale:
                src_of = "系统 UI 语言"
            else:
                src_of = "兜底"
            ui_info = {"locale": ui_locale, "source": src_of, "setting": inst or "跟随系统",
                       "installer": read_installer_locale() or "（无标记）",
                       "system": detect_system_locale()}
        except Exception as e:
            ui_info = {"locale": "en-US", "source": f"探测失败：{type(e).__name__}: {e}"}
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
            # 解析器名单（**冻结态自证的一部分**：打包态注册表掉成员时 doctor 当场可见，
            # tools/build.py 的打包态冒烟就靠它断言四件套齐——台账 B20-2）
            "parsers": [p.get("name") for p in self.registry.summary()],
            "ui": ui_info,                 # 界面语言与来源（装完在目标机上可查）
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

    # ── 任务级的三个操作（界面「任务」页的按钮走这里）──────────────
    def retry_tasks(self, task_ids, *, max_attempts: int = 5) -> dict:
        """把选中的任务**重新排回队列**（重试）。原始层不动，只是让它们再跑一次。

        * 重试**有上限**（默认 5 次，见 `frontier.requeue`）：反复重试不能无限；
        * 正在跑的会被拒（同一条任务不能有两个执行者）；
        * 每一条的结果都**如实带回**（成功/失败原因），不做「批量假装成功」。
        """
        ids = [str(t) for t in (task_ids or []) if str(t)]
        if not ids:
            return {"requested": 0, "ok": 0, "results": [], "note": "没有选中任何任务"}
        rows = self.frontier.requeue(ids, max_attempts=max_attempts)
        ok = sum(1 for _tid, good, _why in rows if good)
        for tid, good, why in rows:
            log_event(logger, "task.retry" if good else "task.retry_rejected",
                      f"重试 {tid}：{why}", task_id=tid,
                      level=logging.INFO if good else logging.WARNING)
        METRICS.inc("app.tasks_retried", ok)
        return {"requested": len(ids), "ok": ok,
                "results": [{"task_id": t, "ok": g, "why": w} for t, g, w in rows]}

    def export_tasks_jsonl(self, task_ids, *, limit: int = 2000) -> str:
        """导出选中任务的结果（JSONL）。与 CLI `export` **同一实现**，因此**同样过脱敏**。"""
        ids = [str(t) for t in (task_ids or []) if str(t)]
        log_event(logger, "task.export", f"导出选中任务 {len(ids)} 条", n=len(ids))
        return self.drilldown.export_jsonl(limit=limit, task_ids=ids)

    def forget_tasks(self, task_ids, *, dry_run: bool = True) -> dict:
        """删除任务记录（tasks + task_evidence + 该任务的 errors）。

        **捕获面不可变**——这是本工程的地基，所以这里划死三条：
          * `raw_artifacts` / `pages` / `extracted` / `downloads` **一个字节都不删**
            （原始层是地面真值，派生层可重算；要清理原始层请用专门的工具，别走这里）；
          * `dry_run=True` 是默认值：先自证「我会删多少行」，不信就不执行；
          * 删除**留痕**（结构化日志 `task.forgotten` + 指标计数），事后能查「什么时候删了什么」。
        """
        ids = [str(t) for t in (task_ids or []) if str(t)]
        if not ids:
            return {"dry_run": bool(dry_run), "requested": 0, "deleted": {},
                    "note": "没有选中任何任务"}

        counts = {"tasks": 0, "task_evidence": 0, "errors": 0}

        def job(conn):
            for tid in ids:
                row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (tid,)).fetchone()
                if row is None:
                    continue
                if str(row["state"]) in ("leased", "running"):
                    # 正在跑的任务不能删（它的 worker 还在写证据行，删了会留下孤儿证据）
                    continue
                if dry_run:
                    r1 = conn.execute(
                        "SELECT COUNT(*) AS n FROM task_evidence WHERE task_id = ?",
                        (tid,)).fetchone()
                    counts["task_evidence"] += int((r1["n"] if r1 else 0))
                    r2 = conn.execute(
                        "SELECT COUNT(*) AS n FROM errors WHERE task_id = ?", (tid,)).fetchone()
                    counts["errors"] += int((r2["n"] if r2 else 0))
                    counts["tasks"] += 1
                    continue
                conn.execute("DELETE FROM task_evidence WHERE task_id = ?", (tid,))
                conn.execute("DELETE FROM errors WHERE task_id = ?", (tid,))
                conn.execute("DELETE FROM tasks WHERE task_id = ?", (tid,))
                counts["tasks"] += 1
            return counts

        got = self.writer.run_now(job, label="app.forget_tasks")
        if not dry_run:
            log_event(logger, "task.forgotten",
                      f"删除任务记录：{got['tasks']} 条（原始层未动）",
                      level=logging.WARNING, n_tasks=got["tasks"],
                      n_evidence=got["task_evidence"], n_errors=got["errors"])
            METRICS.inc("app.tasks_forgotten", got["tasks"])
        return {"dry_run": bool(dry_run), "requested": len(ids), "deleted": got,
                "raw_untouched": True,
                "note": "只删任务/证据/错误行；原始层（raw_artifacts）与派生层不动"}

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

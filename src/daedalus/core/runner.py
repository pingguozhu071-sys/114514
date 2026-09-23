# -*- coding: utf-8 -*-
"""任务运行器：把"分层各自跑通"串成**一个闭环**

它不决定"用哪个环境"（那是 `core/router.py` 的职责），只负责把闭环里的每一步落到实处：

    Task ──(Router.start)──→ 决策
      │                        │
      │  Direct  → 直连网络环境：取流 → **原始层落盘** → 解析 → 质量闸 → 证据
      │  Artifact→ 制品与媒体环境：下载 → **产物契约** → 证据
      │                        │
      ├──(Router.on_evidence)──┘  ← 证据回灌，得到下一步或终态
      ↓
    Frontier.commit_done（CAS + 产物 + 派生行 **同一事务**） / mark_failed（限流与重试分别记账）
      ↓
    变更台账（new/unchanged/updated/failed/policy_denied）

三条必须守住的：
  * **打卡在写产物之前**（`commit_done` 内部就是 CAS 先行；失守则一行都不写）；
  * **限流 ≠ 重试**（429/被拦 → `throttled=True`，只加独立计数）；
  * **转移有界**（超预算/超转移次数 → 死信，绝不无限升级）。

关于"原始层 vs CAS"的**刻意取舍**（S6 想清楚了才这么写的）：
    原始层在**执行期**就落盘（Capture First：进程随时可能死，事实必须先留下），而 CAS 保护的是
    "任务完成"这个**状态**与**派生行**。这不会产生重复或坏数据，因为原始层是**内容寻址 + 幂等写**：
    同一个 URL 同一份字节 → 同一 sha256 → `INSERT OR IGNORE`；两个 worker 各抓到不同字节
    （站点中途变了）→ 两行，各自都是**当时看见的事实**。截断的响应会在行上标 `too_big`，
    不会伪装成完整产物。租约失守时台账会记下"本次结果作废"，审计链完整。
"""

from __future__ import annotations

import logging
import time

from daedalus.core.evidence import Evidence, from_decision, from_parse
from daedalus.core.router import Router, Stage
from daedalus.core.task import Task, TaskState
from daedalus.frontier.dedup import clamp63, content_hash, simhash64
from daedalus.obs.metrics import METRICS
from daedalus.understand.ledger import ChangeLedger

logger = logging.getLogger(__name__)

__all__ = ["TaskRunner", "RunReport", "response_evidence"]


class RunReport:
    """一次任务运行的报告（可读、可记台账、可进观测）。"""

    __slots__ = ("task_id", "final_state", "reason", "steps", "artifacts", "derived",
                 "children", "elapsed", "lease_lost")

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.final_state = TaskState.PENDING
        self.reason = ""
        self.steps: list[str] = []
        self.artifacts: list[dict] = []
        self.derived: dict | None = None
        self.children: int = 0
        self.elapsed: float = 0.0
        self.lease_lost: bool = False

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "final_state": self.final_state, "reason": self.reason,
                "steps": list(self.steps), "artifacts": len(self.artifacts),
                "derived": bool(self.derived), "children": self.children,
                "elapsed": round(self.elapsed, 3), "lease_lost": self.lease_lost}


class TaskRunner:
    """跑一个任务直到终态（或交给别人重试）。"""

    def __init__(self, *, frontier, router: Router, net_env=None, media_env=None,
                 store=None, registry=None, ledger: ChangeLedger | None = None,
                 discovery=None, worker_id: str = "w1", sleep=time.sleep, inflight=None,
                 browser_env=None, browser_settle: float = 0.5):
        self.frontier = frontier
        self.router = router
        self.net_env = net_env
        self.media_env = media_env
        self.browser_env = browser_env          # 环境②（缺省 None = 未装配 → 只记候选）
        self.browser_settle = float(browser_settle)
        self.store = store
        self.registry = registry
        self.ledger = ledger or ChangeLedger()
        self.discovery = discovery
        self.worker_id = worker_id
        self._sleep = sleep                     # 注入点：门禁里不需要真等退避
        # 在飞任务**强引用**（E9）：交给线程池后如果没人持有，任务会被 GC 静默吞掉
        self.inflight = inflight

    # 路由决定重试时的退避阶梯（抖动交给环境层，避免两处叠加）
    BACKOFF_BASE, BACKOFF_CAP = 0.5, 8.0

    def _backoff(self, task: Task) -> float:
        n = max(0, int(task.attempts) + int(task.throttles))
        return min(self.BACKOFF_CAP, self.BACKOFF_BASE * (2 ** min(n, 8)))

    # ── 主入口 ───────────────────────────────────────────────────
    def run_one(self, task: Task) -> RunReport:
        rep = RunReport(task.task_id)
        t0 = time.monotonic()
        METRICS.inc("task.total")
        if self.inflight is not None:
            self.inflight.add(task)             # **强引用**：跑着的任务不许被 GC 吞掉
        try:
            self._loop(task, rep)
        except Exception as e:                          # 运行器自身绝不静默
            rep.final_state, rep.reason = TaskState.DEAD, f"运行器异常：{type(e).__name__}: {e}"
            self.ledger.record("failed", url=task.target, detail=rep.reason)
            logger.exception("任务 %s 运行器异常", task.task_id)
        finally:
            if self.inflight is not None:
                self.inflight.remove(task.task_id)
        rep.elapsed = time.monotonic() - t0
        METRICS.observe("task.duration", rep.elapsed)
        METRICS.inc("task.transitions", task.transitions)
        state = str(rep.final_state)
        if state == TaskState.DONE:
            METRICS.inc("task.done")
            METRICS.count_throughput(pages_added=1)
        elif state == TaskState.POLICY_DENIED:
            METRICS.inc("task.policy_denied")
        elif state == "lease_lost":
            METRICS.inc("task.lease_lost")
        else:
            METRICS.inc("task.failed", state=state)
        return rep

    # ── 闭环 ─────────────────────────────────────────────────────
    def _loop(self, task: Task, rep: RunReport) -> None:
        decision = self.router.start(task)
        while True:
            if decision.is_terminal:
                self._finalize(task, decision, rep)
                return
            # **取消点**（关闭时被标记取消）→ 交还队列：任务没问题，只是这次不跑了
            if self.inflight is not None and self.inflight.cancelled(task.task_id):
                self._release(task, rep, "进程关闭：任务交还队列（不计失败）")
                return
            step = decision.next_stage
            task.policy["stage"] = step
            task.transitions += 1
            rep.steps.append(f"{step}[{decision.environment or '-'}]")
            evs = self._execute(task, step, rep)        # 执行一步 → 产出证据（可多条）
            for e in (evs if isinstance(evs, list) else [evs]):
                task = task.add_evidence(e)
            ev = task.evidence[-1]                      # **最后一条是这一步的裁决**
            decision = self.router.on_evidence(task, ev)
            if not decision.is_terminal and decision.next_stage == Stage.DIRECT:
                # 路由决定"再来一次"时才记账与退避（否则坏目标会被瞬间打满）。
                # **两本账分开**：限流只加 throttles、瞬时失败只加 attempts（限流≠重试）。
                if ev.signal == "throttled":
                    task.throttles += 1
                elif ev.signal == "transient_failure":
                    task.attempts += 1
                if ev.signal in ("throttled", "transient_failure"):
                    self._sleep(self._backoff(task))

    # ── 执行一步（按环境分派）────────────────────────────────────
    def _execute(self, task: Task, stage: str, rep: RunReport):
        """执行一个阶段。返回单条证据，或**一串**（响应事实在前、裁决在后）。"""
        if stage == Stage.DIRECT:
            return self._do_direct(task, rep)
        if stage == Stage.ARTIFACT:
            return self._do_artifact(task, rep)
        if stage in (Stage.BROWSER, Stage.OBSERVE):
            return self._do_browser(task, rep)
        return from_decision("resource_denied", "stop",
                             f"阶段 {stage} 在 V0.1 未启用（只记候选）", stage="runner")

    def _do_browser(self, task: Task, rep: RunReport):
        """环境②浏览器运行时：执行页面 → 观察网络活动 → 文档进原始层。

        两道前置（**缺省即拒绝**）：
          ① 资源注册表里必须有浏览器槽位（没申明/容量 0 → 明确拒绝，不偷偷起浏览器）；
          ② 环境自己就绪（二进制可用 + 槽位 > 0），否则如实报缺件。
        """
        env = self.browser_env
        if env is None:
            return from_decision("resource_denied", "stop", "浏览器环境未装配", stage="runner")
        want = max(1, int(task.resources.browser or 0))
        if self.registry is not None:
            try:
                self.registry.require(_browser_request(want))
            except Exception as e:
                return from_decision("resource_denied", "stop",
                                     f"资源缺省即拒绝：{e}"[:200], stage="runner")
        cap = {}
        try:
            cap = env.capability()
        except Exception as e:
            cap = {"available": False, "reason": f"{type(e).__name__}: {e}"}
        if not cap.get("available") or not cap.get("enabled", True):
            return from_decision("resource_denied", "stop",
                                 f"浏览器环境不可用：{cap.get('reason') or '未启用'}"[:250],
                                 stage="browser", facts={"capability": cap})
        try:
            verdict = env.observe(task.target, settle_seconds=self.browser_settle)
        except Exception as e:
            return from_decision("transient_failure", "retry",
                                 f"浏览器观察异常：{type(e).__name__}: {e}"[:200], stage="browser")
        rep.steps.append(f"browser: {'ok' if verdict.ok else 'fail'}"
                         f"（观察 {len(verdict.observed)} / 拦 {len(verdict.denied)}）")
        task.seconds_done += verdict.seconds
        task.bytes_done += int(verdict.captured_bytes or 0)
        if verdict.blocked:
            return from_decision("policy_denied", "stop", f"被拦：{verdict.reason}"[:200],
                                 stage="browser")
        facts = {"status": verdict.status, "title": verdict.title[:200],
                 "observed": len(verdict.observed), "denied": len(verdict.denied),
                 "html_sha256": verdict.html_sha256, "final_url": verdict.final_url[:300]}
        if verdict.ok and verdict.observed:
            # "观察到的网络活动"本身是**一等结果**（新事实 → 可派生新任务）
            return from_decision("network_activity", "capture_observed",
                                 f"观察到 {len(verdict.observed)} 条网络活动"
                                 f"（拦截 {len(verdict.denied)} 条）", stage="browser", **facts)
        if verdict.ok:
            return from_decision("ok", "capture", f"浏览器拿到内容：{verdict.summary()}"[:200],
                                 stage="browser", **facts)
        return from_decision("empty_content", "keep_raw",
                             f"浏览器也没拿到可用内容：{verdict.reason}"[:200],
                             stage="browser", **facts)

    def _do_direct(self, task: Task, rep: RunReport):
        if self.net_env is None:
            return from_decision("resource_denied", "stop", "直连网络环境未装配", stage="runner")
        res = self.net_env.get(task.target)
        task.bytes_done += res.size
        task.seconds_done += res.elapsed
        if res.throttled:
            return from_decision("throttled", "backoff",
                                 f"被限流（Retry-After={res.retry_after}）", stage="direct")
        if res.robots_denied:
            return from_decision("policy_denied", "stop", f"robots 不允许：{task.target}",
                                 stage="direct")
        if res.blocked:
            return from_decision("policy_denied", "stop", f"被闸拦下：{res.error}", stage="direct")
        if not res.ok:
            sig = "transient_failure" if res.status == 0 or res.status >= 500 else "permanent_failure"
            return from_decision(sig, "retry" if sig == "transient_failure" else "stop",
                                 f"HTTP {res.status}：{res.error}"[:200], stage="direct")
        # 成功：**先落原始层**（事实优先），再解析（理解可以重做）
        art = self._capture(task, res, rep)
        if art is None:
            return from_decision("transient_failure", "retry", "原始层写入失败", stage="capture")
        if res.too_big:
            return from_decision("large_object", "skip_parse",
                                 f"响应体超限（{res.size} 字节）→ 只捕获不解析", stage="direct")
        ev = response_evidence(res)
        if ev.signal in ("media_manifest", "large_object", "stream"):
            # 按**事实**改道：媒体清单 / 大对象 / 流式响应不走解析面（交给对应环境或适配器）。
            # 这里只回灌证据，改道与否由 Router 决定（运行器不自己选环境）。
            return ev
        # 理解面（解析 + 质量闸 + 指纹），派生行与打卡在同一个事务里落库
        derived, ev2 = self._understand(task, res, art, rep)
        rep.derived = derived
        if ev2 is not None:
            # 两条都留：① 响应事实（状态码/类型/体积）② 裁决（拒收/解析失败）。
            # 只留裁决会丢掉"凭什么"，事后没法解释也没法离线重判。
            return [ev, ev2]
        return ev

    def _do_artifact(self, task: Task, rep: RunReport) -> Evidence:
        """制品与媒体环境：下载 + 契约验证（大对象与分片不走解析）。"""
        if self.media_env is None:
            return from_decision("resource_denied", "stop", "制品与媒体环境未装配", stage="runner")
        from daedalus.capture.artifacts import for_document
        dest = (self.store.root / "artifacts" / f"{task.idempotency_key}.bin") if self.store \
            else None
        verdict = self.media_env.download_large(task.target, dest, contract=for_document())
        rep.steps.append(f"artifact: {'ok' if verdict.ok else 'fail'}")
        METRICS.inc("artifact.downloads", ok=bool(verdict.ok))
        if not verdict.ok:
            METRICS.inc("artifact.verify_failed")
            sig = "transient_failure" if "过小" not in verdict.reason else "permanent_failure"
            return from_decision(sig, "retry" if sig == "transient_failure" else "stop",
                                 f"制品获取失败：{verdict.reason}"[:200], stage="artifact")
        rep.artifacts.append(dict(verdict.facts, kind="artifact"))
        task.bytes_done += int(verdict.facts.get("size", 0) or 0)
        METRICS.inc("artifact.bytes", int(verdict.facts.get("size", 0) or 0))
        return from_decision("ok", "capture",
                             f"制品合格（{verdict.reason}）", stage="artifact",
                             sha256=verdict.facts.get("sha256", ""))

    # ── 捕获与理解 ───────────────────────────────────────────────
    def _capture(self, task: Task, res, rep: RunReport) -> dict | None:
        if self.store is None:
            return {}
        try:
            art = self.store.put(res.body, url=res.final_url or task.target, status=res.status,
                                 headers=res.headers, mime=str(res.headers.get("content-type", "")),
                                 source="network/direct", session_id="",
                                 parent_task=task.task_id, discovery_path=task.discovery_path,
                                 note="too_big" if res.too_big else "")
            rep.artifacts.append({"sha256": art["sha256"], "size": art["size"], "path": art["path"]})
            METRICS.inc("rawstore.puts")
            METRICS.inc("rawstore.bytes", int(art.get("size", 0) or 0))
            return art
        except Exception as e:
            METRICS.inc("rawstore.failed")
            logger.warning("原始层写入失败：%s", e)
            return None

    def _understand(self, task: Task, res, art: dict, rep: RunReport) -> tuple[dict | None, Evidence | None]:
        """解析 + 质量闸 + 指纹。返回 `(派生记录, 若需要提前结束的证据)`。"""
        from daedalus.understand.quality import check as quality_check
        try:
            from daedalus.understand.registry import default_registry
            reg = default_registry()
            with METRICS.timer("parse.duration"):
                out = reg.parse(res.body, meta={"url": res.final_url or task.target,
                                                "content_type": res.headers.get("content-type", ""),
                                                "encoding": res.encoding},
                                url=res.final_url or task.target)
        except Exception as e:
            METRICS.inc("parse.failed")
            return None, from_parse(False, 0.0, f"{type(e).__name__}: {e}")
        if not out.get("ok"):
            # 解析失败的证据也要带**结构化事实**：只写一句人话，事后既解释不清也判不回来
            return None, from_decision("parser_failed", "keep_raw",
                                       f"解析失败但原始已存：{out.get('error')}"[:200], stage="parse",
                                       size=len(res.body), format=out.get("format", ""),
                                       format_how=out.get("format_how", ""),
                                       tried=list(out.get("tried") or []))
        from daedalus.understand.normalize import normalize_record
        rec = normalize_record(dict(out, url=res.final_url or task.target))
        q = quality_check(rec)
        rec.update({"content_hash": content_hash(rec.get("text") or rec.get("title") or ""),
                    "simhash": clamp63(simhash64(rec.get("text") or "")),
                    "quality": q["score"], "quality_reasons": q["reasons"]})
        if not q["accepted"]:
            # **拒收 ≠ 重试**：质量不合格是"这页就长这样"→ 记 failed 并视为已处理
            METRICS.inc("parse.rejected")
            return rec, from_decision("empty_content", "quality_rejected",
                                      f"质量闸拒收（{q['score']}）：{'; '.join(q['reasons'])[:140]}",
                                      stage="parse",
                                      score=float(q["score"]), reasons=list(q["reasons"]),
                                      size=len(res.body), parser=out.get("parser", ""),
                                      format=out.get("format", ""),
                                      content_hash=rec.get("content_hash", ""))
        return rec, None

    # ── 交还（取消/主动放弃：不是失败）────────────────────────────
    def _release(self, task: Task, rep: RunReport, reason: str) -> None:
        """把任务交还队列。**不计 attempts、不进死信**——它只是这次没跑完。"""
        new_state = self.frontier.release(task, reason)
        rep.final_state = "released"
        rep.reason = reason
        if new_state == "lease_lost":
            rep.lease_lost = True
            rep.final_state = "lease_lost"
            rep.reason = "租约已易主：交还无效（本次结果作废）"
        self.ledger.record("failed" if rep.lease_lost else "unchanged", url=task.target,
                           detail=f"{rep.final_state}：{reason}"[:200])
        METRICS.inc("task.released")

    # ── 终态处理 ─────────────────────────────────────────────────
    def _finalize(self, task: Task, decision, rep: RunReport) -> None:
        rep.final_state = decision.terminal or TaskState.DEAD
        rep.reason = decision.reason
        if decision.evidence is not None:
            task = task.add_evidence(decision.evidence)

        if rep.final_state == TaskState.DONE:
            page = None
            if rep.derived:
                d = rep.derived
                # `title`/`text` 一起交出去：全文索引要在**打卡的同一个事务**里写
                # （`pages` 表不存正文，正文在原始层；索引是派生层的派生）
                page = {"url_hash": task.idempotency_key, "url": task.target,
                        "content_hash": d.get("content_hash"), "simhash": d.get("simhash"),
                        "size": task.bytes_done,
                        "title": str(d.get("title") or "")[:500],
                        "text": str(d.get("text") or "")[:200000]}
            artifact = rep.artifacts[-1] if rep.artifacts else None
            won = self.frontier.commit_done(task, artifact=artifact, page=page,
                                            evidence=task.evidence,
                                            bytes_done=task.bytes_done,
                                            seconds_done=task.seconds_done)
            if not won:
                # 租约易主：**DB 行一行都没动**（这正是 CAS 失守的正确语义）。
                # 注意：`lease_lost` 只出现在**报告**里，**不是** 任务状态值——
                # 任务状态集是有界的（见 `core/task.py` 的 TaskState），不给报告开后门。
                rep.lease_lost = True
                rep.final_state = "lease_lost"
                rep.reason = "租约已易主：本次结果作废（一行都没写）"
                self.ledger.record("failed", url=task.target, detail=rep.reason)
                return
            self.ledger.record("new", url=task.target,
                               detail=f"已捕获（{task.bytes_done} 字节）"
                                      + (f"；解析 {rep.derived.get('parser')}" if rep.derived else ""))
            self._spawn_children(task, rep)
            return

        # **路由的裁决就是终态**：不再由 frontier 二次判断"是否可重试"
        # （曾经 DEAD 被 mark_failed 改回 retry、POLICY_DENIED 被吞成 dead —— 门禁 D/F 抓到的真 bug）
        throttled = any(e.signal == "throttled" for e in task.evidence)
        new_state = self.frontier.mark_failed(task, rep.reason, state=rep.final_state,
                                              throttled=throttled, evidence=task.evidence)
        self.ledger.record("policy_denied" if new_state == TaskState.POLICY_DENIED else "failed",
                           url=task.target, detail=f"{new_state}：{rep.reason}"[:200])
        rep.final_state = new_state

    # ── 发现链接线（新事实 → 新任务）─────────────────────────────
    def _spawn_children(self, task: Task, rep: RunReport) -> None:
        if self.discovery is None or not rep.derived:
            return
        try:
            payload = {"links": rep.derived.get("links") or [],
                       "extra": rep.derived.get("extra") or {},
                       "items": rep.derived.get("items") or [],
                       "urls": rep.derived.get("urls") or []}
            found = self.discovery.from_parse(payload, url=task.target, source=task.task_id)
            accepted, rejected = self.discovery.filter_policy(found)
            for r in accepted[:50]:
                child = Task.acquire(r.url, goal=f"从 {task.target} 发现（{r.kind}）",
                                     parent_id=task.task_id, discovery_path=r.kind)
                done, why = self.frontier.enqueue(child)
                rep.children += int(bool(done))
            if rejected:
                self.ledger.record("policy_denied", url=task.target,
                                   detail=f"发现里 {len(rejected)} 条被策略拦下（未入队）")
        except Exception as e:
            logger.warning("发现链接线失败（不影响本次结果）：%s", e)


def _browser_request(n: int):
    """浏览器阶段要用的资源声明（构造一个 `ResourceRequest`，只声明浏览器槽位）。"""
    from daedalus.core.task import ResourceRequest
    return ResourceRequest(network=0, browser=int(n), cpu="medium", memory_mb=300)


def response_evidence(res) -> Evidence:
    """把 `NetResult` 的事件转成证据：**取第一条响应证据**（不做"信号过滤"——

    曾经这里过滤掉 `large_object`，结果 octet-stream 的大对象永远升不到制品环境
    （S6 门禁 C 用例抓到的真 bug）。事实是什么信号就是什么信号，改道与否交给 Router。
    """
    for ev in (res.evidence or []):
        return ev
    from daedalus.core.evidence import from_response
    return from_response(res.status, res.headers, size=res.size, final_url=res.final_url,
                         stage="direct", decision="capture")

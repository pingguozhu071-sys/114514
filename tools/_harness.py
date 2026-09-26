# -*- coding: utf-8 -*-
"""离线合成工作负载（基准与长跑共用）——**同一份工作负载，两次跑才可比**

为什么不用真网络做基准：
  * 本工程的 SSRF 闸**刻意**拦下 localhost（见 `net/ssrf_gate.py`），起本地服务会被自己拦掉；
  * 真网络基准的方差来自目标站，测不出"引擎自身的开销"；
  * 离线合成能固定住每一个变量（负载形状、字节数、并发数），所以**跨版本可比**——
    这正是 L7 要的"跑两次能出回归报告"。

它跑的是**真闭环**：`Task → Frontier → TaskRunner → 假咽喉 → 原始层 → 解析 → 质量闸 →
CAS 打卡 → 台账`，只把"出网"换成一个确定性的假响应。也就是说：router/capture/parse/store/
writer 全部真跑，只有 socket 是假的。

（这个文件是开发脚手架，不进产品运行时；放在 `tools/` 下并在文件头说明。）
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import random
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

__all__ = ["build_offline_stack", "payload_for", "run_batch", "BenchPayload"]


class FakeResp:
    """最小响应桩（与 `net/ssrf_gate.safe_open` 返回的形状一致：status/headers/read/close）。"""

    def __init__(self, status=200, headers=None, body=b""):
        self.status = int(status)
        self.headers = dict(headers or {})
        self._body = bytes(body)

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DeterministicFetcher:
    """确定性假咽喉：同一个 URL 永远给同一份字节（所以内容哈希稳定、去重可验证）。"""

    def __init__(self, body_for, *, latency: float = 0.0):
        self._body_for = body_for
        self.latency = float(latency)
        self.calls = 0
        self.bytes = 0
        self._lock = threading.Lock()

    def open(self, url, method="GET", headers=None, timeout=None):
        if self.latency:
            time.sleep(self.latency)
        ctype, body = self._body_for(str(url))
        with self._lock:
            self.calls += 1
            self.bytes += len(body)
        return FakeResp(200, {"Content-Type": ctype, "Content-Length": str(len(body))}, body)

    def is_allowed(self, url, *, fetch=True):
        return True, "ok"

    def stats(self):
        return {"calls": self.calls, "bytes": self.bytes}


class BenchPayload:
    """负载参数（**写进基准记录**：没有这些数字，两次跑就不可比）。"""

    def __init__(self, *, kind: str = "html", size: int = 24 * 1024, pages: int = 400,
                 workers: int = 8, latency: float = 0.0):
        self.kind = str(kind)
        self.size = int(size)
        self.pages = int(pages)
        self.workers = int(workers)
        self.latency = float(latency)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "size": self.size, "pages": self.pages,
                "workers": self.workers, "latency": self.latency}

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()[:12]


def payload_for(url: str, kind: str = "html", size: int = 24 * 1024) -> tuple[str, bytes]:
    """按 URL 派生**确定性**正文（同 URL 同字节 → 内容寻址去重可验证）。

    正文必须是"能通过质量闸"的真内容：随机词元 + 结构化标记，长度可控且可复现。
    """
    h = hashlib.sha256(str(url).encode()).hexdigest()
    # 这里的 `random.Random(种子)` **不是**安全用途，而是刻意要"同 URL 同字节"：
    # 基准要可复现、内容寻址去重要可验证（换一次正文就换个内容哈希，去重就测不出来了）。
    # 所以用固定种子的 Mersenne Twister 是对的——它**不承担任何安全属性**，
    # 真需要不可预测随机的地方一律走 `secrets`（见 `privacy/secrets.py`）。
    rnd = random.Random(int(h[:8], 16))
    if kind == "json":
        items = [{"id": f"{h[:6]}-{i}", "title": f"条目 {i} {h[6:12]}",
                  "value": rnd.random()} for i in range(max(8, size // 120))]
        return "application/json", json.dumps({"url": url, "items": items},
                                              ensure_ascii=False).encode()
    if kind == "binary":
        return "application/octet-stream", (h.encode() * (size // 64 + 1))[:size]
    body_words = ["数据", "采集", "引擎", "闭环", "证据", "路由", "捕获", "原始层"]
    chunks = []
    n = max(200, size // 24)
    for i in range(n):
        chunks.append(f"{body_words[i % len(body_words)]}{i} {h[i % len(h)]}")
    text = "".join(chunks)
    html = (f"<!DOCTYPE html><html><head><title>合成页 {h[:8]}</title>"
            f"<meta charset=\"utf-8\"></head><body><article><h1>合成页 {h[:8]}</h1>"
            f"<p>{text}</p></article></body></html>")
    return "text/html; charset=utf-8", html.encode()


def build_offline_stack(data_root, *, payload: BenchPayload | None = None,
                        worker_id: str = "bench", inflight=None,
                        enable_browser: bool = False,
                        extra_bodies: dict | None = None):
    """搭一套**完整闭环**（假咽喉）。返回 (stack, fetcher)。

    * `enable_browser=True`：路由把浏览器环境列进候选（默认不列，等价于"未装配"）；
    * `extra_bodies`：`{URL: (content_type, 字节)}`，给指定 URL 换掉合成正文
      （用于构造"空壳页/大对象"这类特定形态，而不必改负载参数）。
    """
    from daedalus.capture.discovery import Discovery
    from daedalus.capture.rawstore import RawStore
    from daedalus.core.limits import ResourcePlan
    from daedalus.core.registry import ResourceRegistry
    from daedalus.core.router import Environment, Router
    from daedalus.core.runner import TaskRunner
    from daedalus.env.media import MediaEnvironment
    from daedalus.env.net import NetEnvironment
    from daedalus.frontier.frontier import Frontier
    from daedalus.store.db import Database
    from daedalus.store.deadletter import DeadLetter
    from daedalus.store.writer import SingleWriter
    from daedalus.understand.ledger import ChangeLedger

    p = payload or BenchPayload()
    root = pathlib.Path(data_root)
    plan = ResourcePlan.from_config(None)
    override = {str(k): v for k, v in (extra_bodies or {}).items()}

    def body_for(url: str):
        hit = override.get(str(url))
        if hit is not None:
            return hit
        return payload_for(url, p.kind, p.size)

    fetcher = DeterministicFetcher(body_for, latency=p.latency)
    db = Database(root / "bench.db")
    dl = DeadLetter(path=root / "dead.jsonl", db=db)
    writer = SingleWriter(db, dead_letter=dl, batch_rows=plan.batch_rows,
                          flush_interval=plan.flush_interval,
                          queue_max=plan.queue_writer).start()
    frontier = Frontier(writer, max_queue=plan.queue_frontier, lease_timeout=60)
    net_env = NetEnvironment(fetcher, cache=None, cookies=None, retries=1, sleep=lambda s: None)
    store = RawStore(root / "data", db, writer)
    media_env = MediaEnvironment(fetcher, workdir=root / "media")
    ledger = ChangeLedger("bench")
    envs = (Environment.NETWORK, Environment.ARTIFACT) + \
        ((Environment.BROWSER,) if enable_browser else ())
    runner = TaskRunner(frontier=frontier, router=Router(enabled_environments=envs,
                                                        max_transitions=8),
                        net_env=net_env, media_env=media_env, store=store,
                        registry=ResourceRegistry(), ledger=ledger,
                        discovery=Discovery(base_hosts=("bench.local",), fetcher=fetcher),
                        worker_id=worker_id, sleep=lambda s: None, inflight=inflight)
    return dict(db=db, writer=writer, frontier=frontier, fetcher=fetcher, runner=runner,
                store=store, ledger=ledger, net_env=net_env, media_env=media_env,
                plan=plan, payload=p, root=root), fetcher


def run_batch(stack, count: int, *, workers: int = 8, host: str = "https://bench.local",
              start_index: int = 0) -> dict:
    """跑一批任务（多线程领取），返回可比的统计量。

    `start_index`：这一批从第几个 URL 开始。**长跑必须每轮换新的**——否则幂等键相同，
    第二轮起会被前沿直接去重（入队返回 False），跑出来是"一堆空轮"，什么也没测到
    （s7 门禁把这条抓出来了：516 轮里只有 1 轮真的跑了任务）。
    """
    from daedalus.core.task import Task
    frontier = stack["frontier"]
    runner = stack["runner"]
    t0 = time.monotonic()
    for i in range(int(start_index), int(start_index) + int(count)):
        task = Task.acquire(f"{host}/p{i}", goal="合成基准页")
        frontier.enqueue(task)

    results: list[tuple[str, float]] = []
    lock = threading.Lock()

    def worker(wid: str) -> None:
        while True:
            batch = frontier.claim_batch(1, wid)
            if not batch:
                return
            for task in batch:
                t1 = time.monotonic()
                rep = runner.run_one(task)
                with lock:
                    results.append((str(rep.final_state), time.monotonic() - t1))

    threads = [threading.Thread(target=worker, args=(f"bench-{i}",), name=f"bench-{i}")
               for i in range(max(1, int(workers)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seconds = max(1e-9, time.monotonic() - t0)

    durations = sorted(d for _, d in results)
    states: dict[str, int] = {}
    for st, _ in results:
        states[st] = states.get(st, 0) + 1

    def q(p: float) -> float:
        if not durations:
            return 0.0
        idx = min(len(durations) - 1, int(p * len(durations)))
        return durations[idx]

    bytes_total = int(stack["fetcher"].bytes)
    return {
        "tasks": len(results),
        "states": states,
        "seconds": round(seconds, 4),
        "pages_per_sec": round(len(results) / seconds, 3),
        "mb_per_sec": round(bytes_total / seconds / (1 << 20), 4),
        "bytes_total": bytes_total,
        "task_ms_p50": round(q(0.50) * 1000, 3),
        "task_ms_p95": round(q(0.95) * 1000, 3),
        "task_ms_p99": round(q(0.99) * 1000, 3),
        "fetcher_calls": int(stack["fetcher"].calls),
    }


def cleanup(stack, *, keep: bool = False) -> dict:
    """收尾：走**优雅关闭链**（顺带验证"排空后不丢行"）。"""
    from daedalus.core.lifecycle import ShutdownChain
    rep = ShutdownChain().run(writer=stack["writer"], db=stack["db"])
    return rep.to_dict()


def env_note() -> dict:
    """环境注记（基准记录里必须带上，否则两次跑的差异说不清是代码还是机器）。"""
    import platform
    try:
        cpus = os.cpu_count()
    except Exception:
        cpus = None
    return {"python": platform.python_version(), "machine": platform.machine(),
            "cpus": cpus, "platform": platform.system()}

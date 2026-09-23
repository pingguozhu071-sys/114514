# -*- coding: utf-8 -*-
"""S2 门禁：核心骨架（任务模型 / 证据路由 / 存储 / 前沿）

覆盖施工计划 S2 的门禁与《10-完工检测清单》的早期条目：
  A 任务模型与资源声明（**缺省即拒绝**）
  B 证据与路由（有界、可解释、不猜）
  C 存储（单写线程批提交、坏数据不拖垮整批、死信留痕）
  D 前沿（幂等入队、有界背压、租约、心跳续到期、CAS 三态、超时回收、重启能续、写库前钳位）

跑法（离线）：
    python tests/gates/s2_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s2_"))
os.environ["DAEDALUS_DATA_ROOT"] = str(_TMP)

_CASES: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


def skip(note: str) -> str:
    return f"SKIP {note}"


def make_stack(name: str, **kw):
    """搭一套 (db, deadletter, writer, frontier)，用完在用例里 stop。"""
    from daedalus.frontier.frontier import Frontier
    from daedalus.store.db import Database
    from daedalus.store.deadletter import DeadLetter
    from daedalus.store.writer import SingleWriter
    db = Database(_TMP / f"{name}.db")
    dl = DeadLetter(path=_TMP / f"{name}.deadletter.jsonl", db=db)
    writer = SingleWriter(db, dead_letter=dl, batch_rows=kw.pop("batch_rows", 1000),
                          flush_interval=kw.pop("flush_interval", 0.05)).start()
    fr = Frontier(writer, **kw)
    return db, dl, writer, fr


# ══════════════════════════════════════════════════════════════════
# A. 任务模型与资源声明
# ══════════════════════════════════════════════════════════════════
@case("A1 任务：幂等键稳定、未知 kind/state 被拒")
def t_task_model():
    from daedalus.core.task import Task, make_idempotency_key
    a = Task.acquire("https://example.com/x")
    b = Task.acquire("https://example.com/x")
    assert a.idempotency_key == b.idempotency_key, "同 kind+target 的幂等键必须一致"
    c = Task.acquire("https://example.com/y")
    assert c.idempotency_key != a.idempotency_key
    assert make_idempotency_key("acquire", "t") == make_idempotency_key("acquire", "t")
    for bad in ({"kind": "nope"}, {"state": "weird"}):
        try:
            Task(target="x", **bad)
            raise AssertionError(f"非法 {bad} 没被拒")
        except ValueError:
            pass
    # 派生任务默认**不声明网络资源** → 物理上没法出网
    d = Task.derive("artifact:abc")
    assert d.resources.network == 0, d.resources
    return ok(f"幂等键 {a.idempotency_key[:8]}…；derive 无网络资源")


@case("A2 资源：**缺省即拒绝**（浏览器默认容量 0）")
def t_registry_deny():
    from daedalus.core.registry import ResourceDenied, ResourceRegistry
    from daedalus.core.task import ResourceRequest
    reg = ResourceRegistry()
    assert reg.capacity("browser") == 0 and reg.capacity("process") == 0
    try:
        reg.require(ResourceRequest(network=1, browser=1))
        raise AssertionError("要浏览器居然通过了（应该被拒）")
    except ResourceDenied as e:
        assert "未启用" in str(e) or "容量 0" in str(e), str(e)
    reg.require(ResourceRequest(network=1))                   # 只声明网络 → 通过
    reg.register("browser", 2)                                # 启用浏览器环境（S8 才做）
    reg.require(ResourceRequest(network=1, browser=1))
    probs = reg.problems(ResourceRequest(browser=5))
    assert probs and "超过容量" in probs[0], probs
    return ok(f"浏览器默认拒绝；登记后放行；超容量报 {probs[0][:24]}…")


@case("A3 预算：各维度耗尽都能识别；限流不计入重试")
def t_budget():
    from daedalus.core.budget import Budget
    from daedalus.core.task import Task
    t = Task.acquire("https://example.com/x", budget=Budget(max_attempts=2, max_transitions=3,
                                                           max_seconds=10, max_bytes=100,
                                                           max_throttles=4))
    assert t.exhausted() is None
    t.attempts = 2
    assert t.exhausted() == "attempts"
    t.attempts = 0; t.transitions = 3
    assert t.exhausted() == "transitions"
    t.transitions = 0; t.throttles = 4
    assert t.exhausted() == "throttles"
    t.throttles = 0; t.seconds_done = 11
    assert t.exhausted() == "seconds"
    t.seconds_done = 0; t.bytes_done = 101
    assert t.exhausted() == "bytes"
    return ok("五个维度都识别")


# ══════════════════════════════════════════════════════════════════
# B. 证据与路由
# ══════════════════════════════════════════════════════════════════
@case("B1 证据：未知信号被拒；响应事实映射到正确信号")
def t_evidence():
    from daedalus.core.evidence import Evidence, from_parse, from_response
    try:
        Evidence(signal="made_up")
        raise AssertionError("未知信号没被拒")
    except ValueError:
        pass
    cases = [
        ({"status": 200, "headers": {"Content-Type": "text/html"}}, "ok"),
        ({"status": 429, "headers": {"Retry-After": "5"}}, "throttled"),
        ({"status": 404}, "permanent_failure"),
        ({"status": 500}, "transient_failure"),
        ({"status": 200, "headers": {"Content-Type": "application/vnd.apple.mpegurl"}}, "media_manifest"),
        ({"status": 200, "headers": {"Content-Type": "text/event-stream"}}, "stream"),
        ({"status": 200, "headers": {"Content-Type": "application/octet-stream"}, "size": 100}, "large_object"),
    ]
    for kw, expect in cases:
        got = from_response(**kw).signal
        assert got == expect, f"{kw} → {got}（期望 {expect}）"
    assert from_parse(False, 0.0, "bad html").signal == "parser_failed"
    assert from_parse(False).decision == "keep_raw", "解析失败也要保留原始（延期解释）"
    return ok(f"{len(cases)} 种响应事实映射正确")


@case("B2 路由：直连成功 / 空壳转候选 / 媒体转制品 / 失败分类")
def t_router_basic():
    from daedalus.core.evidence import from_response
    from daedalus.core.router import Environment, Router, Stage
    from daedalus.core.task import Task, TaskState
    r = Router(enabled_environments=(Environment.NETWORK, Environment.ARTIFACT))
    t = Task.acquire("https://example.com/x")
    assert r.start(t).next_stage == Stage.DIRECT
    d = r.on_evidence(t, from_response(200, {"Content-Type": "text/html"}))
    assert d.terminal == TaskState.DONE and d.reason, d
    d2 = r.on_evidence(t, from_response(200, {"Content-Type": "application/vnd.apple.mpegurl"}))
    assert d2.next_stage == Stage.ARTIFACT and d2.environment == Environment.ARTIFACT, d2
    d3 = r.on_evidence(t, from_response(404))
    assert d3.terminal == TaskState.DEAD, d3
    d4 = r.on_evidence(t, from_response(500))
    assert d4.next_stage == Stage.DIRECT, d4                # 瞬时失败 → 重试
    return ok("成功/媒体/永久失败/瞬时失败 四条路径都对")


@case("B3 路由：未启用环境**只记候选**（不偷偷起浏览器）")
def t_router_candidate():
    from daedalus.core.evidence import from_decision
    from daedalus.core.router import Environment, Router
    from daedalus.core.task import Task, TaskState
    r = Router(enabled_environments=(Environment.NETWORK,))    # 没启用浏览器
    t = Task.acquire("https://example.com/x")
    d = r.on_evidence(t, from_decision("empty_content", "", "内容是空壳"))
    assert d.terminal == TaskState.DEAD, d
    assert d.evidence is not None and d.evidence.decision == "browser_candidate", d.evidence
    assert "未启用" in d.reason, d.reason
    # 启用之后就会升级过去
    r2 = Router(enabled_environments=(Environment.NETWORK, Environment.BROWSER))
    d2 = r2.on_evidence(t, from_decision("empty_content", "", "内容是空壳"))
    assert d2.next_stage == "browser", d2
    return ok("未启用 → 记候选并终止；启用 → 升级")


@case("B4 路由：有界（转移超限 / 无匹配规则都不猜）")
def t_router_bounded():
    from daedalus.core.evidence import from_decision
    from daedalus.core.router import Router, Stage
    from daedalus.core.task import Task, TaskState
    r = Router(max_transitions=2)
    t = Task.acquire("https://example.com/x")
    t.transitions = 2
    d = r.on_evidence(t, from_decision("ok", "", "随便"))
    assert d.terminal == TaskState.DEAD and "上限" in d.reason, d
    # 无匹配规则：用一个"阶段无关"的信号在 classify 阶段（没有对应分支）
    t2 = Task.acquire("https://example.com/y")
    t2.policy["stage"] = Stage.CLASSIFY
    d2 = r.on_evidence(t2, from_decision("unknown_format", "", "不知道是什么"))
    # classify 阶段 → 仍会去直连（先看看再说，最便宜）；换个真"没规则"的组合：
    assert d2.next_stage == Stage.DIRECT, d2
    t3 = Task.acquire("https://example.com/z")
    t3.policy["stage"] = Stage.OBSERVE
    d3 = r.on_evidence(t3, from_decision("media_manifest", "", "在观察阶段看到清单"))
    assert d3.terminal == TaskState.DEAD and "不猜" in d3.reason, d3
    return ok("转移超限 → dead；无规则 → 明确失败不猜")


@case("B5 路由：限流走**独立计数**（不计重试）")
def t_router_throttle():
    from daedalus.core.budget import Budget
    from daedalus.core.evidence import from_response
    from daedalus.core.router import Router
    from daedalus.core.task import Task, TaskState
    r = Router()
    t = Task.acquire("https://example.com/x", budget=Budget(max_attempts=2, max_throttles=3))
    d = r.on_evidence(t, from_response(429, {"Retry-After": "5"}))
    assert d.next_stage == "direct" and "独立计数" in d.reason, d
    t.throttles = 2
    d2 = r.on_evidence(t, from_response(429))
    assert d2.terminal == TaskState.DEAD and "限流" in d2.reason, d2
    return ok("429 → 退避重试（独立计数）；连续到上限 → 死信")


# ══════════════════════════════════════════════════════════════════
# C. 存储
# ══════════════════════════════════════════════════════════════════
@case("C1 迁移：结构齐备、幂等、版本号来自脚本自身")
def t_migrations():
    from daedalus.frontier.migrations import LATEST_VERSION
    from daedalus.store.db import Database
    db = Database(_TMP / "c1.db")
    need = {"tasks", "task_evidence", "raw_artifacts", "pages", "cooldowns", "robots_cache",
            "deadletter", "settings", "downloads", "errors", "extracted"}
    assert not (need - db.table_names()), f"缺表 {need - db.table_names()}"
    # 版本号**不写死**：跟着 `LATEST_VERSION`（单一来源）——写死 1 会在加迁移时误报红
    assert db.user_version() == LATEST_VERSION, db.user_version()
    assert db.migrate() == [], "重复迁移产生了变更（不幂等）"
    return ok(f"{len(need)} 表 + 幂等（user_version={LATEST_VERSION}，由迁移脚本自己写）")


@case("C2 单写线程：批提交 + 坏任务不拖垮整批 + 死信留痕")
def t_writer_batch():
    db, dl, writer, _ = make_stack("c2", batch_rows=50, flush_interval=0.05)
    try:
        def good(n):
            def job(conn):
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (f"k{n}", "v"))
                return n
            return job

        def bad():
            def job(conn):
                conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("boom", "v"))
                raise RuntimeError("这条任务自己坏了")
            return job

        futs = [writer.submit(good(i), label="good") for i in range(30)]
        futs.append(writer.submit(bad(), label="bad"))
        futs += [writer.submit(good(i), label="good") for i in range(30, 60)]
        results = []
        for f in futs:
            try:
                results.append(("ok", f.result(timeout=10)))
            except Exception as e:
                results.append(("fail", f"{type(e).__name__}: {e}"))
        n_ok = sum(1 for r in results if r[0] == "ok")
        bad = [r[1] for r in results if r[0] == "fail"]
        assert n_ok == 60 and len(bad) == 1, f"坏任务拖垮了整批：ok={n_ok} 失败={bad}"
        st = writer.stats()
        assert st["jobs"] == 60 and st["failed"] == 1, st
        assert st["batches"] < 61, f"没有批提交（batches={st['batches']}）"
        con = db.connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
            boom = con.execute("SELECT COUNT(*) FROM settings WHERE key='boom'").fetchone()[0]
        finally:
            con.close()
        # savepoint 的语义：坏任务**自己的写入也被回滚**（只留好任务的 60 行），
        # 但它的 SQL 轨迹已经落进死信 → 数据没丢，人能看到
        assert n == 60, f"落库行数不对: {n}"
        assert boom == 0, "坏任务的写入没有被回滚（savepoint 没生效）"
        recs = dl.read()
        assert recs and recs[0].label == "bad", recs
        assert any("INSERT INTO settings" in s for s in recs[0].statements), \
            f"死信里没有 SQL 轨迹: {recs[0].statements}"
        return ok(f"60 好 + 1 坏：批 {st['batches']} 次、死信带 SQL 轨迹")
    finally:
        writer.stop()


@case("C3 死信可导出（人工核对后手动执行；不自动重放）")
def t_deadletter_export():
    from daedalus.store.deadletter import DeadLetter
    dl = DeadLetter(path=_TMP / "c3.deadletter.jsonl")
    dl.write("unit", "RuntimeError: x", ["UPDATE tasks SET state='retry' WHERE task_id='abc'"])
    n_stmt, n_rec = dl.export_sql(_TMP / "c3_export.sql")
    text = (_TMP / "c3_export.sql").read_text(encoding="utf-8")
    assert (n_stmt, n_rec) == (1, 1) and "UPDATE tasks" in text and "人工核对" in text
    return ok("导出 1 条语句（供人工执行）")


# ══════════════════════════════════════════════════════════════════
# D. 前沿
# ══════════════════════════════════════════════════════════════════
@case("D1 入队幂等：同一任务投 3 次只有 1 条")
def t_enqueue_idempotent():
    db, dl, writer, fr = make_stack("d1")
    try:
        from daedalus.core.task import Task
        t = Task.acquire("https://example.com/same")
        r = [fr.enqueue(t) for _ in range(3)]
        assert r[0][0] is True and r[1][0] is False and r[2][0] is False, r
        assert "重复" in r[1][1], r[1]
        assert fr.stats()["counts"].get("pending") == 1, fr.stats()
        return ok("3 次入队 → 1 条（第 2/3 次返回'重复任务'）")
    finally:
        writer.stop()


@case("D2 有界：前沿满了就拒绝（背压，不无限增长）")
def t_enqueue_bounded():
    db, dl, writer, fr = make_stack("d2", max_queue=1)
    try:
        from daedalus.core.task import Task
        assert fr.enqueue(Task.acquire("https://example.com/a"))[0] is True
        okk, why = fr.enqueue(Task.acquire("https://example.com/b"))
        assert okk is False and "前沿已满" in why, (okk, why)
        return ok(f"第二条被拒：{why[:30]}…")
    finally:
        writer.stop()


@case("D3 领取：租约字段齐全，且返回**本次真实的 leased_at**")
def t_claim():
    db, dl, writer, fr = make_stack("d3")
    try:
        from daedalus.core.task import Task, TaskState
        fr.enqueue(Task.acquire("https://example.com/a"))
        claimed = fr.claim_batch(5, "w1")
        assert len(claimed) == 1, claimed
        t = claimed[0]
        assert t.state == TaskState.LEASED and t.worker_id == "w1"
        assert t.leased_at and t.lease_expires and t.lease_expires > t.leased_at, vars(t)
        assert fr.claim_batch(5, "w2") == [], "同一条任务被领了两次"
        return ok(f"leased_at={t.leased_at:.0f} expires=+{t.lease_expires - t.leased_at:.0f}s")
    finally:
        writer.stop()


@case("D4 心跳：续的是**到期时间**，`leased_at` 一动不动")
def t_heartbeat():
    db, dl, writer, fr = make_stack("d4", lease_timeout=1.0)
    try:
        from daedalus.core.task import Task
        fr.enqueue(Task.acquire("https://example.com/a"))
        t = fr.claim_batch(1, "w1")[0]
        before_leased, before_exp = t.leased_at, t.lease_expires
        time.sleep(0.2)
        assert fr.heartbeat(t) is True
        assert t.leased_at == before_leased, "心跳改了 leased_at（会破坏 CAS 闭环）"
        assert t.lease_expires > before_exp, "到期时间没有延长"
        con = db.connect()
        try:
            row = con.execute("SELECT leased_at, lease_expires FROM tasks WHERE task_id=?",
                              (t.task_id,)).fetchone()
        finally:
            con.close()
        assert row["leased_at"] == before_leased and row["lease_expires"] > before_exp
        return ok("lease_expires 变长、leased_at 不变（库里也一致）")
    finally:
        writer.stop()


@case("D5 CAS：失守则**一行都不写**（产物与状态在同一事务）")
def t_cas_three_state():
    db, dl, writer, fr = make_stack("d5")
    try:
        from daedalus.core.task import Task
        fr.enqueue(Task.acquire("https://example.com/a"))
        t = fr.claim_batch(1, "w1")[0]
        art = {"sha256": "a" * 64, "url": t.target, "size": 10, "mime": "text/html",
               "path": "aa/aa.bin"}
        page = {"url_hash": "h1", "url": t.target, "content_hash": "c" * 16,
                "simhash": (1 << 64) - 1}          # 故意给 64 位值：写路径必须钳到 63 位
        assert fr.commit_done(t, artifact=art, page=page) is True, "正常提交失败"
        # 用**过期令牌**再提交一次 → 必须是 False，且不能再写产物
        stale = Task.from_row({"task_id": t.task_id, "kind": t.kind, "target": t.target,
                               "idempotency_key": t.idempotency_key, "state": "leased",
                               "worker_id": "w1", "leased_at": t.leased_at - 999,
                               "attempts": 0, "throttles": 0, "transitions": 0,
                               "bytes_done": 0, "seconds_done": 0.0,
                               "created_at": 0, "updated_at": 0})
        art2 = dict(art, sha256="b" * 64)
        assert fr.commit_done(stale, artifact=art2) is False, "失守的 CAS 居然写进去了"
        con = db.connect()
        try:
            n_art = con.execute("SELECT COUNT(*) FROM raw_artifacts").fetchone()[0]
            n_page = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            n_done = con.execute("SELECT COUNT(*) FROM tasks WHERE state='done'").fetchone()[0]
            sim = con.execute("SELECT simhash FROM pages").fetchone()[0]
        finally:
            con.close()
        assert n_art == 1 and n_page == 1 and n_done == 1, \
            f"失守仍写入了：art={n_art} page={n_page} done={n_done}"
        assert sim <= (1 << 63) - 1, f"写路径没有钳位：{sim}"
        return ok("正常=1 条；失守=一行不写；写路径强制钳 63 位")
    finally:
        writer.stop()


@case("D6 失败记账：限流只加 throttles；可重试 → retry；不可重试 → dead")
def t_mark_failed():
    db, dl, writer, fr = make_stack("d6")
    try:
        from daedalus.core.budget import Budget
        from daedalus.core.task import Task, TaskState
        fr.enqueue(Task.acquire("https://example.com/a", budget=Budget(max_attempts=3)))
        t = fr.claim_batch(1, "w1")[0]
        assert fr.mark_failed(t, "429 被限流", throttled=True) == TaskState.RETRY
        con = db.connect()
        try:
            row = con.execute("SELECT attempts, throttles, state FROM tasks").fetchone()
        finally:
            con.close()
        assert row["attempts"] == 0 and row["throttles"] == 1, dict(row)
        # 不可重试 → dead
        t2 = fr.claim_batch(1, "w2")[0]
        assert fr.mark_failed(t2, "404", retryable=False) == TaskState.DEAD
        return ok("限流只加 throttles（attempts 不变）；404 → dead")
    finally:
        writer.stop()


@case("D7 看门狗：过期租约被回收（能重试回 retry，超限转 dead）")
def t_watchdog_reap():
    db, dl, writer, fr = make_stack("d7", lease_timeout=0.2)
    try:
        from daedalus.core.budget import Budget
        from daedalus.core.task import Task
        fr.enqueue(Task.acquire("https://example.com/a", budget=Budget(max_attempts=5)))
        t = fr.claim_batch(1, "w1")[0]
        time.sleep(0.35)
        assert fr.watchdog_scan()["expired_leases"] == 1, fr.watchdog_scan()
        again = fr.claim_batch(1, "w2")
        assert again and again[0].task_id == t.task_id, "过期租约没有被回收重派"
        assert again[0].attempts == 1, f"回收没有记 attempts: {again[0].attempts}"
        # 超限的情形：把 attempts 顶到上限再让它过期
        con = db.connect()
        try:
            con.execute("UPDATE tasks SET attempts=4 WHERE task_id=?", (t.task_id,))
        finally:
            con.close()
        time.sleep(0.35)
        assert fr.claim_batch(1, "w3") == [], "超限任务不该再被领取"
        con = db.connect()
        try:
            st = con.execute("SELECT state FROM tasks WHERE task_id=?", (t.task_id,)).fetchone()[0]
        finally:
            con.close()
        assert st == "dead", f"超限应转 dead，实际 {st}"
        return ok("过期 → retry(attempts+1)；超限 → dead")
    finally:
        writer.stop()


@case("D8 重启能续：换一套栈打开同一个库，未完成任务仍可领（kill 后恢复）")
def t_restart_resume():
    db, dl, writer, fr = make_stack("d8", lease_timeout=0.2)
    from daedalus.core.task import Task
    fr.enqueue(Task.acquire("https://example.com/a"))
    fr.enqueue(Task.acquire("https://example.com/b"))
    t = fr.claim_batch(1, "w1")[0]
    writer.stop()                                   # ← 模拟进程死掉（没打卡、没释放租约）
    from daedalus.frontier.frontier import Frontier
    from daedalus.store.writer import SingleWriter
    w2 = SingleWriter(db, dead_letter=dl, batch_rows=10, flush_interval=0.05).start()
    fr2 = Frontier(w2, lease_timeout=0.2)
    try:
        time.sleep(0.35)                            # 等租约过期
        claimed = fr2.claim_batch(5, "w2")
        ids = {c.task_id for c in claimed}
        assert t.task_id in ids, f"重启后拿不回在途任务（拿到 {ids}）"
        assert len(ids) == 2, f"应能领到 2 条（含被回收的那条），实际 {len(ids)}"
        return ok("重启后：在途任务被回收并重派（不丢）")
    finally:
        w2.stop()


@case("D9 证据链落库：每次转移都能说出为什么")
def t_evidence_persist():
    db, dl, writer, fr = make_stack("d9")
    try:
        from daedalus.core.evidence import from_response
        from daedalus.core.task import Task
        t = Task.acquire("https://example.com/a")
        t = t.add_evidence(from_response(200, {"Content-Type": "text/html"}))
        fr.enqueue(t)
        claimed = fr.claim_batch(1, "w1")[0]
        # 提交时再补一条"捕获完成"的证据（claim 回来的任务不带历史证据——证据在库里）
        from daedalus.core.evidence import from_decision
        fr.commit_done(claimed, evidence=[from_decision("ok", "capture", "已捕获并落原始层")])
        con = db.connect()
        try:
            rows = con.execute("SELECT signal, reason FROM task_evidence").fetchall()
        finally:
            con.close()
        assert len(rows) >= 2, [dict(r) for r in rows]
        assert any(r["signal"] == "ok" for r in rows)
        assert any("已捕获" in (r["reason"] or "") for r in rows), [dict(r) for r in rows]
        return ok(f"{len(rows)} 条证据落库（入队 1 条 + 提交 1 条）")
    finally:
        writer.stop()


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S2 门禁 · 数据根={_TMP}\n" + "─" * 68)
    for name, fn in _CASES:
        try:
            note = str(fn())
            status = "SKIP" if note.startswith("SKIP") else "PASS"
            skips += status == "SKIP"
            print(f"[{status}] {name}\n        {note}")
        except Exception as e:
            fails += 1
            print(f"[FAIL] {name}\n        {type(e).__name__}: {e}")
    print("─" * 68)
    print(f"共 {len(_CASES)} 项：PASS {len(_CASES) - fails - skips} / SKIP {skips} / FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

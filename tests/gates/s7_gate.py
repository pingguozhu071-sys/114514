# -*- coding: utf-8 -*-
"""S7 门禁：观测与运维

覆盖（每一项都对应一个"没有它就会出的事"）：
  A 指标面     A1 摘要键数 ≥15 且含 p50/p95/p99｜A2 直方图有界（样本不随流量增长）
               A3 百分位数值正确（已知分布）｜A4 序列基数闸生效且可见
               A5 同名不同类型立刻报错（调用方 bug 不许静默混用）
  B 日志       B1 JSON 字段稳定｜B2 先脱敏后落盘｜B3 轮转产出备份｜B4 重复 setup 不重复写
               B5 中文目录路径不炸
  C 阈值告警   C1 队列/失败率/磁盘/内存/延迟五类都能报警｜C2 指标缺失报 unknown（不是 ok）
  D 资源计划   D1 Σ(池规模×单任务峰值) ≤ 4GB 且算式可读｜D2 无界队列被拒｜D3 写线程 ≠1 被拒
  E 下钻       E1 单任务视图（任务+证据+原始层+派生层）｜E2 台账五态｜E3 JSONL 导出
               E4 不存在的任务如实说｜E5 缺失表如实说
  F 生命周期   F1 在飞强引用（对象被持有、能取消）｜F2 取消 = 交还队列（**不计失败**）
               F3 关闭链顺序执行且幂等｜F4 某一步抛异常不阻断后续｜F5 线程数回落到基线
  G 回归钉子   G1 CAS 打卡不被批提交拖慢（跑赢 flush_interval）｜G2 背压在指标里可见
  H 长跑结论   H1 soak 报告结构完整且结论可读（离线快速冒烟，不跑满 1h）

跑法（离线；不联网）：
    python tests/gates/s7_gate.py       # 退出码 0 = 全通过
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s7_"))
os.environ["DAEDALUS_DATA_ROOT"] = str(_TMP)

_CASES: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


def build_stack(name: str, **kw):
    """借 `tools/_harness.py` 的完整闭环栈（与基准/长跑用同一套，避免"门禁测的和跑的不是一个东西"）"""
    from _harness import BenchPayload, build_offline_stack
    payload = BenchPayload(pages=kw.pop("pages", 40), **kw)
    stack, fetcher = build_offline_stack(_TMP / name, payload=payload)
    return stack, fetcher


def fresh_metrics(**kw):
    """门禁用独立指标实例，避免用例之间互相污染计数（全局 METRICS 仍照常工作）。"""
    from daedalus.obs.metrics import Metrics
    return Metrics(**kw)


# ══════════════════════════════════════════════════════════════════
@case("A1 指标摘要：≥15 项且含 p50/p95/p99")
def t_metrics_summary():
    from daedalus.obs.metrics import METRICS
    s = METRICS.summary()
    need = ("pages_per_sec", "mb_per_sec", "net_latency_p50", "net_latency_p95",
            "net_latency_p99", "task_duration_p50", "task_duration_p95",
            "task_duration_p99", "queue_blocked_puts", "active_threads", "rss_mb",
            "disk_free_mb", "db_flush_p95", "series_total", "tasks_total")
    missing = [k for k in need if k not in s]
    assert not missing, f"缺指标：{missing}"
    assert len(s) >= 15, f"摘要项数不足：{len(s)}"
    return ok(f"{len(s)} 项；含 p50/p95/p99（{s['net_latency_p50']}/{s['net_latency_p95']}"
              f"/{s['net_latency_p99']}）")


@case("A2 直方图有界：观测 1 万次，样本数不超容量（内存不随流量涨）")
def t_metrics_bounded():
    m = fresh_metrics()
    for i in range(10000):
        m.observe("x.latency", i * 0.001)
    h = m.histogram("x.latency")
    assert h["count"] == 10000, h
    assert h["samples"] == m.hist_capacity, h
    assert h["dropped"] == 10000 - m.hist_capacity, h
    return ok(f"count={h['count']} / samples={h['samples']}（容量 {m.hist_capacity}）"
              f" / dropped={h['dropped']}")


@case("A3 百分位正确：1..100 的 p50/p90/p95/p99 落在应有值上")
def t_metrics_percentiles():
    m = fresh_metrics()
    for i in range(1, 101):
        m.observe("x.dist", float(i))
    h = m.histogram("x.dist")
    assert abs(h["p50"] - 50.5) < 0.6, h
    assert abs(h["p95"] - 95.05) < 0.6, h
    assert abs(h["p99"] - 99.01) < 0.6, h
    assert h["min"] == 1.0 and h["max"] == 100.0, h
    return ok(f"p50={h['p50']:.2f} p95={h['p95']:.2f} p99={h['p99']:.2f}")


@case("A4 序列基数闸：超高基数标签被丢弃且计数可见（防指标面自己 OOM）")
def t_metrics_cardinality():
    m = fresh_metrics(max_series=64)
    for i in range(500):
        m.inc("x.explode", label=f"k{i}")
    total = m.counter("metrics.series_total")
    over = m.counter("metrics.series_overflow")
    assert total <= 64, total
    assert over == 500 - int(total), (total, over)
    assert m.summary()["series_overflow"] == over
    return ok(f"序列上限 {int(total)}；丢弃 {int(over)} 条并可见")


@case("A5 同名不同类型立刻报错（调用方 bug 不许静默混用）")
def t_metrics_type_conflict():
    m = fresh_metrics()
    m.inc("x.mixed")
    try:
        m.set("x.mixed", 1)
        raise AssertionError("同名不同类型竟然被接受了")
    except ValueError as e:
        return ok(f"已拒绝：{str(e)[:60]}")


@case("B1/B2 JSON 日志：字段稳定 + 先脱敏后落盘")
def t_logs_json_and_sanitize():
    from daedalus.obs.logs import log_event, setup_logging
    d = _TMP / "logs_b"
    st = setup_logging({"level": "DEBUG", "dir": str(d), "json": True,
                        "max_bytes": 1 << 20, "backup_count": 2}, force=True)
    assert st["file"] and pathlib.Path(st["file"]).parent == d, st
    lg = logging.getLogger("s7.test")
    log_event(lg, "task.done", "完成一个任务", task_id="t1",
              url="https://example.com/a?token=PLAINTOKEN", bytes=10)
    try:
        raise ValueError("boom")
    except ValueError:
        lg.exception("炸一下", extra={"event": "boom"})
    logging.shutdown()
    lines = [json.loads(x) for x in pathlib.Path(st["file"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    rec = [x for x in lines if x.get("event") == "task.done"]
    assert rec, lines
    keys = {"ts", "level", "logger", "msg", "thread", "event", "task_id", "fields"}
    assert keys <= set(rec[0]), (keys - set(rec[0]))
    assert "PLAINTOKEN" not in json.dumps(lines, ensure_ascii=False), "明文凭据进了日志"
    assert any("exc" in x for x in lines), "异常没有进 exc 字段"
    return ok(f"{len(lines)} 行 JSON，字段齐全，token 已脱敏，异常在 exc")


@case("B3 轮转：写超 max_bytes 后产出备份文件")
def t_logs_rotation():
    from daedalus.obs.logs import setup_logging
    d = _TMP / "logs_c"
    st = setup_logging({"level": "INFO", "dir": str(d), "json": True,
                        "max_bytes": 2048, "backup_count": 2}, force=True)
    lg = logging.getLogger("s7.rot")
    for i in range(400):
        lg.info("轮转填充 %d %s", i, "x" * 40, extra={"event": "fill"})
    logging.shutdown()
    files = sorted(p.name for p in d.iterdir())
    assert len(files) >= 2, f"没有备份文件：{files}"
    return ok(f"产出 {files}")


@case("B4 重复 setup 不产生重复行")
def t_logs_idempotent():
    from daedalus.obs.logs import setup_logging
    d = _TMP / "logs_d"
    setup_logging({"level": "INFO", "dir": str(d), "json": True}, force=True)
    setup_logging({"level": "INFO", "dir": str(d), "json": True})     # 第二次（幂等）
    lg = logging.getLogger("s7.idem")
    lg.info("只该出现一次", extra={"event": "once"})
    logging.shutdown()
    text = "".join(p.read_text(encoding="utf-8") for p in sorted(d.iterdir()) if p.is_file())
    assert text.count("只该出现一次") == 1, text.count("只该出现一次")
    return ok("同一 handler 只挂一份，日志无重复")


@case("B5 中文目录路径不炸（Windows 默认 cp936 的坑）")
def t_logs_cjk_path():
    from daedalus.obs.logs import setup_logging
    d = _TMP / "日志目录" / "深一层"
    st = setup_logging({"level": "INFO", "dir": str(d), "json": True}, force=True)
    logging.getLogger("s7.cjk").info("中文路径写入 ✓", extra={"event": "cjk"})
    logging.shutdown()
    assert pathlib.Path(st["file"]).exists(), st
    assert "中文路径写入" in pathlib.Path(st["file"]).read_text(encoding="utf-8")
    return ok(f"写入 {pathlib.Path(st['file']).parent.name}/…，内容可读")


@case("C1 告警：队列积压/失败率/磁盘/内存/延迟五类都能报")
def t_alerts_all_kinds():
    from daedalus.core.limits import ResourcePlan
    from daedalus.obs.alerts import Thresholds, evaluate, worst_level
    th = Thresholds(failure_rate_pct=10.0, disk_free_pct=20.0, rss_mb=1000.0,
                    latency_p95_ms=100.0, queue_depth_pct=50.0)
    plan = ResourcePlan()
    alarms = evaluate({"tasks_done": 50, "tasks_failed": 50, "rss_mb": 5000.0,
                       "net_latency_p95": 0.5, "disk_free_pct": 2.0, "series_overflow": 3},
                      thresholds=th, queue_limits=plan.queues(),
                      queue_depths={"frontier": 190000, "writer": 0})
    levels = {a.key: a.level for a in alarms}
    assert levels["queue.depth.frontier"] in ("warn", "critical"), levels
    assert levels["task.failure_rate"] == "critical", levels
    assert levels["disk.free_pct"] == "critical", levels
    assert levels["proc.rss_mb"] == "critical", levels
    assert levels["net.latency_p95_ms"] == "critical", levels
    assert levels["metrics.series_overflow"] == "warn", levels
    assert worst_level(alarms) == "critical"
    return ok(f"六类告警齐全，最严重级别 {worst_level(alarms)}；文案示例："
              f"{alarms[0].message[:48]}")


@case("C2 指标缺失报 unknown（看不见 ≠ 没问题）")
def t_alerts_unknown():
    from daedalus.obs.alerts import Thresholds, evaluate
    alarms = evaluate({}, thresholds=Thresholds(), queue_limits={}, queue_depths={})
    unknown = [a for a in alarms if a.level == "unknown"]
    assert unknown, alarms
    assert all("看不见" in a.message or "未采样" in a.message or "无法评估" in a.message
               for a in unknown), [a.message for a in unknown]
    return ok(f"{len(unknown)} 项如实报 unknown（不是假 ok）")


@case("D1 资源计划：Σ(池规模×单任务峰值) ≤ 4GB，算式可读")
def t_plan_memory_arithmetic():
    from daedalus.core.limits import MEMORY_BUDGET_MB, ResourcePlan
    plan = ResourcePlan.from_config(None).validate()
    total = plan.memory_total_mb()
    assert total <= MEMORY_BUDGET_MB, (total, MEMORY_BUDGET_MB)
    txt = plan.memory_arithmetic()
    for token in ("128×", "20×", "≤ 4096 MB"):
        assert token in txt, (token, txt)
    d = plan.to_dict()
    assert d["memory_headroom_mb"] > 0, d
    return ok(txt)


@case("D2/D3 资源计划自检：无界队列、写线程数不对 → 一律拒绝")
def t_plan_violations():
    from daedalus.core.limits import PlanViolation, ResourcePlan
    bad = []
    try:
        ResourcePlan(queue_parse_store=0).validate()
    except PlanViolation as e:
        bad.append(str(e))
    try:
        ResourcePlan(writer_threads=2).validate()
    except PlanViolation as e:
        bad.append(str(e))
    try:
        ResourcePlan(batch_rows=50).validate()
    except PlanViolation as e:
        bad.append(str(e))
    assert len(bad) == 3, bad
    return ok("；".join(b[:34] for b in bad))


@case("E1/E2/E3/E4/E5 下钻：单任务视图 + 五态台账 + JSONL 导出 + 缺数据如实说")
def t_drilldown():
    from _harness import run_batch
    from daedalus.obs.drilldown import Drilldown
    stack, _ = build_stack("e", pages=6)
    try:
        run_batch(stack, 6, workers=2)
        dd = Drilldown(stack["db"], ledger=stack["ledger"])
        assert dd.available()["tasks"] and dd.available()["task_evidence"], dd.available()
        row = dd.tasks_overview(limit=1)[0]
        view = dd.task_view(row["task_id"])
        assert view["found"] and view["task"]["state"] == "done", view
        assert view["evidence"] and view["evidence"][0]["facts"] is not None, view["evidence"][:1]
        assert view["pages"], "派生层没连上"
        counts = dd.ledger_counts()
        assert counts["new"] == 6, counts
        line = dd.export_jsonl().splitlines()[0]
        assert "ledger" in line and "counts" in line, line
        miss = dd.task_view("不存在的任务")
        assert miss["found"] is False and "没有这个任务" in miss["note"], miss
        # 缺表如实说：给一个空库
        from daedalus.store.db import Database
        empty = Database(_TMP / "empty.db")
        empty_conn = empty.connect()
        empty_conn.execute("DROP TABLE IF EXISTS task_evidence")   # 只读面探测用
        empty_conn.close()
        av = Drilldown(empty).available()
        assert av["task_evidence"] is False, av
        return ok(f"视图={view['summary'][:40]}…；台账 {counts}；缺失表已如实标注")
    finally:
        stack["writer"].stop()


@case("F1/F2 在飞强引用 + 取消 = 交还队列（不计失败）")
def t_inflight_cancel():
    from daedalus.core.lifecycle import InflightRegistry
    from daedalus.core.task import Task
    reg = InflightRegistry()
    stack, _ = build_stack("f", pages=3)
    stack["runner"].inflight = reg
    try:
        t = Task.acquire("https://bench.local/cancel-me")
        stack["frontier"].enqueue(t)
        task = stack["frontier"].claim_batch(1, "w1")[0]
        reg.add(task)
        assert reg.get(task.task_id) is task, "在飞任务没有被强引用持有"
        assert len(reg) == 1
        reg.cancel(task.task_id)                       # 关闭时标记取消
        rep = stack["runner"].run_one(task)
        assert rep.final_state == "released", rep.to_dict()
        st = stack["frontier"].stats()
        assert st["in_flight"] == 0, st                # 不再是"在飞"
        conn = stack["db"].connect(readonly=True)
        try:
            r = conn.execute("SELECT state, attempts, throttles FROM tasks WHERE task_id=?",
                             (task.task_id,)).fetchone()
        finally:
            conn.close()
        # 交还 = retry 且**不计失败**（这不是重试、更不是死信）
        assert r["state"] == "retry" and r["attempts"] == 0 and r["throttles"] == 0, dict(r)
        assert st["claimable"] == 1, st
        assert len(reg) == 0, "运行结束后没从在飞表里移除"
        return ok("被持有的任务在安全点退出并交还队列（attempts=0，不是失败）")
    finally:
        stack["writer"].stop()


@case("F3/F4/F5 关闭链：顺序执行、幂等、某步抛异常不阻断、线程回落")
def t_shutdown_chain():
    from daedalus.core.lifecycle import InflightRegistry, ShutdownChain
    from daedalus.exec.pools import ManagedPool
    from daedalus.obs.samplers import ProcessSampler
    from daedalus.store.db import Database
    from daedalus.store.writer import SingleWriter

    db = Database(_TMP / "f5.db")
    writer = SingleWriter(db, batch_rows=20, flush_interval=0.05).start()
    pool = ManagedPool("s7pool", 3).start()
    sampler = ProcessSampler(interval=0.2, disk_path=_TMP).start()
    reg = InflightRegistry()
    before = threading.active_count()

    def boom():                                        # 故意失败的步骤
        raise RuntimeError("这一步坏了")

    chain = ShutdownChain(inflight=reg)
    rep = chain.run(pools=(pool,), writer=writer, sampler=sampler, db=db,
                    extra=[("custom_boom", boom, 1.0), ("custom_ok", lambda: {"ok": 1}, 1.0)])
    names = [s["name"] for s in rep.steps]
    # 顺序写死：取消在飞 → 排空池 → 停采样 → 冲刷写线程 → 关库（顺序错了就有数据损失）
    assert names[:4] == ["cancel_inflight", "pool_shutdown:s7pool", "sampler_stop",
                         "writer_flush"], names
    assert "db_close" in names, names
    assert names[-2:] == ["custom_boom", "custom_ok"], names        # 失败不阻断后续
    assert rep.steps[-2]["ok"] is False and rep.steps[-1]["ok"] is True, rep.steps[-2:]
    assert rep.ok is False, rep.to_dict()
    time.sleep(0.3)
    after = threading.active_count()
    assert after <= before, (before, after)
    rep2 = chain.run(pools=(pool,))
    assert rep2.steps[0]["name"] == "already_closed", rep2.to_dict()
    return ok(f"顺序 {names}；失败不阻断；幂等；线程 {before}→{after}；"
              f"残留 {rep.leaked_threads or '无'}")


@case("G1 回归钉子：CAS 打卡不被批提交拖慢（run_now 必须跑赢 flush_interval）")
def t_writer_sync_not_delayed():
    from daedalus.store.db import Database
    from daedalus.store.writer import SingleWriter
    db = Database(_TMP / "g1.db")
    # flush_interval 故意设成 1s（默认档）：同步作业**不该**等它攒批
    w = SingleWriter(db, batch_rows=1000, flush_interval=1.0).start()
    try:
        t0 = time.monotonic()
        for i in range(5):
            w.run_now(lambda conn: conn.execute("SELECT 1").fetchone(), label="probe")
        each = (time.monotonic() - t0) / 5
        assert each < 0.2, f"同步作业被攒批拖了 {each * 1000:.0f} ms（应远小于 1s）"
        return ok(f"5 次同步提交平均 {each * 1000:.1f} ms（flush_interval=1.0s）")
    finally:
        w.stop()


@case("G2 背压可见：队列塞满 → put 返回 False 且 queue.blocked_puts 增加")
def t_backpressure_visible():
    from daedalus.exec.pools import BoundedQueue
    from daedalus.obs.metrics import METRICS
    q = BoundedQueue("s7q", 3)
    for i in range(3):
        assert q.put(i, timeout=0.05) is True
    before = METRICS.counter("queue.blocked_puts", name="s7q")
    assert q.put(99, timeout=0.05) is False, "满了还放进去了"
    after = METRICS.counter("queue.blocked_puts", name="s7q")
    assert after == before + 1, (before, after)
    assert METRICS.gauge("queue.depth", name="s7q") == 3
    return ok(f"满队列拒绝 + 计数 {before:.0f}→{after:.0f}；深度 gauge={METRICS.gauge('queue.depth', name='s7q')}")


@case("H1 长跑报告：结论结构完整且可读（离线快速冒烟）")
def t_soak_report():
    import subprocess
    out = _TMP / "soak"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "soak.py"),
                        "--minutes", "0.15", "--tasks-per-round", "60", "-w", "4",
                        "--sample-interval", "3", "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180, cwd=str(ROOT))
    assert r.returncode == 0, f"soak 退出码 {r.returncode}\n{r.stdout[-1500:]}\n{r.stderr[-500:]}"
    rep = json.loads((out / "soak_report.json").read_text(encoding="utf-8"))
    names = {c["name"] for c in rep["checks"]}
    need = {"rss_slope", "rss_growth", "handle_slope", "no_lost_rows", "own_threads_zero",
            "metrics_bounded", "queues_drained", "shutdown_ok"}
    assert need <= names, need - names
    assert rep["all_ok"] is True, [c for c in rep["checks"] if not c["ok"]]
    assert rep["tasks_run"] > 0 and rep["ledger_counts"]["new"] == rep["tasks_run"], rep
    assert (out / "soak_samples.jsonl").exists()
    return ok(f"{rep['tasks_run']} 任务 / {rep['rounds']} 轮；"
              f"rss 斜率 {rep['rss_slope_mb_per_min']} MB/分；跑丢 0 条；全部检查通过")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S7 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

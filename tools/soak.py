# -*- coding: utf-8 -*-
"""长跑冒烟（清单 L5 / L6）：内存斜率平稳、无句柄/线程泄漏、无丢行

跑法：
    python tools/soak.py --minutes 1          # 快速冒烟（先看斜率对不对）
    python tools/soak.py --minutes 60         # 1 小时（清单 L5 的要求）
    python tools/soak.py --minutes 1440       # 24 小时（L6，需机主点头；PENDING P19）
    python tools/soak.py --minutes 60 --report out/soak_1h.json

判据（全部写进报告，**每一条都要能自证**）：
  * **内存斜率**：丢掉前 `--warmup-minutes`（分配器预热期）后对 (t, rss) 做最小二乘，
    斜率 < `--rss-slope-mb-per-min`（默认 1 MB/分钟）且总增量 < `--rss-growth-budget-mb`
    （默认 256MB）才算平稳；
  * **句柄/线程**：句柄斜率 < 1/分钟；本进程自己的线程（`dae-*`）在关闭后必须归零；
  * **不丢行**：跑过的任务数 == 台账里 new 的条数（**这是最硬的一条**：长跑最容易悄悄丢数据）；
  * **观测面自身有界**：指标序列数不涨、直方图样本数不超过容量（观测不能自己长成 OOM）；
  * **队列回落**：关闭后各队列深度回到 0。

中断（Ctrl+C）也会先把已采到的样本与结论落盘，报告里标 `interrupted=true`——
"跑到一半被打断"和"跑完了没问题"是两件事，不许混。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))


def _slope_per_min(samples: list[tuple[float, float]]) -> float | None:
    """最小二乘斜率（单位：值/分钟）。样本不足返回 None（**不编造**）。"""
    if len(samples) < 3:
        return None
    t0 = samples[0][0]
    xs = [(t - t0) / 60.0 for t, _ in samples]
    ys = [v for _, v in samples]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daedalus 长跑冒烟（离线合成负载）")
    ap.add_argument("--minutes", type=float, default=60.0, help="跑多久（分钟）")
    ap.add_argument("--tasks-per-round", type=int, default=200, help="每轮任务数")
    ap.add_argument("-w", "--workers", type=int, default=8, help="并发领取线程数")
    ap.add_argument("--size", type=int, default=24 * 1024, help="每页字节数")
    ap.add_argument("--kind", default="html", choices=("html", "json", "binary"))
    ap.add_argument("--sample-interval", type=float, default=10.0, help="采样间隔（秒）")
    ap.add_argument("--warmup-minutes", type=float, default=None,
                    help="预热期（默认取总时长的 10%%）")
    ap.add_argument("--rss-slope-mb-per-min", type=float, default=1.0, help="内存斜率上限")
    ap.add_argument("--rss-growth-budget-mb", type=float, default=256.0, help="内存总增量上限")
    ap.add_argument("--data-root", default=None, help="数据根（默认临时目录）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <工程>/out）")
    ap.add_argument("--report", default=None, help="报告 JSON 路径")
    args = ap.parse_args(argv)

    import shutil
    import tempfile

    from _harness import BenchPayload, build_offline_stack, cleanup, run_batch
    from daedalus.core.lifecycle import InflightRegistry, ShutdownChain
    from daedalus.obs.metrics import METRICS
    from daedalus.obs.samplers import ProcessSampler

    out_dir = pathlib.Path(args.out) if args.out else (ROOT / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "soak_samples.jsonl"
    report_path = pathlib.Path(args.report) if args.report else (out_dir / "soak_report.json")

    total_seconds = max(5.0, args.minutes * 60.0)
    warmup = (args.warmup_minutes * 60.0) if args.warmup_minutes is not None \
        else total_seconds * 0.10
    deadline = time.monotonic() + total_seconds
    payload = BenchPayload(kind=args.kind, size=args.size, pages=args.tasks_per_round,
                           workers=args.workers)
    tmp_root = pathlib.Path(args.data_root or tempfile.mkdtemp(prefix="dae_soak_"))

    inflight = InflightRegistry()
    stack, _ = build_offline_stack(tmp_root, payload=payload, inflight=inflight)
    sampler = ProcessSampler(interval=max(1.0, args.sample_interval / 2.0),
                             disk_path=tmp_root).start()
    # 先采一次：让 `proc.*` / `disk.*` 这批序列**在第一轮之前**就位。
    # 否则它们的出现时刻取决于采样线程的调度，会被"序列数稳态"判据误判成持续增长。
    sampler.sample_once()

    samples: list[dict] = []
    t_start = time.monotonic()
    rounds = 0
    tasks_run = 0
    interrupted = False

    def take_sample(tag: str) -> dict:
        from daedalus.obs.samplers import read_process_stats
        st = read_process_stats()
        s = {"ts": round(time.monotonic() - t_start, 2), "tag": tag,
             "rss_mb": (st.get("rss_bytes") or 0) / (1 << 20),
             "handles": st.get("handles"), "threads": st.get("threads"),
             "own_threads": sum(1 for th in __import__("threading").enumerate()
                                if th.name.startswith("dae-")),
             "series_total": METRICS.counter("metrics.series_total"),
             "series_overflow": METRICS.counter("metrics.series_overflow"),
             "inflight": len(inflight),
             "rounds": rounds, "tasks": tasks_run}
        samples.append(s)
        with samples_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(s, ensure_ascii=False, sort_keys=True) + "\n")
        return s

    print(f"长跑开始：{args.minutes:.2f} 分钟，每轮 {args.tasks_per_round} 任务 × {args.workers} 线程，"
          f"样本写 {samples_path}")
    s0 = take_sample("start")
    print(f"  起始 rss={s0['rss_mb']:.1f} MB 句柄={s0['handles']} 线程={s0['threads']} "
          f"(自有 {s0['own_threads']})")
    last_print = time.monotonic()
    try:
        while time.monotonic() < deadline:
            # **每轮换新 URL**：同 URL 的幂等键相同，第二轮起会被前沿去重 → 空轮（什么也没测）
            result = run_batch(stack, args.tasks_per_round, workers=args.workers,
                               start_index=rounds * args.tasks_per_round)
            rounds += 1
            tasks_run += result["tasks"]
            take_sample("round")
            if time.monotonic() - last_print >= max(20.0, args.sample_interval * 2):
                last_print = time.monotonic()
                s = samples[-1]
                print(f"  [{s['ts']:7.0f}s] 轮 {rounds}｜任务 {tasks_run}｜"
                      f"rss {s['rss_mb']:.1f} MB｜句柄 {s['handles']}｜线程 {s['threads']}｜"
                      f"序列 {s['series_total']:.0f}")
    except KeyboardInterrupt:
        interrupted = True
        print("\n收到中断：停止投放新任务，先把已采样本与结论落盘（不当作「跑完没问题」）")

    # ── 关闭链（**必须真的把线程收干净**，否则长跑结论无效）────────
    # 稳态判据要在**关闭之前**取样：关闭链自己会新增几条序列（`lifecycle.shutdown` 等），
    # 那是收尾动作，不是"跑着跑着一直在涨"。
    series_pre_shutdown = int(METRICS.counter("metrics.series_total"))
    shutdown = ShutdownChain(inflight=inflight).run(
        writer=stack["writer"], db=stack["db"], sampler=sampler)
    time.sleep(0.2)
    end = take_sample("end")

    # ── 结论文 ──────────────────────────────────────────────────
    t_post_warm = [s for s in samples if s["ts"] >= warmup]
    rss_pts = [(s["ts"], s["rss_mb"]) for s in t_post_warm if s["rss_mb"]]
    hnd_pts = [(s["ts"], float(s["handles"])) for s in t_post_warm if s.get("handles")]
    rss_slope = _slope_per_min(rss_pts)
    hnd_slope = _slope_per_min(hnd_pts)
    rss_growth = (rss_pts[-1][1] - rss_pts[0][1]) if len(rss_pts) >= 2 else 0.0
    # 斜率只有在**跨度够长**时才有意义：9 秒的窗口里一次分配器扩容就能算出"20MB/分钟"，
    # 而总增量其实只有 0.6MB。所以短跑以**总增量**判、长跑以**斜率**判（清单 L5 要的是 1h 的斜率）。
    span = (rss_pts[-1][0] - rss_pts[0][0]) if len(rss_pts) >= 2 else 0.0
    slope_reliable = span >= 300.0 and len(rss_pts) >= 6
    rss_basis = ("斜率（跨度 %.0f 秒，可信）" % span) if slope_reliable else \
                ("总增量（跨度仅 %.0f 秒，斜率不可信→只看增量）" % span)
    rss_ok = (rss_growth <= args.rss_growth_budget_mb
              and ((rss_slope is None or rss_slope <= args.rss_slope_mb_per_min)
                   if slope_reliable else True))

    counts = stack["ledger"].summary().get("counts", {})
    ledger_new = int(counts.get("new", 0) or 0)
    lost = tasks_run - ledger_new
    hist = METRICS.histogram("task.duration")
    series_end = int(METRICS.counter("metrics.series_total"))
    queues_after = METRICS.gauges("queue.depth")

    checks = [
        {"name": "rss_slope", "ok": rss_ok,
         "value": None if rss_slope is None else round(rss_slope, 4),
         "limit": args.rss_slope_mb_per_min, "unit": "MB/分钟",
         "note": rss_basis},
        {"name": "rss_growth", "ok": rss_growth <= args.rss_growth_budget_mb,
         "value": round(rss_growth, 2), "limit": args.rss_growth_budget_mb, "unit": "MB"},
        {"name": "handle_slope", "ok": (hnd_slope is None or hnd_slope < 1.0),
         "value": None if hnd_slope is None else round(hnd_slope, 4), "limit": 1.0,
         "unit": "个/分钟"},
        {"name": "no_lost_rows", "ok": lost == 0, "value": lost, "limit": 0, "unit": "行"},
        {"name": "own_threads_zero", "ok": end["own_threads"] == 0,
         "value": end["own_threads"], "limit": 0, "unit": "个"},
        {"name": "metrics_bounded",
         # 判据是**稳态**而不是"从不变化"：第一轮会新建一批序列（正常，标签是运行时才知道的），
         # 之后必须不再增长；直方图样本数也永远不该超过容量（那是"观测把自己变成 OOM"的形状）。
         "ok": (hist.get("samples", 0) <= METRICS.hist_capacity
                and (len(samples) < 2
                     or int(samples[1]["series_total"]) == series_pre_shutdown)),
         "value": {"samples": hist.get("samples"),
                   "series_first": int(samples[0]["series_total"]),
                   "series_round1": int(samples[1]["series_total"]) if len(samples) > 1 else None,
                   "series_pre_shutdown": series_pre_shutdown,
                   "series_end": series_end},
         "limit": f"样本≤{METRICS.hist_capacity} 且序列数稳态（关闭前）", "unit": ""},
        {"name": "queues_drained", "ok": all(v == 0 for v in queues_after.values()),
         "value": queues_after, "limit": "全 0", "unit": "深度"},
        {"name": "shutdown_ok", "ok": bool(shutdown.ok), "value": shutdown.ok,
         "limit": True, "unit": ""},
    ]
    report = {"ts": time.time(), "minutes": round(total_seconds / 60.0, 3),
              "rounds": rounds, "tasks_run": tasks_run, "interrupted": interrupted,
              "warmup_seconds": round(warmup, 1),
              "rss_mb_start": round(s0["rss_mb"], 2), "rss_mb_end": round(end["rss_mb"], 2),
              "rss_slope_mb_per_min": None if rss_slope is None else round(rss_slope, 4),
              "handle_slope_per_min": None if hnd_slope is None else round(hnd_slope, 4),
              "ledger_counts": counts, "checks": checks,
              "all_ok": all(c["ok"] for c in checks),
              "shutdown": shutdown.to_dict(),
              "samples_path": str(samples_path), "samples": len(samples)}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n长跑结论：")
    for c in checks:
        mark = "OK  " if c["ok"] else "FAIL"
        extra = f"｜{c['note']}" if c.get("note") else ""
        print(f"  [{mark}] {c['name']:18} {c['value']} / 上限 {c['limit']} {c['unit']}{extra}")
    print(f"  任务 {tasks_run} 条｜台账 new {ledger_new}｜跑丢 {lost} 条"
          + ("（中断，样本仅代表已跑部分）" if interrupted else ""))
    print(f"  报告：{report_path}")
    if not args.data_root:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

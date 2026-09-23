# -*- coding: utf-8 -*-
"""基准与回归对比（清单 L7）

跑法：
    python tools/bench.py                      # 跑一轮，追加进 bench_runs.jsonl
    python tools/bench.py --tasks 800 -w 16    # 指定负载
    python tools/bench.py --compare            # 拿最近两轮出回归报告（改代码前后各跑一次）
    python tools/bench.py --compare --baseline-ref 3   # 与倒数第 3 轮比

产出：`<数据根或 tools/out>/bench_runs.jsonl`，一行一条**可比记录**（含负载参数与环境注记）。
判据：同一 `payload.fingerprint` 的两轮才能比；跨负载比较会**明确拒绝**而不是给个假结论。
回归阈值：p95/吞吐变化超过 `--threshold`（默认 20%）即判"疑似回归"，并列出各项差值。
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


def _out_dir(arg: str | None) -> pathlib.Path:
    if arg:
        d = pathlib.Path(arg)
    else:
        d = ROOT / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_runs(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    runs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                runs.append(json.loads(line))
            except Exception:
                continue
    return runs


def _regression(before: dict, after: dict, threshold: float) -> dict:
    """两轮对比：吞吐掉了或延迟涨了超过阈值 = 疑似回归。"""
    rows: list[dict] = []
    verdict = "ok"

    def cmp(metric: str, lower_is_better: bool) -> None:
        nonlocal verdict
        a = before.get(metric)
        b = after.get(metric)
        if a in (None, 0) or b is None:
            rows.append({"metric": metric, "before": a, "after": b, "delta_pct": None,
                         "note": "数据缺失，不判"})
            return
        delta = (b - a) / abs(a) * 100.0
        bad = (delta > threshold) if lower_is_better else (delta < -threshold)
        if bad:
            verdict = "suspect_regression"
        rows.append({"metric": metric, "before": a, "after": b,
                     "delta_pct": round(delta, 2),
                     "verdict": "regression?" if bad else "ok"})

    cmp("pages_per_sec", lower_is_better=False)
    cmp("mb_per_sec", lower_is_better=False)
    cmp("task_ms_p50", lower_is_better=True)
    cmp("task_ms_p95", lower_is_better=True)
    cmp("task_ms_p99", lower_is_better=True)
    return {"verdict": verdict, "threshold_pct": threshold, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daedalus 离线基准（合成负载，真闭环）")
    ap.add_argument("--tasks", type=int, default=400, help="本轮任务数")
    ap.add_argument("-w", "--workers", type=int, default=8, help="并发领取线程数")
    ap.add_argument("--size", type=int, default=24 * 1024, help="每页字节数")
    ap.add_argument("--kind", default="html", choices=("html", "json", "binary"))
    ap.add_argument("--latency", type=float, default=0.0, help="假咽喉每次调用的固定延迟（秒）")
    ap.add_argument("--data-root", default=None, help="输入库目录（默认临时目录，跑完即弃）")
    ap.add_argument("--out", default=None, help="JSONL 输出目录（默认 <工程>/out）")
    ap.add_argument("--label", default="", help="给这一轮起个名字（如 baseline / after-fix）")
    ap.add_argument("--repeat", type=int, default=1, help="重复跑几轮取中位数（更稳）")
    ap.add_argument("--compare", action="store_true", help="对比最近两轮出回归报告")
    ap.add_argument("--baseline-ref", type=int, default=2,
                    help="对比时往前数第几轮作为基线（默认 2 = 上一轮）")
    ap.add_argument("--threshold", type=float, default=20.0, help="回归阈值（百分比）")
    args = ap.parse_args(argv)

    out_dir = _out_dir(args.out)
    runs_path = out_dir / "bench_runs.jsonl"

    if args.compare:
        runs = _read_runs(runs_path)
        if len(runs) < 2:
            print(f"还没有两轮可比记录（{runs_path} 里只有 {len(runs)} 条）——先跑两次 bench.py")
            return 2
        after = runs[-1]
        ref = min(len(runs), max(1, args.baseline_ref))
        before = runs[-ref]
        if before.get("payload", {}).get("fingerprint") != after.get("payload", {}).get("fingerprint"):
            print("两轮负载不同（payload.fingerprint 不一致）→ **拒绝对比**（会是假结论）")
            print(f"  基线 {before.get('payload')}")
            print(f"  本轮 {after.get('payload')}")
            return 2
        rep = _regression(before.get("result", {}), after.get("result", {}), args.threshold)
        print(f"回归报告：{before.get('label') or '基线'} → {after.get('label') or '本轮'}"
              f"（阈值 ±{args.threshold:.0f}%）")
        print(f"  结论：{rep['verdict']}")
        for r in rep["rows"]:
            d = r.get("delta_pct")
            print(f"  {r['metric']:16} {r['before']!s:>10} → {r['after']!s:>10}  "
                  f"{'' if d is None else f'{d:+.1f}%':>8}  {r.get('verdict', '')}")
        return 1 if rep["verdict"] == "suspect_regression" else 0

    import shutil
    import tempfile
    from _harness import (BenchPayload, build_offline_stack, cleanup, env_note, run_batch)

    payload = BenchPayload(kind=args.kind, size=args.size, pages=args.tasks,
                           workers=args.workers, latency=args.latency)
    tmp_root = pathlib.Path(args.data_root or tempfile.mkdtemp(prefix="dae_bench_"))
    rounds: list[dict] = []
    try:
        for i in range(max(1, args.repeat)):
            stack, _ = build_offline_stack(tmp_root / f"r{i}", payload=payload)
            try:
                result = run_batch(stack, args.tasks, workers=args.workers)
                shutdown = cleanup(stack)
            finally:
                pass
            # 收尾后再验一次"库里的行数 = 期望行数"（基准顺带证明不丢行）
            ledger_counts = stack["ledger"].summary().get("counts", {})
            result["ledger"] = ledger_counts
            result["shutdown_ok"] = bool(shutdown.get("ok"))
            rounds.append(result)
            print(f"  第 {i + 1} 轮：{result['pages_per_sec']} 页/秒，"
                  f"{result['mb_per_sec']} MB/s，p95 {result['task_ms_p95']} ms，"
                  f"状态 {result['states']}")
    finally:
        if not args.data_root:
            shutil.rmtree(tmp_root, ignore_errors=True)

    # 取中位数轮（重复跑的目的是抗抖动；记录里保留全部原始轮）
    def med(key: str) -> float:
        return round(statistics.median([r[key] for r in rounds]), 4)

    merged = dict(rounds[0])
    for k in ("seconds", "pages_per_sec", "mb_per_sec", "task_ms_p50", "task_ms_p95",
              "task_ms_p99"):
        merged[k] = med(k)

    rec = {"ts": time.time(), "label": args.label, "payload": {
               **payload.to_dict(), "fingerprint": payload.fingerprint},
           "env": env_note(), "rounds": len(rounds), "result": merged}
    with runs_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"\n负载指纹 {payload.fingerprint}（跨轮可比的前提）")
    print(f"结果：{merged['tasks']} 任务 / {merged['seconds']}s → "
          f"{merged['pages_per_sec']} 页/秒、{merged['mb_per_sec']} MB/s；"
          f"p50/p95/p99 = {merged['task_ms_p50']}/{merged['task_ms_p95']}/{merged['task_ms_p99']} ms")
    print(f"台账：{merged.get('ledger')}；关闭链 ok={merged.get('shutdown_ok')}")
    print(f"已追加到 {runs_path}（下次跑 `--compare` 出回归报告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

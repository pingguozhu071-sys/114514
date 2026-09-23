# -*- coding: utf-8 -*-
"""停顿探针（设计系统的性能红线，实测而不是"应该很快"）

    python tools/perf_probe.py                 # 默认在 offscreen 平台跑（无显示器也能验）
    python tools/perf_probe.py --real          # 用真实平台跑（有桌面时更接近体感）

它测四件事，对应设计系统里写死的四条红线：
    ① **停顿** < 200ms：心跳定时器（20ms）在交互过程中记录**最大间隔**——
       界面卡住时心跳就会迟到，迟到多久 = 停了多久（Kiana 的停顿探针就是这个思路）；
    ② **切页** < 400ms（每页各测一次）；
    ③ **最大化重排** < 1200ms（改窗口尺寸 + 重刷，含底图裁切复用）；
    ④ **图像管线单次** < 200MB（`tracemalloc` 峰值）。

产出 JSON（可进基准对比）与结论；退出码 0 = 全过。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import tracemalloc

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 红线（与设计系统一致）
STALL_MS, SWITCH_MS, REFLOW_MS, PIPE_MB = 200.0, 400.0, 1200.0, 200.0


def _make_test_image(path: pathlib.Path, w: int, h: int, *, colorful: bool = True) -> None:
    import cv2
    import numpy as np
    rng = np.random.default_rng(11)
    if colorful:
        img = np.zeros((h, w, 3), "uint8")
        img[:, :, 0] = 200                       # BGR
        img[:, :, 2] = 120
        img[:, :w // 2, 1] = 90
        img = cv2.addWeighted(img, 0.8,
                              rng.integers(0, 255, (h, w, 3)).astype("uint8"), 0.2, 0)
    else:
        img = np.full((h, w, 3), 128, "uint8")
    path.write_bytes(cv2.imencode(".png", img)[1].tobytes())


def probe(*, real: bool = False, out: pathlib.Path | None = None) -> dict:
    import os
    if not real:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tempfile

    from PySide6.QtCore import QTimer
    from daedalus.ui.app import MainWindow, build_headless
    from daedalus.ui.wallpaper import WallpaperCache, extract_accent, process_wallpaper

    root = pathlib.Path(tempfile.mkdtemp(prefix="dae_perf_"))
    b = build_headless(data_root=root)
    win, app = b["window"], b["app"]
    win.show()
    app.processEvents()

    # ① 心跳：记录最大间隔（= 最大停顿）
    beats: list[float] = []
    last = [time.perf_counter()]

    def beat():
        now = time.perf_counter()
        beats.append((now - last[0]) * 1000.0)
        last[0] = now

    hb = QTimer()
    hb.setInterval(20)
    hb.timeout.connect(beat)
    hb.start()
    last[0] = time.perf_counter()

    # ② 切页
    switches: dict[str, float] = {}
    for key in ("tasks", "logs", "settings", "about", "overview"):
        switches[key] = MainWindow.goto(win, key)
        app.processEvents()

    # ③ 底图（含中文路径）+ 重排
    from daedalus.ui.wallpaper import to_qimage
    d = root / "底图目录"
    d.mkdir(parents=True, exist_ok=True)
    big = d / "8K.png"
    _make_test_image(big, 7680, 4320)
    pipe: dict = {}
    tracemalloc.start()
    t0 = time.perf_counter()
    q, meta = process_wallpaper(str(big), width=win.width(), height=win.height(), blur=6)
    pipe["ms"] = round((time.perf_counter() - t0) * 1000, 2)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    pipe["peak_mb"] = round(peak / (1 << 20), 2)
    pipe["meta"] = meta
    MainWindow.set_wallpaper(win, q, meta)
    app.processEvents()

    reflows: list[float] = []
    for w, h in ((1600, 900), (980, 620), (1360, 860)):
        t0 = time.perf_counter()
        win.resize(w, h)
        app.processEvents()
        reflows.append(round((time.perf_counter() - t0) * 1000, 2))

    # ④ 缓存复用 + 代数号守卫（连换图：只跑一次管线）
    cache = WallpaperCache()
    t0 = time.perf_counter()
    for _i in range(6):
        cache.get(str(big), width=1360, height=860, blur=6)
    cache_ms = round((time.perf_counter() - t0) * 1000, 2)
    sched = win._ui["sched"]                     # noqa: SLF001
    for i in range(10):
        sched.request({"i": i}, reason="probe")
    time.sleep(0.30)
    due_ok = sched.due()
    req = sched.take()
    stale_dropped = sched.complete(0, {"old": True}) is None if req else None
    sched.finish_busy()
    accent = extract_accent(str(big))

    hb.stop()
    app.processEvents()
    stall_max = round(max(beats[1:] or [0.0]), 2)

    checks = [
        {"name": "stall_max_ms", "value": stall_max, "limit": STALL_MS, "ok": stall_max < STALL_MS},
        {"name": "switch_max_ms", "value": max(switches.values()), "limit": SWITCH_MS,
         "ok": max(switches.values()) < SWITCH_MS},
        {"name": "reflow_max_ms", "value": max(reflows), "limit": REFLOW_MS,
         "ok": max(reflows) < REFLOW_MS},
        {"name": "pipeline_peak_mb", "value": pipe["peak_mb"], "limit": PIPE_MB,
         "ok": pipe["peak_mb"] < PIPE_MB},
        {"name": "wallpaper_cached_hit", "value": cache.stats()["hits"], "limit": "≥4",
         "ok": cache.stats()["hits"] >= 4},
        {"name": "debounce_coalesced", "value": sched.stats()["coalesced"], "limit": "≥5",
         "ok": sched.stats()["coalesced"] >= 5},
    ]
    report = {"platform": "real" if real else "offscreen", "hearts": len(beats),
              "beats_ms_max": stall_max, "switches_ms": switches, "reflows_ms": reflows,
              "pipeline": pipe, "cache_ms_all": cache_ms, "cache": cache.stats(),
              "scheduler": sched.stats(), "accent": accent, "checks": checks,
              "all_ok": all(c["ok"] for c in checks),
              "note": "offscreen 平台的绝对耗时偏乐观（无真实合成器）；关注**相对变化**与缓存命中"}
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daedalus 界面停顿探针（性能红线实测）")
    ap.add_argument("--real", action="store_true", help="用真实平台（默认 offscreen）")
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    args = ap.parse_args(argv)
    rep = probe(real=args.real, out=pathlib.Path(args.out) if args.out else None)
    for c in rep["checks"]:
        mark = "OK  " if c["ok"] else "FAIL"
        print(f"  [{mark}] {c['name']:22} {c['value']} / 上限 {c['limit']}")
    print("  切页(ms):", rep["switches_ms"])
    print("  重排(ms):", rep["reflows_ms"])
    print("  底图管线:", {k: rep["pipeline"][k] for k in ("ms", "peak_mb")})
    print(f"平台 {rep['platform']}｜结论 {'全过' if rep['all_ok'] else '有超线'}")
    return 0 if rep["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

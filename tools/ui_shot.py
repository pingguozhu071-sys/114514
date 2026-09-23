# -*- coding: utf-8 -*-
"""界面截图：把每一页在若干外观配置下**真渲染成 PNG**，供人眼验收

    python tools/ui_shot.py                     # 默认：深色/浅色 + 底图有/无
    python tools/ui_shot.py --out out/ui_shots
    python tools/ui_shot.py --wallpaper 图.png   # 指定底图（默认自动造一张测试图）
    python tools/ui_shot.py --pages overview,tasks

为什么要有这个工具：**offscreen 平台的断言能证明「没崩、参数生效」，但证明不了「好不好看/挤不挤」**。
界面验收必须看图。所以把每页渲成 PNG，由人（或子代理）真的看一眼，再决定改哪儿。
（`QT_QPA_PLATFORM=offscreen`：无显示器也能渲。）
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

__all__ = ["shoot"]


def _make_test_wallpaper(path: pathlib.Path, w: int = 1920, h: int = 1080) -> pathlib.Path:
    """造一张「真实感」测试底图：渐变 + 亮/暗块 + 高饱和色块（考文字可读性与取色）。"""
    import cv2
    import numpy as np
    rng = np.random.default_rng(7)
    img = np.zeros((h, w, 3), "uint8")
    for y in range(h):                                   # 竖向渐变（模拟天空到地面）
        img[y, :, :] = (30 + 90 * y / h, 20 + 60 * y / h, 15 + 40 * y / h)
    cv2.rectangle(img, (0, 0), (w // 3, h // 4), (235, 235, 235), -1)      # 亮块（白）
    cv2.rectangle(img, (w // 2, h // 2), (w, h), (18, 14, 12), -1)         # 暗块
    cv2.circle(img, (int(w * 0.72), int(h * 0.28)), 240, (180, 60, 200), -1)  # 高饱和紫
    img = cv2.addWeighted(img, 0.85, rng.integers(0, 255, (h, w, 3)).astype("uint8"), 0.15, 0)
    path.write_bytes(cv2.imencode(".png", img)[1].tobytes())
    return path


def shoot(*, out_dir: pathlib.Path, wallpaper: pathlib.Path | None = None,
          pages: tuple[str, ...] = ("overview", "tasks", "logs", "settings", "about"),
          variants: tuple[str, ...] = ("dark", "light")) -> list[pathlib.Path]:
    from daedalus.ui.app import MainWindow, build_headless
    from daedalus.ui.theme import tokens as make_tokens
    from daedalus.ui.wallpaper import process_wallpaper

    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[pathlib.Path] = []
    root = out_dir / "_data"
    root.mkdir(parents=True, exist_ok=True)
    b = build_headless(data_root=root)
    app, win = b["app"], b["window"]
    win.resize(1360, 860)
    win.show()
    app.processEvents()

    wp = wallpaper or _make_test_wallpaper(out_dir / "_test_wallpaper.png")
    for variant in variants:
        for light in (False, True) if variant == "dark-light" else (variant == "light",):
            t = make_tokens(light=light, panel_alpha=65, blur=0, dim_manual=0)
            if wallpaper is not None:
                q, meta = process_wallpaper(str(wp), width=1360, height=860, blur=0,
                                            dim_manual=0)
                MainWindow.set_wallpaper(win, q, meta)
            else:
                q, meta = process_wallpaper(str(wp), width=1360, height=860, blur=4,
                                            dim_manual=0)
                MainWindow.set_wallpaper(win, q, meta)
            MainWindow.apply_tokens(win, t)
            app.processEvents()
            for key in pages:
                MainWindow.goto(win, key)
                for _ in range(3):
                    app.processEvents()
                p = out_dir / f"{'light' if light else 'dark'}_{key}.png"
                pm = win.grab()
                pm.save(str(p), "PNG")
                made.append(p)
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="界面截图（offscreen 真渲染）")
    ap.add_argument("--out", default=str(ROOT / "out" / "ui_shots"))
    ap.add_argument("--wallpaper", default=None, help="底图路径（不给就自动造测试图）")
    ap.add_argument("--pages", default="overview,tasks,logs,settings,about")
    args = ap.parse_args(argv)
    made = shoot(out_dir=pathlib.Path(args.out),
                 wallpaper=pathlib.Path(args.wallpaper) if args.wallpaper else None,
                 pages=tuple(p.strip() for p in args.pages.split(",") if p.strip()))
    print(f"已渲染 {len(made)} 张：")
    for p in made:
        print(f"  {p}  ({p.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

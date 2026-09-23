# -*- coding: utf-8 -*-
"""打包态 GUI 真窗口截图：验"装完之后界面到底是什么语言、长什么样"。

    python tools/grab_frozen.py --lang 2052 --out out/ui_shots/frozen_zh.png
    python tools/grab_frozen.py --lang 1041 --out out/ui_shots/frozen_ja.png

**为什么要这个工具**：源码态的截图（`tools/ui_shot.py`）证明不了"装到机器上的那个 exe 是不是
本机语言"——而机主的原话就是"安装时选了简体，装完别给我蹦日文"。所以这里**真启动 dist 里的
那个 exe**（带 `install.marker` 的 `lang=`，模拟安装器写下的选择），抓它的**真窗口**像素。

启动方式**就是用户双击那种**（`os.startfile`：不经 shell、不拼命令行），进程靠 `psutil`
按"启动时间 + 进程名"认领——因此本工具**不接收任何"要执行哪个程序"的参数**。

流程：写 marker → 启动 exe → 等窗口出现 → 按 PID 找窗口矩形 → 截图 → 关掉。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "dist" / "daedalus" / "daedalus.exe"        # 固定：只跑打包产物
__all__ = ["grab"]


def _print_window(hwnd: int, w: int, h: int):
    """抓**窗口自己的表面**（`PrintWindow` + PW_RENDERFULLCONTENT）。

    比"截屏那块矩形"可靠：截屏会被别的窗口盖住——第一次跑就是这样，抓到的是另一个应用的
    对话框（`SetForegroundWindow` 在 Windows 上经常被系统拒绝，前台窗口根本不是我们启的那个）。
    """
    import cv2
    import numpy as np
    import ctypes.wintypes as _wt
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, 2):            # 2 = PW_RENDERFULLCONTENT
            raise RuntimeError("PrintWindow 失败")
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG), ("biHeight", _wt.LONG),
                        ("biPlanes", _wt.WORD), ("biBitCount", _wt.WORD),
                        ("biCompression", _wt.DWORD), ("biSizeImage", _wt.DWORD),
                        ("biXPelsPerMeter", _wt.LONG), ("biYPelsPerMeter", _wt.LONG),
                        ("biClrUsed", _wt.DWORD), ("biClrImportant", _wt.DWORD)]
        bi = BITMAPINFOHEADER()
        bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(bi), w, -h   # 负高度 = 自上而下
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        arr = np.frombuffer(buf, np.uint8).reshape(h, w, 4)
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)


def _dpi_aware() -> None:
    """PIL 的 bbox 用物理像素；不声明 DPI 感知的话缩放屏上会截偏。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _launched_pid(name: str, since: float) -> int | None:
    """认领"刚刚被本工具启动的那个进程"：进程名匹配且创建时间晚于 since。"""
    import psutil
    for p in psutil.process_iter(["name", "create_time"]):
        try:
            if (p.info["name"] or "").lower() == name.lower() and p.info["create_time"] >= since:
                return p.pid
        except Exception:
            continue
    return None


def _windows_of(pid: int) -> list[tuple[int, str, tuple[int, int, int, int]]]:
    """列出某个进程的可见顶层窗口：[(hwnd, 标题, (l, t, r, b))]。"""
    out: list[tuple[int, str, tuple[int, int, int, int]]] = []
    user32 = ctypes.windll.user32
    cb = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def _visit(hwnd, _l):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            user32.GetWindowTextW(hwnd, buf, n + 2)
            r = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left > 200 and r.bottom - r.top > 200:
                out.append((hwnd, buf.value, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(cb(_visit), 0)
    return out


def grab(lang: int | None, out: pathlib.Path, *, settle: float = 4.0) -> pathlib.Path:
    from PIL import ImageGrab
    if not APP.exists():
        raise SystemExit(f"没找到打包好的界面程序：{APP}（先跑 tools/build.py）")
    marker = APP.parent / "install.marker"
    _dpi_aware()
    old = marker.read_text(encoding="utf-8") if marker.exists() else None
    if lang:
        marker.write_text(f"installed=1\nlang={lang}\nversion=0.1.0.dev0\n", encoding="utf-8")
    pid = None
    t0 = time.time() - 1
    try:
        os.startfile(str(APP))                              # 与用户双击等价；不经 shell
        deadline = time.time() + 45
        win = None
        while time.time() < deadline:
            time.sleep(0.6)
            if pid is None:
                pid = _launched_pid(APP.name, t0)
                if pid is None:
                    continue
            wins = _windows_of(pid)
            if wins:
                win = wins[0]
                break
        if pid is None:
            raise SystemExit("45 秒内没看到打包程序启动")
        if win is None:
            raise SystemExit(f"等了 45 秒也没看到窗口（进程 pid={pid}）")
        time.sleep(settle)                                  # 让底图管线/入场动画跑完
        wins = _windows_of(pid) or [win]
        hwnd, title, _box = max(wins, key=lambda w: (w[2][2] - w[2][0]) * (w[2][3] - w[2][1]))
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.8)
        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        img = _print_window(hwnd, r.right - r.left, r.bottom - r.top)
        out.parent.mkdir(parents=True, exist_ok=True)
        import cv2
        if not cv2.imwrite(str(out), img):
            raise SystemExit(f"截图写不出去：{out}")
        print(f"窗口标题：{title}")
        print(f"窗口尺寸：{img.shape[1]}×{img.shape[0]} → {out}")
    finally:
        if pid is not None:
            try:
                import psutil
                psutil.Process(pid).terminate()
                psutil.Process(pid).wait(timeout=10)
            except Exception:
                pass
        if old is None:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(old, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="打包态 GUI 真窗口截图")
    ap.add_argument("--lang", type=int, help="写入 install.marker 的 LCID（2052 简体 / 1041 日语 / 1033 英语）")
    ap.add_argument("--out", required=True, help="输出 PNG")
    ap.add_argument("--settle", type=float, default=4.0, help="窗口出现后再等几秒（默认 4）")
    a = ap.parse_args()
    grab(a.lang, pathlib.Path(a.out), settle=a.settle)
    return 0


if __name__ == "__main__":
    sys.exit(main())

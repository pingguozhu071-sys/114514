# -*- coding: utf-8 -*-
"""图标颜色契约门禁：**原图 ↔ 抠图 ↔ 各尺寸产物**（`python tools/icon_check.py`，退出码为准）

这个检查器为什么存在（2026-09-23 的事故，别删这段）
    用户投诉「图标颜色不对」。根因在 `tools/make_icon.py` 的两个 PNG 写出点：
    `cv2.imencode` 外面又套了一层 `cvtColor(BGRA2RGBA)`——imencode 本来就期望 BGRA，
    于是**连换两次**，落盘的每张 PNG/ICO 都成了原画的 **R↔B 镜像**。实测：
    抠图里一块全不透明的主体方块与源图同位置的 mean RGB 差 (R,B) 各 22.3、G 差 0，
    产物整体从冷色（B>R）翻成暖色（R>B），安装器美术里的 Logo 芯片也被一起传染。
    修代码只修了「这一处」；**这个门禁盯的是「这一类」**：
    任何一次颜色被翻、被串、被别的脚本按错误顺序重编码，都必须在这里红。

三道检查（退出码：0 通过 / 1 颜色契约违规 / 2 派生物过期）
    A. **源锚定**（原图在场时才算）：从 `assets/icon_source_cutout.png` 取一块
       **全不透明**（alpha=255）的主体内部方块，在**原图**里用灰度模板匹配定位同一块，
       逐通道比 mean——派生链的第一步必须是原画的颜色。灰度匹配与通道顺序无关，
       所以它不会「顺着」被互换的图找到错误位置（这正是我们要它能抓住的）。
    B. **各产物色向**：各 PNG 与 ICO 内嵌 PNG 的**角色像素** mean RGB 必须满足 `R < B`
       （这张立绘是冷色角色；R/B 一翻，差值约 ±14…±25，非常显眼）。
       深色底版本（icon_dark_*）只取**亮部角色像素**：它的底是 #0E1620（很暗、且是实心的），
       按 alpha 取「不透明像素」会把底色算进来，方向立刻被底色带跑（底色本身是蓝黑）。
    C. **派生物同步**：`packaging/art/*.bmp` 的 Logo 区必须与 `assets/icon_256.png` **同色**
       （像素级 mean|Δ|）。bmp 是另一个脚本从图标生成的——不同步就说明它还是旧图
       （那时退出码 2，处置是重跑 `python packaging/make_installer_art.py`，不是改图标）。

用法
    python tools/icon_check.py                 # 人可读；默认查本仓库
    python tools/icon_check.py --json          # 机器可读（CI 用）
    python tools/icon_check.py --no-derived    # 只查图标本体（跳过 packaging/art）
    python tools/icon_check.py --root DIR      # 换根目录查（故障注入自测 / 别的检出）
    python tools/icon_check.py --src FILE      # 换原图路径（原图不在场就自动跳过 A）

口径说明：报告里的数字**一律按 RGB 念**（cv2 内存里是 BGR，本文件在每个出口都换了序）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SRC = r"C:\HONOR Share\Honor Share\19th_149128711.jpg"

TOL_BLOCK = 2.0        # A：全不透明块的逐通道 mean 容差（PNG 无损 → 理论上 0.0；留给重采样/重编码）
TOL_DIRECTION = 5.0    # B/C：B−R 至少要这么大才算「冷色侧」（实测 |B−R| ≈ 14…25，留足余量）
TOL_DERIVED = 3.0      # C：Logo 区与图标的逐通道 mean|Δ|（实测 0.8…1.2，缩放的边缘混色所致）
BLOCK = 128            # A 的方块边长
CHIP_SEARCH = 10       # C：芯片位置的容错半径（允许美术脚本挪几像素而不误报）

# 各产物的期望色向（None = 独立 PNG 文件；数字 = ICO 内嵌的那一档尺寸）
# 第三列是取样口径：opaque=全部不透明像素；bright=亮部角色像素（深色底版专用，见抬头）
_DIRECTION_CASES: tuple[tuple[str, int | None, str, str], ...] = (
    ("assets/icon_source_cutout.png", None, "opaque", "全分辨率抠图（所有尺寸的源头）"),
    ("assets/icon_256.png", None, "opaque", "256 预览（全身）"),
    ("assets/icon_bust_256.png", None, "opaque", "256 预览（胸像）"),
    ("assets/icon_dark_256.png", None, "bright", "256 预览（深色底：只看亮部角色像素）"),
    ("assets/icon.ico", 256, "opaque", "主图标 256 档（全身）"),
    ("assets/icon.ico", 32, "opaque", "主图标 32 档（胸像，小尺寸专用）"),
    ("assets/icon_full.ico", 256, "opaque", "备选：全身 256 档"),
    ("assets/icon_full.ico", 32, "opaque", "备选：全身 32 档"),
    ("assets/icon_bust.ico", 32, "opaque", "备选：胸像 32 档"),
    ("assets/icon_dark.ico", 256, "bright", "备选：深色底 256 档（只看亮部角色像素）"),
)

# C 的芯片几何：来源 `packaging/make_installer_art.py` 的 make_welcome / make_header 里
# `_logo_chip(x=..., y=..., size=...)`。**改那边的芯片尺寸/位置，这里必须同步改**，否则会误报过期。
_CHIP: dict[str, tuple[int, int, int]] = {
    "welcome.bmp": (54, 42, 56),      # x=(164-56)//2, y=42, size=56
    "header.bmp": (10, 8, 40),        # x=10, y=8, size=40
}
_CHIP_ICON = "assets/icon_256.png"    # 美术脚本贴的就是它（make_installer_art.py:197）


def _load(path: pathlib.Path) -> np.ndarray:
    """中文/空格路径安全读图（cv2.imread 在 Windows 上对 Unicode 路径会静默返回 None）。"""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    im = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if im is None:
        raise SystemExit(f"[icon_check] 解不开这张图：{path}")
    return im


def read_ico(path: pathlib.Path) -> dict[int, np.ndarray]:
    """解析 ICO 容器 → {尺寸: BGRA 数组}（内嵌 PNG 格式，与 tools/make_icon.py 的写法对应）。"""
    raw = path.read_bytes()
    if len(raw) < 6:
        raise SystemExit(f"[icon_check] 文件太短，不是 ICO：{path}")
    _rsv, _kind, count = struct.unpack("<HHH", raw[:6])
    out: dict[int, np.ndarray] = {}
    for i in range(count):
        entry = raw[6 + 16 * i: 22 + 16 * i]
        if len(entry) != 16:
            raise SystemExit(f"[icon_check] ICO 目录项 {i} 不完整：{path}")
        w_b, _h_b, _colors, _rsv, _planes, _bps, nbytes, off = struct.unpack("<BBBBHHII", entry)
        size = 256 if w_b == 0 else int(w_b)
        blob = raw[off:off + nbytes]
        arr = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_UNCHANGED)
        if arr is None or arr.ndim != 3 or arr.shape[2] != 4:
            raise SystemExit(f"[icon_check] ICO 第 {i} 项解不出 BGRA（size={size}）：{path}")
        out[size] = arr
    return out


def _mean_rgb(px: np.ndarray) -> tuple[float, float, float]:
    """BGR 像素 → mean RGB（统一口径：报告里的数字都按 RGB 念）。"""
    b, g, r = (float(v) for v in px.reshape(-1, 3).mean(axis=0))
    return r, g, b


def _fmt(t: tuple[float, float, float]) -> str:
    return f"({t[0]:7.2f}, {t[1]:7.2f}, {t[2]:7.2f})"


def _mask_opaque(im: np.ndarray) -> np.ndarray:
    """角色像素：不透明（alpha>250）的部分。"""
    return im[..., 3] > 250


def _mask_bright(im: np.ndarray) -> np.ndarray:
    """深色底版专用：只看**亮部**角色像素。

    底是 #0E1620（很暗）且实心——按 alpha 取像素会把底色一起算进来，方向直接被底色带跑
    （底色本身是蓝黑，于是「被翻过的暖色角色 + 蓝黑底」也能凑出一个像样的均值）。
    脸/发/白衣在 luma>150 上，底色在 luma≈24，两者分得很开。
    """
    lum = cv2.cvtColor(im[..., :3], cv2.COLOR_BGR2GRAY)
    return (lum > 150) & (im[..., 3] > 200)


_MASKS = {"opaque": _mask_opaque, "bright": _mask_bright}


def _interior_block(cut: np.ndarray, block: int) -> tuple[np.ndarray, int, int] | None:
    """从抠图里取一块**全不透明**的主体内部方块：腐蚀找「最深处」，不猜坐标。

    为什么要腐蚀：直接按比例切一块可能切到边缘半透明区——那里的颜色已经被
    边缘去色污染改过（`decontaminate`），拿它当基准会把误报当事故。
    全 alpha=255 的块则是**原画像素本身**（去色污染对 a=1 是恒等变换）。
    """
    if cut.ndim != 3 or cut.shape[2] != 4:
        return None
    h, w = cut.shape[:2]
    if h <= block or w <= block:
        return None
    ker = cv2.getStructuringElement(cv2.MORPH_RECT, (block + 1, block + 1))
    er = cv2.erode((cut[..., 3] > 250).astype(np.uint8), ker)
    n, _lab, st, cent = cv2.connectedComponentsWithStats(er, 8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(st[1:, 4]))
    cx, cy = int(round(float(cent[i][0]))), int(round(float(cent[i][1])))
    y0, x0 = cy - block // 2, cx - block // 2
    return cut[y0:y0 + block, x0:x0 + block], cx, cy


def check_source_anchor(root: pathlib.Path, src_path: pathlib.Path) -> dict:
    """A：抠图 vs 原画，同一块的逐通道 mean 比对（R/B 一翻，这里立刻现形）。"""
    rep: dict = {"id": "A", "name": "源锚定块（原画 ↔ 抠图 同一块）", "status": "ok", "numbers": {}}
    try:
        cut = _load(root / "assets" / "icon_source_cutout.png")
    except FileNotFoundError:
        return {**rep, "status": "fail", "detail": "缺 assets/icon_source_cutout.png"}
    got = _interior_block(cut, BLOCK)
    if got is None:
        return {**rep, "status": "fail",
                "detail": f"抠图里找不到 {BLOCK}×{BLOCK} 的全不透明块（掩膜坏了？）"}
    block, cx, cy = got
    rep["numbers"].update({"block": BLOCK, "block_center": [cx, cy],
                           "cutout_mean_rgb": list(_mean_rgb(block[..., :3]))})
    if not src_path.exists():
        return {**rep, "status": "skip",
                "detail": f"原图不在场（{src_path}）→ 跳过源锚定，仅剩 B/C 两道"}
    src = _load(src_path)
    if min(src.shape[:2]) <= BLOCK:
        return {**rep, "status": "fail", "detail": f"原图太小（{src.shape[1]}×{src.shape[0]}）"}
    # 灰度模板匹配定位：**与通道顺序无关**，所以它不会把被互换的图「顺着」匹配到错误位置
    res = cv2.matchTemplate(cv2.cvtColor(src, cv2.COLOR_BGR2GRAY),
                            cv2.cvtColor(block[..., :3], cv2.COLOR_BGR2GRAY), cv2.TM_SQDIFF)
    _mn, _mx, loc, _ml = cv2.minMaxLoc(res)
    bx, by = int(loc[0]), int(loc[1])
    src_block = src[by:by + BLOCK, bx:bx + BLOCK]
    src_rgb, cut_rgb = _mean_rgb(src_block), rep["numbers"]["cutout_mean_rgb"]
    delta = [abs(cut_rgb[i] - src_rgb[i]) for i in range(3)]
    # 诊断：R 与 B 正好对调（G 不动）——这是「多换了一次」的指纹
    swapped = (abs(cut_rgb[0] - src_rgb[2]) <= TOL_BLOCK
               and abs(cut_rgb[2] - src_rgb[0]) <= TOL_BLOCK
               and abs(cut_rgb[1] - src_rgb[1]) <= TOL_BLOCK)
    rep["numbers"].update({"src_loc": [bx, by], "src_mean_rgb": list(src_rgb),
                           "delta": delta, "tolerance": TOL_BLOCK,
                           "src_B_minus_R": src_rgb[2] - src_rgb[0],
                           "cutout_B_minus_R": cut_rgb[2] - cut_rgb[0]})
    if max(delta) > TOL_BLOCK:
        hint = "（R 与 B 正好对调 → 编码时多换了一次通道）" if swapped else ""
        rep.update({"status": "fail",
                    "detail": f"逐通道 |Δ| 超过容差 {TOL_BLOCK}{hint}"})
    elif (cut_rgb[0] - cut_rgb[2]) * (src_rgb[0] - src_rgb[2]) < 0:
        rep.update({"status": "fail", "detail": "逐通道差在容差内但 R/B 的相对大小相反（色向被翻）"})
    return rep


def check_direction(root: pathlib.Path) -> list[dict]:
    """B：各 PNG / ICO 内嵌 PNG 的角色像素必须落在冷色侧（R < B）。"""
    icos: dict[str, dict[int, np.ndarray]] = {}
    out: list[dict] = []
    for rel, size, mode, note in _DIRECTION_CASES:
        rep: dict = {"id": "B", "name": f"{rel}" + (f" [{size}px]" if size else "") + f" · {note}",
                     "status": "ok", "numbers": {}}
        path = root / rel
        try:
            if size is None:
                im = _load(path)
            else:
                if rel not in icos:
                    icos[rel] = read_ico(path)
                if size not in icos[rel]:
                    rep.update({"status": "fail", "detail": f"ICO 里没有 {size} 档"})
                    out.append(rep)
                    continue
                im = icos[rel][size]
        except FileNotFoundError:
            rep.update({"status": "fail", "detail": "文件不存在"})
            out.append(rep)
            continue
        if im.ndim != 3 or im.shape[2] != 4:
            rep.update({"status": "fail",
                        "detail": f"不是 BGRA 四通道（shape={im.shape}）——图标必须带 alpha"})
            out.append(rep)
            continue
        mask = _MASKS[mode](im)
        if int(mask.sum()) < 16:
            rep.update({"status": "fail", "detail": f"取样像素太少（{int(mask.sum())} 个，口径 {mode}）"})
            out.append(rep)
            continue
        rgb = _mean_rgb(im[..., :3][mask])
        b_minus_r = rgb[2] - rgb[0]
        rep["numbers"].update({"mode": mode, "pixels": int(mask.sum()), "mean_rgb": list(rgb),
                               "B_minus_R": b_minus_r, "tolerance": TOL_DIRECTION})
        if b_minus_r < TOL_DIRECTION:
            rep.update({"status": "fail",
                        "detail": f"B−R = {b_minus_r:+.2f} < {TOL_DIRECTION} → 落在暖色侧"})
        out.append(rep)
    return out


def check_derived(root: pathlib.Path) -> list[dict]:
    """C：packaging/art/*.bmp 的 Logo 区必须与 assets/icon_256.png 同色（否则就是旧图）。"""
    out: list[dict] = []
    icon_path = root / _CHIP_ICON
    for name, (x, y, size) in _CHIP.items():
        rep: dict = {"id": "C", "name": f"packaging/art/{name} · Logo 区（芯片 {size}px）",
                     "status": "ok", "numbers": {}}
        bmp = root / "packaging" / "art" / name
        if not bmp.exists() or not icon_path.exists():
            rep.update({"status": "skip", "detail": "文件不在场（跳过）"})
            out.append(rep)
            continue
        icon = _load(icon_path)
        inner = int(size * 0.78)
        scale = inner / max(icon.shape[:2])
        ic = cv2.resize(icon, (max(1, int(icon.shape[1] * scale)),
                               max(1, int(icon.shape[0] * scale))), interpolation=cv2.INTER_AREA)
        oy, ox = y + (size - ic.shape[0]) // 2, x + (size - ic.shape[1]) // 2
        mask = ic[..., 3] > 200
        img = _load(bmp)
        ic_rgb = ic[..., :3].astype(np.float32)[mask]
        best: tuple[float, int, int, int, int] | None = None
        for dy in range(-CHIP_SEARCH, CHIP_SEARCH + 1):
            for dx in range(-CHIP_SEARCH, CHIP_SEARCH + 1):
                y0, x0 = oy + dy, ox + dx
                if y0 < 0 or x0 < 0 or y0 + ic.shape[0] > img.shape[0] \
                        or x0 + ic.shape[1] > img.shape[1] or img.shape[2] < 3:
                    continue
                roi = img[y0:y0 + ic.shape[0], x0:x0 + ic.shape[1], :3].astype(np.float32)
                d = float(np.abs(roi[mask] - ic_rgb).mean())
                if best is None or d < best[0]:
                    best = (d, dy, dx, y0, x0)
        if best is None:
            rep.update({"status": "fail", "detail": "在 bmp 里找不到图标区（尺寸不对？）"})
            out.append(rep)
            continue
        d, dy, dx, y0, x0 = best
        roi_px = img[y0:y0 + ic.shape[0], x0:x0 + ic.shape[1], :3][mask]
        rgb = _mean_rgb(roi_px)
        b_minus_r = rgb[2] - rgb[0]
        rep["numbers"].update({"pixels": int(mask.sum()), "bmp_mean_rgb": list(rgb),
                               "icon_mean_rgb": list(_mean_rgb(ic_rgb)),
                               "mean_abs_delta": d, "tolerance": TOL_DERIVED,
                               "found_offset": [x0, y0], "shift": [dx, dy],
                               "B_minus_R": b_minus_r})
        if d > TOL_DERIVED:
            rep.update({"status": "stale",
                        "detail": (f"与 {_CHIP_ICON} 不同色（逐通道 mean|Δ| = {d:.2f} > "
                                   f"{TOL_DERIVED}）→ 它是旧图标生成的，重跑 "
                                   f"python packaging/make_installer_art.py")})
        elif b_minus_r < TOL_DIRECTION:
            rep.update({"status": "fail",
                        "detail": f"与图标同色但 B−R = {b_minus_r:+.2f} < {TOL_DIRECTION}（图标本身就是暖的）"})
        out.append(rep)
    return out


def _print_report(rep: dict) -> None:
    print(f"[icon_check] 根目录 {rep['root']}")
    print(f"[icon_check] 原图   {rep['src']}")
    print("─" * 78)
    tag = {"ok": "[ OK ]", "fail": "[FAIL]", "stale": "[STALE]", "skip": "[SKIP]"}
    for c in rep["checks"]:
        n = c["numbers"]
        print(f"{tag[c['status']]} {c['id']} · {c['name']}")
        if c["id"] == "A" and "cutout_mean_rgb" in n:
            src_rgb = n.get("src_mean_rgb")
            print(f"      方块 {n['block']}×{n['block']}（alpha 全 255）中心 {tuple(n['block_center'])}"
                  + (f" → 原图定位 {tuple(n['src_loc'])}" if "src_loc" in n else ""))
            if src_rgb:
                print(f"      原图 mean RGB = {_fmt(tuple(src_rgb))}   B−R = {n['src_B_minus_R']:+.2f}")
            print(f"      抠图 mean RGB = {_fmt(tuple(n['cutout_mean_rgb']))}   "
                  f"B−R = {n['cutout_B_minus_R']:+.2f}")
            if "delta" in n:
                d = n["delta"]
                print(f"      逐通道 |Δ| = ({d[0]:.2f}, {d[1]:.2f}, {d[2]:.2f})   容差 {n['tolerance']}")
        elif c["id"] == "B" and "mean_rgb" in n:
            print(f"      取样 {n['pixels']} px（{n['mode']}）   mean RGB = {_fmt(tuple(n['mean_rgb']))}"
                  f"   B−R = {n['B_minus_R']:+.2f}   要求 ≥ {n['tolerance']}")
        elif c["id"] == "C" and "bmp_mean_rgb" in n:
            print(f"      取样 {n['pixels']} px   bmp mean RGB = {_fmt(tuple(n['bmp_mean_rgb']))}"
                  f"   B−R = {n['B_minus_R']:+.2f}")
            print(f"      图标 mean RGB = {_fmt(tuple(n['icon_mean_rgb']))}"
                  f"   逐通道 mean|Δ| = {n['mean_abs_delta']:.2f}   容差 {n['tolerance']}")
        if c.get("detail"):
            print(f"      → {c['detail']}")
    print("─" * 78)
    print(f"通过 {rep['n_ok']} / 违规 {rep['n_fail']} / 过期 {rep['n_stale']} / 跳过 {rep['n_skip']}"
          f"（共 {len(rep['checks'])} 项）")
    if rep["exit"] == 0:
        print("结论：颜色契约通过（原图 ↔ 抠图 ↔ 各尺寸产物 ↔ 安装器美术 Logo 同色向）")
    elif rep["exit"] == 2:
        print("结论：图标本体没问题，但派生物过期 —— 重跑 python packaging/make_installer_art.py")
    else:
        print("结论：颜色契约违规（见上面的差值；exit 1）")
    print("退出码：0=通过，1=颜色契约违规，2=派生物过期")


def check(root: pathlib.Path, src: pathlib.Path, *, derived: bool = True) -> dict:
    checks = [check_source_anchor(root, src)] + check_direction(root)
    if derived:
        checks += check_derived(root)
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_stale = sum(1 for c in checks if c["status"] == "stale")
    rep = {"root": str(root), "src": str(src), "checks": checks,
           "n_ok": sum(1 for c in checks if c["status"] == "ok"),
           "n_fail": n_fail, "n_stale": n_stale,
           "n_skip": sum(1 for c in checks if c["status"] == "skip")}
    rep["exit"] = 1 if n_fail else (2 if n_stale else 0)
    rep["ok"] = rep["exit"] == 0
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="图标颜色契约门禁（源图 ↔ 抠图 ↔ 各尺寸产物）")
    ap.add_argument("--root", default=str(ROOT), help="仓库根目录（默认本文件的上两级）")
    ap.add_argument("--src", default=DEFAULT_SRC, help="原图路径（只读；不在场则跳过 A）")
    ap.add_argument("--no-derived", action="store_true", help="跳过 packaging/art 的派生物同步检查")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)
    rep = check(pathlib.Path(args.root), pathlib.Path(args.src), derived=not args.no_derived)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_report(rep)
    return rep["exit"]


if __name__ == "__main__":
    sys.exit(main())

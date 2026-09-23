# -*- coding: utf-8 -*-
"""图标管线：从原图高精度抠图 → 透明 PNG + 多尺寸 ICO（含"小尺寸用胸像"的专业做法）

用法
    python tools/make_icon.py --src "C:\\HONOR Share\\Honor Share\\19th_149128711.jpg"

做出来的东西（全部落在 `assets/`）
    icon_source_cutout.png   全分辨率 RGBA 抠图（后续所有尺寸都从它派生）
    icon_256.png             256×256 透明预览（全身）
    icon_bust_256.png        256×256 透明预览（胸像）
    icon.ico                 主图标：**16/24/32 用胸像、48+ 用全身**（Windows 读哪个尺寸就给哪个）
    icon_full.ico            全部尺寸都用全身（备选）
    icon_bust.ico            全部尺寸都用胸像（备选）

为什么这么抠（而不是一键抠图库）
    1) 背景是"近纯白 + 浅灰网格/标注/水印 + 柔和投影"：先用**边界像素建背景色模型**（Lab 空间、
       按通道标准差归一化），比"矩形框丢给 GrabCut"稳得多——标注文字和网格线不会被当成主体。
    2) 形态学闭合 + **最大连通域**：去掉散布的标注碎块；再**填洞**，避免主体内部出现透明孔。
    3) **GrabCut 精修**（以第 1、2 步的结果做 trimap）：把头发边缘、麦克风支架这类细结构收干净。
    4) 收边：掩膜先腐蚀 1px 再高斯羽化 + smoothstep，得到"核心实、边缘抗锯齿"的 alpha；
       再做**边缘去色污染**（halo 抑制）——否则抠出来的浅色背景会在角色轮廓留下白边。
    5) ICO 缩放用 **INTER_AREA + 预乘 alpha**：否则缩小时透明边缘会发黑/发白。
    6) 每个尺寸都单独重采样（不靠 Windows 缩放），并给足留白（图标需要呼吸位）。

🖼️ 原图纪律：**只读**。本脚本不改动、不覆盖、不重编码源图；只把派生结果写进 `assets/`。
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import sys

import cv2
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
DEFAULT_SRC = r"C:\HONOR Share\Honor Share\19th_149128711.jpg"

# 工作分辨率上限（掩膜在这一档算，最后映射回原分辨率）
WORK_MAX_SIDE = 1400
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
# 小尺寸用胸像：16–48px 下全身立绘是糊的，而且胸像区域完全没有背景残留（最干净）
SMALL_SIZES = (16, 24, 32, 48)
DARK_BG = (14, 22, 32)              # 备用深色底（BGR）
MARGIN = 0.07                       # 图标留白比例


def cutout_rembg(img: np.ndarray, model: str = "isnet-anime",
                 finalize: bool = True) -> np.ndarray:
    """专业抠图分支：rembg（onnxruntime）+ `isnet-anime`（专为动漫立绘训练）。

    为什么优先用它：立绘抠图的难点是"浅色衣物/皮肤贴着浅色背景"和"柔和投影"，
    这两类靠颜色/阈值/形态学都很难稳；学习型分割模型是这活的正解。
    `finalize=True` 时再走一遍统一收尾（闭运算 → 填洞 → 保留够大的连通域 → 腐蚀 → 羽化），
    保证内部无孔、边缘抗锯齿。

    ⚠️ 它只是**开发期生成素材**用，不进 APP 运行时依赖（打包体积不受影响）。
    ⚠️ **不要再往上叠"按颜色剔背景"的规则**：本工具试过"浅色可达泛洪"，它会把**脸/白衬衫
    一起吃掉**（皮肤与白衣服也是浅色、且与背景接壤）——这是 13 版迭代里最贵的一课。
    """
    from rembg import new_session, remove            # 延迟导入：没装也能用 cv2 分支
    session = new_session(model)                     # 首次会下载模型到 ~/.rembg/
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise SystemExit("编码失败（喂给 rembg 之前的 PNG 编码）")
    out = remove(buf.tobytes(), session=session, post_process_mask=False)
    rgba = cv2.imdecode(np.frombuffer(out, np.uint8), cv2.IMREAD_UNCHANGED)
    if rgba is None or rgba.shape[2] != 4:
        raise SystemExit("rembg 返回的不是 RGBA")
    if finalize:
        alpha = finialize_mask((rgba[..., 3] > 100).astype(np.uint8) * 255)
        rgba = decontaminate(img, alpha)
    return rgba                                       # cv2 解码 PNG 得到的是 BGRA


def load_image(path: str) -> np.ndarray:
    """中文/空格路径安全读图（cv2.imread 不支持 Unicode 路径，会静默返回 None）。"""
    buf = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读图失败（路径或格式问题）：{path}")
    return img


def imwrite_u(path: pathlib.Path, bgra: np.ndarray) -> None:
    """中文/空格路径安全写图。

    ⚠️ **不要用 cv2.imwrite**：它在 Windows 上走 ANSI API，路径含中文时**静默失败**
    （返回 False 不抛异常，文件根本不出现）。本工具第一版就踩了这个坑——
    `assets/` 的路径里有中文，四张 PNG 一张都没写出来，而 ICO 写成了（那是 Python 的文件 I/O）。
    """
    ok, buf = cv2.imencode(".png", cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGBA))
    if not ok:
        raise SystemExit(f"PNG 编码失败：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


def resize_rgba(img: np.ndarray, size: int) -> np.ndarray:
    """RGBA 缩放：先预乘 alpha 再 INTER_AREA，避免透明边缘发黑/发白。"""
    h, w = img.shape[:2]
    a = img[..., 3:4].astype(np.float32) / 255.0
    rgb = img[..., :3].astype(np.float32) * a
    pre = np.dstack([rgb, img[..., 3:4]]).astype(np.uint8)
    interp = cv2.INTER_AREA if size < max(h, w) else cv2.INTER_LANCZOS4
    out = cv2.resize(pre, (size, size), interpolation=interp).astype(np.float32)
    a2 = out[..., 3:4] / 255.0
    rgb2 = np.where(a2 > 1e-4, out[..., :3] / np.maximum(a2, 1e-4), 0)
    res = np.dstack([np.clip(rgb2, 0, 255), out[..., 3:4]]).astype(np.uint8)
    return res


def keep_components(mask: np.ndarray, min_area_ratio: float = 0.003) -> np.ndarray:
    """保留所有"够大的"连通域，并淘汰"横跨整幅"的框状块。

    为什么不只留最大连通域：立绘常被拆成多块（头/手臂与躯干之间可能被浅色区域断开），
    只留最大块会把脑袋整块丢掉（这一版之前就丢过）。
    为什么还要淘汰框状块：构图框/网格线一旦成连通域，会因为"贴着图边、横跨整幅"而留下来。
    """
    h, w = mask.shape[:2]
    n, lab, st, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n <= 1:
        return mask
    out = np.zeros_like(mask)
    min_area = max(64, int(h * w * min_area_ratio))
    for i in range(1, n):
        x, y, bw, bh, area = st[i]
        if area < min_area:
            continue
        spans = (bw > 0.9 * w) and (bh > 0.9 * h)          # 框状：两个方向几乎都占满
        if spans and area < 0.15 * h * w:                   # 但面积不大 → 是细框，不是主体
            continue
        out[lab == i] = 255
    return out


def build_alpha(img: np.ndarray, debug_dir: pathlib.Path | None = None) -> tuple[np.ndarray, np.ndarray, dict]:
    """返回 (alpha 全尺寸, 掩膜预览, 诊断)。

    判据（第四版；前三版都因为"背景被当主体"失败，过程见 DEVLOG）：
      * **种子要严**：`sat>75`（鲜青发）或 `val<120`（真黑线/衣物）——背景的浅灰、投影的灰、
        构图框的灰线都不在其中；
      * **闭运算的核要大**：白衬衫/浅色皮肤这类"被深色轮廓包住"的区域宽度可达上百像素，
        核太小（前几版用 1% 短边 ≈10px）根本桥不过去 → 填洞补不回来 → GrabCut 于是把背景判成前景。
        这里用 **4% 短边**（约 40px），且闭运算只填凹口、不会把外轮廓吹胖；
      * **填洞**：补回面孔、白衬衫、浅色内衬；
      * **GrabCut 只做边缘抛光**（BGD=从边界泛洪可达的浅色背景）。
    """
    h0, w0 = img.shape[:2]
    scale = min(1.0, WORK_MAX_SIDE / max(h0, w0))
    work = cv2.resize(img, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_AREA) \
        if scale < 1.0 else img.copy()
    h, w = work.shape[:2]

    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]

    # ① 严种子
    dark = ((val < 120).astype(np.uint8)) * 255
    colored = ((sat > 75).astype(np.uint8)) * 255
    k_line = max(3, int(min(h, w) * 0.005)) | 1                    # 剔细线（框线/网格/文字）
    ker_line = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_line, k_line))
    seed = cv2.bitwise_or(cv2.morphologyEx(dark, cv2.MORPH_OPEN, ker_line),
                          cv2.morphologyEx(colored, cv2.MORPH_OPEN, ker_line))

    # ② 大核闭运算：把"被轮廓包住"的浅色区域接起来
    k_close = max(5, int(min(h, w) * 0.040)) | 1
    ker_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_close, k_close))
    closed = cv2.morphologyEx(seed, cv2.MORPH_CLOSE, ker_close)

    # ③ 填洞
    ff = closed.copy()
    m2 = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, m2, (0, 0), 255)
    filled = cv2.bitwise_or(closed, cv2.bitwise_not(ff))

    # ④ 保留所有够大的连通域（头/手臂可能和躯干断开，不能只留最大块）
    m = keep_components(filled)

    # ⑤ GrabCut 只抛光边缘
    outside = ((sat < 32) & (val > 205)).astype(np.uint8) * 255
    ff2 = outside.copy()
    m3 = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff2, m3, (0, 0), 255)
    outside = ff2
    gc = np.full((h, w), cv2.GC_PR_FGD, np.uint8)
    gc[outside > 0] = cv2.GC_BGD
    gc[m == 0] = cv2.GC_PR_BGD
    gc[cv2.erode(m, ker_line) > 0] = cv2.GC_FGD
    bgd = np.zeros((1, 65), np.float64)
    fgd = np.zeros((1, 65), np.float64)
    cv2.grabCut(work, gc, None, bgd, fgd, 3, cv2.GC_INIT_WITH_MASK)
    pol = np.where((gc == cv2.GC_FGD) | (gc == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    mask = cv2.bitwise_and(pol, m)          # 关键：**只允许在已判定的轮廓内**抛光，别让它把背景吃回来
    mask = finialize_mask(mask)

    alpha = cv2.resize(mask, (w0, h0), interpolation=cv2.INTER_LINEAR) if scale < 1.0 else mask

    if debug_dir is not None:               # 中间掩膜导出（人工核对：哪一步错了一眼看到）
        debug_dir.mkdir(parents=True, exist_ok=True)
        for nm, arr in (("1_seed", seed), ("2_closed", closed), ("3_filled", filled),
                        ("4_largest", m), ("5_polished", mask)):
            imwrite_u(debug_dir / f"mask_{nm}.png", cv2.cvtColor(arr, cv2.COLOR_GRAY2BGRA))

    info = {
        "work_size": (w, h),
        "fg_ratio": float((alpha > 128).mean()),
        "edge_ratio": float(((alpha > 12) & (alpha < 243)).mean()),
        "outside_ratio": float((outside > 0).mean()),
        "seed_ratio": float((seed > 0).mean()),
        "dark_thick_ratio": float((dark > 0).mean()),
        "colored_ratio": float((colored > 0).mean()),
        "k_close": k_close,
    }
    return alpha, mask, info


def ring_ok(h: int, w: int) -> np.ndarray:  # pragma: no cover - 保留给"只在边界找背景"的旧策略
    """占位：背景可以出现在图内任何位置（网格/标注/水印都在内部），所以不做位置限制。"""
    return np.ones((h, w), bool)


def decontaminate(img: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """边缘去色污染：把半透明像素里的背景色分量减掉（抑制白边/halo）。"""
    b = max(8, int(min(img.shape[:2]) * 0.01))
    bg = np.median(img[:b, :].reshape(-1, 3), axis=0).astype(np.float32)   # 背景估计（BGR）
    a = (alpha.astype(np.float32) / 255.0)[..., None]
    px = img.astype(np.float32)
    fixed = np.where(a > 1e-3, (px - (1.0 - a) * bg) / np.maximum(a, 1e-3), px)
    return np.dstack([np.clip(fixed, 0, 255), alpha]).astype(np.uint8)


def crop_to_content(rgba: np.ndarray, pad_ratio: float = 0.02) -> np.ndarray:
    a = rgba[..., 3]
    ys, xs = np.where(a > 8)
    if len(ys) == 0:
        raise SystemExit("抠图失败：alpha 全空")
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    pad = int(max(y1 - y0, x1 - x0) * pad_ratio)
    y0, y1 = max(0, y0 - pad), min(rgba.shape[0], y1 + pad)
    x0, x1 = max(0, x0 - pad), min(rgba.shape[1], x1 + pad)
    return rgba[y0:y1, x0:x1]


def square_canvas(rgba: np.ndarray, margin: float = MARGIN, anchor: str = "center") -> np.ndarray:
    """把裁剪后的角色放进透明正方形（图标需要留白呼吸位）。"""
    h, w = rgba.shape[:2]
    side = int(round(max(h, w) / (1.0 - 2 * margin)))
    canvas = np.zeros((side, side, 4), np.uint8)
    x = (side - w) // 2
    if anchor == "center":
        y = (side - h) // 2
    elif anchor == "top":
        y = int(side * margin)
    else:                                   # bottom
        y = side - h - int(side * margin)
    canvas[y:y + h, x:x + w] = rgba
    return canvas


def bust_crop(rgba: np.ndarray) -> np.ndarray:
    """"头肩"裁剪（小尺寸图标用）：**用原图比例框**，不做启发式检测。

    为什么改成固定比例：这张立绘的背景残留（水印/框线）会把"alpha 质心""肤色检测"这类
    启发式全部带偏（试过 4 版，都会裁到胸口或把水印框进来）。一次性素材，确定性优先：
    比例是按原图肉眼读出来的——头部约在 x 28%–45%、y 8%–22%，
    取一个把"头 + 上肩"框住的方形（含上方头发空间）。
    要微调就改下面这四个数，改完重跑本脚本即可。
    """
    h, w = rgba.shape[:2]
    x0f, y0f, x1f, y1f = BUST_RECT
    side = int(min((x1f - x0f) * w, (y1f - y0f) * h))
    cx = int((x0f + x1f) / 2 * w)
    cy = int((y0f + y1f) / 2 * h)
    x0 = int(np.clip(cx - side / 2, 0, max(0, w - side)))
    y0 = int(np.clip(cy - side / 2, 0, max(0, h - side)))
    out = rgba[y0:y0 + side, x0:x0 + side]
    return crop_to_content(out) if out.size else out


BUST_RECT = (0.36, 0.06, 0.66, 0.30)      # (x0, y0, x1, y1) 原图比例：**聚焦头部**（图标惯例）
# 画框核对（改比例框时先跑这个，别在缩略图上猜）：
#     python tools/make_icon.py --draw-rect



def compose_dark(rgba: np.ndarray, size: int) -> np.ndarray:
    """把角色放到深色圆角方块上（给浅色任务栏/开始菜单一个可读的备选）。"""
    canvas = np.zeros((size, size, 4), np.uint8)
    r = int(size * 0.22)
    cv2.rectangle(canvas, (0, 0), (size - 1, size - 1), (*DARK_BG, 255), -1)
    # 圆角（用四个圆角矩形挖掉角）
    for cx, cy in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
        cv2.circle(canvas, (cx, cy), r, (0, 0, 0, 0), -1)
    inner = resize_rgba(square_canvas(rgba, margin=0.16), int(size * 0.98))
    a = inner[..., 3:4].astype(np.float32) / 255.0
    ih, iw = inner.shape[:2]
    y = (size - ih) // 2
    x = (size - iw) // 2
    roi = canvas[y:y + ih, x:x + iw].astype(np.float32)
    canvas[y:y + ih, x:x + iw] = np.dstack([
        (inner[..., :3].astype(np.float32) * a + roi[..., :3] * (1 - a)),
        (a * 255 + roi[..., 3:4] * (1 - a)),
    ]).astype(np.uint8)
    return canvas


def write_ico(entries: list[tuple[int, np.ndarray]], path: pathlib.Path) -> None:
    """手写 ICO 容器，每个尺寸内嵌 PNG（Vista+ 支持）。这样每个尺寸都能单独控制重采样。"""
    blobs = []
    for size, rgba in entries:
        ok, buf = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_BGRA2RGBA))
        if not ok:
            raise SystemExit(f"PNG 编码失败：size={size}")
        blobs.append((size, buf.tobytes()))
    n = len(blobs)
    header = struct.pack("<HHH", 0, 1, n)
    offset = 6 + 16 * n
    dirs, data = b"", b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size
        dirs += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    path.write_bytes(header + dirs + data)


def finialize_mask(mask: np.ndarray, k_ratio: float = 0.008,
                   subtract_mask: np.ndarray | None = None) -> np.ndarray:
    """掩膜收尾：闭运算 → 填洞 → 保留够大的连通域 → 腐蚀 1px → 羽化(smoothstep)。

    `subtract_mask`：**填洞之后**再减掉的区域（例如"从边界可达的浅色背景"）。
    顺序很重要：先填洞补回白衬衫，再减掉可达背景——这样"背景"不会被填洞步骤又补回来。
    """
    h, w = mask.shape[:2]
    k = max(3, int(min(h, w) * k_ratio)) | 1
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ker)
    ff = m.copy()
    m2 = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, m2, (0, 0), 255)
    m = cv2.bitwise_or(m, cv2.bitwise_not(ff))          # 填洞
    if subtract_mask is not None:
        m = cv2.bitwise_and(m, cv2.bitwise_not(subtract_mask))
    m = keep_components(m)
    m = cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    soft = cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), 1.1)
    lo, hi = 0.30, 0.70
    soft = np.clip((soft - lo) / (hi - lo), 0.0, 1.0)
    soft = soft * soft * (3 - 2 * soft)
    return (soft * 255).astype(np.uint8)


def cutout_hybrid(img: np.ndarray, model: str = "isnet-anime") -> tuple[np.ndarray, dict]:
    """混合抠图（**采用版**）：rembg 给轮廓 + 浅色可达泛洪剥背景 + 细远离剔除。

    为什么是这套组合（12 版迭代的结论，历史见 DEVLOG `图标抠图` 一节）：
      * **轮廓**取自 rembg/isnet-anime——它给出的剪影最准（头/发/身/支架齐全）；
      * **浅色可达泛洪**（从边界沿"低饱和 + 高亮"像素泛洪，可达者算背景）能剥掉整片浅灰背景
        与近白框线；白衬衫/浅肤虽浅，但被深色轮廓圈住、泛洪到不了 → 保住；
      * **细且远离核心**的像素（网格线是青色的，颜色规则拦不住）按"细 + 远"剔除。

    ⚠️ **已知残留（诚实记录）**：这张立绘的背景上有**"01"水印**（中性灰 val≈143，笔画较粗）、
    构图框线与柔和投影。上述规则能去掉大部分，但"01"与角色头发重叠处会有**浅灰残留**。
    尝试过更激进的规则（加保护圈、加屏障、收窄阈值），都会把白衬衫/躯干打穿或把主体割碎——
    对**图标**而言不值得（图标用胸像裁剪，该区域完全没有残留）。
    如果要"打印级全身透明图"，建议：导出 `mask_preview.png` → 手工修掩膜 → 用 `--mask` 复用。
    """
    alpha_cv2, _mask_work, info_cv2 = build_alpha(img)
    rgba_rb = cutout_rembg(img, model)
    alpha_rb = rgba_rb[..., 3]
    if alpha_rb.shape != alpha_cv2.shape:
        alpha_rb = cv2.resize(alpha_rb, (alpha_cv2.shape[1], alpha_cv2.shape[0]),
                              interpolation=cv2.INTER_LINEAR)
    h, w = alpha_rb.shape[:2]

    # ① 浅色可达泛洪 → 背景（阈值 val>172：近白框线/水印外缘都在这集合里）
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat, val = hsv[..., 1], hsv[..., 2]
    light = (((sat < 42) & (val > 172)).astype(np.uint8)) * 255
    ff = light.copy()
    m2 = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(ff, m2, (0, 0), 255)
    bg_reach = ff

    # ② rembg 掩膜 − 可达背景
    rb_mask = ((alpha_rb > 100).astype(np.uint8)) * 255
    m = cv2.bitwise_and(rb_mask, cv2.bitwise_not(bg_reach))

    # ③ 细 且 远离核心 → 剔除（网格线这类彩色细线只能靠"细+远"识别）
    k_core = max(5, int(min(h, w) * 0.009)) | 1
    core = cv2.morphologyEx((((sat > 60) | (val < 120)).astype(np.uint8)) * 255,
                            cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_core, k_core)))
    r = max(6, int(min(h, w) * 0.014))
    core_near = cv2.dilate(core, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    dist = cv2.distanceTransform((rb_mask > 0).astype(np.uint8), cv2.DIST_L2, 5)
    thin_far = (dist <= r) & (core_near == 0)
    m[thin_far] = 0

    alpha = finialize_mask(m, subtract_mask=bg_reach)
    rgba = decontaminate(img, alpha)
    info = dict(info_cv2)
    info.update({"engine": f"hybrid(rembg/{model} + 浅色可达泛洪 + 细远离剔除)",
                 "fg_ratio": float((alpha > 128).mean()),
                 "edge_ratio": float(((alpha > 12) & (alpha < 243)).mean()),
                 "rb_fg_ratio": float((rb_mask > 0).mean()),
                 "bg_reach_ratio": float((bg_reach > 0).mean()),
                 "thin_far_ratio": float(thin_far.mean())})
    return rgba, info


def draw_rect_check(img: np.ndarray, out: pathlib.Path) -> None:
    """把 `BUST_RECT` 画在原图上（带 10% 网格），用于**改比例框时核对位置**。

    为什么需要它：改比例框时若只在 256px 缩略图上肉眼估位置，很容易把"手臂"当成"头"
    （本工具开发时就真的搞错过一次），最后靠这张标了网格的核对图一次改对。
    """
    vis = img.copy()
    h, w = vis.shape[:2]
    for i in range(1, 10):
        cv2.line(vis, (int(w * i / 10), 0), (int(w * i / 10), h), (150, 150, 150), 1)
        cv2.line(vis, (0, int(h * i / 10)), (w, int(h * i / 10)), (150, 150, 150), 1)
        cv2.putText(vis, str(i * 10), (int(w * i / 10) + 4, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        cv2.putText(vis, str(i * 10), (6, int(h * i / 10) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    x0f, y0f, x1f, y1f = BUST_RECT
    cv2.rectangle(vis, (int(x0f * w), int(y0f * h)), (int(x1f * w), int(y1f * h)),
                  (0, 0, 255), 6)
    small = cv2.resize(vis, (int(w * 0.42), int(h * 0.42)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    pathlib.Path(out).write_bytes(buf.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="原图路径（只读）")
    ap.add_argument("--out", default=str(ASSETS), help="输出目录")
    ap.add_argument("--engine", default="auto", choices=("auto", "rembg", "cv2", "hybrid"),
                    help="抠图引擎：auto=rembg（推荐）；cv2=自研管线；hybrid=旧实验组合（不推荐）")
    ap.add_argument("--model", default="isnet-anime", help="rembg 模型名（动漫立绘用 isnet-anime）")
    ap.add_argument("--draw-rect", action="store_true",
                    help="只在原图上画出胸像比例框（核对位置用），不做抠图")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    img = load_image(args.src)
    if args.draw_rect:
        draw_rect_check(img, out / "_rect_check.jpg")
        print(f"已出核对图：{out / '_rect_check.jpg'}（BUST_RECT={BUST_RECT}）")
        return 0
    engine = args.engine
    has_rembg = True
    try:
        import rembg  # noqa: F401
    except Exception:
        has_rembg = False
    if engine == "auto":
        engine = "rembg" if has_rembg else "cv2"
    if engine in ("rembg", "hybrid") and not has_rembg:
        print("[warn] 未安装 rembg，回退 cv2 管线")
        engine = "cv2"

    info: dict = {}
    if engine == "hybrid":
        rgba_full, info = cutout_hybrid(img, args.model)
        trimmed = crop_to_content(rgba_full)
    elif engine == "rembg":
        rgba_full = cutout_rembg(img, args.model)
        alpha = rgba_full[..., 3]
        trimmed = crop_to_content(rgba_full)
        info = {"engine": f"rembg/{args.model}", "work_size": img.shape[:2],
                "fg_ratio": float((alpha > 128).mean()),
                "edge_ratio": float(((alpha > 12) & (alpha < 243)).mean())}
    else:
        alpha, mask_work, info = build_alpha(img, debug_dir=out / "_debug_mask")
        rgba_full = decontaminate(img, alpha)
        trimmed = crop_to_content(rgba_full)
        info["engine"] = "cv2（自研管线）"
        imwrite_u(out / "mask_preview.png", cv2.cvtColor(mask_work, cv2.COLOR_GRAY2BGRA))

    # 胸像：**在原始坐标系上按比例框裁**（不要用 trimmed——背景残留会把定位带偏）
    bust = bust_crop(rgba_full)

    # 全分辨率抠图（后续派生都从它来）
    imwrite_u(out / "icon_source_cutout.png", trimmed)
    # 预览
    imwrite_u(out / "icon_256.png", resize_rgba(square_canvas(trimmed), 256))
    imwrite_u(out / "icon_bust_256.png", resize_rgba(square_canvas(bust), 256))
    imwrite_u(out / "icon_dark_256.png", compose_dark(trimmed, 256))

    # ICO：主图标在小尺寸用胸像、大尺寸用全身（Windows 按需取用）
    entries_main: list[tuple[int, np.ndarray]] = []
    for s in ICO_SIZES:
        src_rgba = bust if s in SMALL_SIZES else trimmed
        entries_main.append((s, resize_rgba(square_canvas(src_rgba), s)))
    write_ico(entries_main, out / "icon.ico")
    write_ico([(s, resize_rgba(square_canvas(trimmed), s)) for s in ICO_SIZES], out / "icon_full.ico")
    write_ico([(s, resize_rgba(square_canvas(bust), s)) for s in ICO_SIZES], out / "icon_bust.ico")
    write_ico([(s, compose_dark(trimmed, s)) for s in ICO_SIZES], out / "icon_dark.ico")

    print("─" * 66)
    print(f"原图      : {args.src}")
    print(f"引擎      : {info.get('engine', engine)}")
    print(f"原尺寸    : {img.shape[1]}×{img.shape[0]}  →  裁剪后 {trimmed.shape[1]}×{trimmed.shape[0]}")
    print(f"前景占比  : {info['fg_ratio'] * 100:.1f}%   边缘带（半透明像素）: {info['edge_ratio'] * 100:.2f}%")
    if engine == "cv2":
        print(f"种子/成块深色/彩色 : {info['seed_ratio'] * 100:.1f}% / "
              f"{info['dark_thick_ratio'] * 100:.1f}% / {info['colored_ratio'] * 100:.1f}%"
              f"   背景（泛洪可达）: {info['outside_ratio'] * 100:.1f}%")
    print(f"工作分辨率: {info['work_size']}")
    print(f"输出      : {', '.join(sorted(p.name for p in out.glob('*')))}")
    print("─" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())

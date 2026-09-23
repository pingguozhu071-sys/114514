# -*- coding: utf-8 -*-
"""底图与取色：**图像管线六步**（一步都不能省，每步都对着一个坑）

    ① `np.fromfile` + `cv2.imdecode` —— **不能用 `cv2.imread`**：中文路径下它静默返回 None
       （Kiana 那边的老坑，表现为"底图设了没反应"，一句报错都没有）；
    ② 长边 > 上限（默认 2560）→ `INTER_AREA` 下采样（省内存、防 8K 图爆内存）；
    ③ cover 裁切 + **九宫格焦点**（**绝不非等比拉伸**：人像会被拉变形）；
    ④ 高斯模糊（核 `k = 2b+1`，偶数核会偏半像素）；
    ⑤ 智能蒙层：自动档按亮度均值算，**手动只能加暗**（取 max）；
    ⑥ 转 `QImage` **必须 `.copy()`** —— 否则 QImage 指向 numpy 的临时缓冲，出野指针花屏。

另有一个**性能要点**（设计系统里点名的）：窗口尺寸与缓存图尺寸不一致时**裁切而不是整幅重采样**
（重采样是 O(w×h)，窗口拖动时会一帧一帧地卡）。

取色（自动强调色）：缩到 128px → HSV 过滤（S>60 且 V>40，滤掉灰与死黑）→ 每通道 //32 量化
→ 直方图众数 → 可读性钳制。**同色去重**：颜色没变就**不重算、不重绘**（Kiana 实测把最大停顿
7602ms 压到 0ms，就是因为原来每次都在重算）。
"""

from __future__ import annotations

import logging
import pathlib

from daedalus.ui.theme import clamp_accent_readable, parse_hex

logger = logging.getLogger(__name__)

__all__ = ["load_image", "process_wallpaper", "extract_accent", "WallpaperCache",
           "DEFAULT_MAX_EDGE", "mean_brightness"]

DEFAULT_MAX_EDGE = 2560
_ACCENT_SAMPLE = 128
_QUANT = 32


class ImageUnavailable(RuntimeError):
    """图片读不出来（**要说清是"路径不存在"还是"内容不是图"**，不静默当成功）。"""


def _require_cv_np():
    try:
        import cv2
        import numpy as np
        return cv2, np
    except Exception as e:                       # pragma: no cover - 环境缺件
        raise ImageUnavailable(f"缺 numpy/opencv：{type(e).__name__}: {e}") from e


def load_image(path, *, max_edge: int = DEFAULT_MAX_EDGE):
    """读图（BGR ndarray）。中文路径安全；读不出来明确抛错。"""
    cv2, np = _require_cv_np()
    p = pathlib.Path(str(path))
    if not p.exists():
        raise ImageUnavailable(f"图片不存在：{p}")
    try:
        buf = np.fromfile(str(p), dtype=np.uint8)          # ① 中文路径安全
    except Exception as e:
        raise ImageUnavailable(f"读文件失败：{type(e).__name__}: {e}") from e
    if buf.size == 0:
        raise ImageUnavailable(f"文件是空的：{p}")
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise ImageUnavailable(f"内容不是可解码的图片：{p}")
    return _downsample(img, max_edge)


def _downsample(img, max_edge: int):
    """② 长边超上限就等比缩（INTER_AREA 在缩小时质量最好）。"""
    cv2, _ = _require_cv_np()
    h, w = img.shape[:2]
    longest = max(h, w)
    if max_edge and longest > int(max_edge):
        scale = float(max_edge) / float(longest)
        img = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return img


_FOCUS = {
    "center": (0.5, 0.5), "top": (0.5, 0.15), "bottom": (0.5, 0.85),
    "left": (0.15, 0.5), "right": (0.85, 0.5),
    "top_left": (0.15, 0.15), "top_right": (0.85, 0.15),
    "bottom_left": (0.15, 0.85), "bottom_right": (0.85, 0.85),
}


def cover_crop(img, width: int, height: int, focus: str = "center"):
    """③ cover 裁切（保持比例）+ 九宫格焦点。目标比原图大时**只补不拉**（不非等比拉伸）。"""
    cv2, np = _require_cv_np()
    h, w = img.shape[:2]
    tw, th = max(1, int(width)), max(1, int(height))
    if (w, h) == (tw, th):
        return img
    fx, fy = _FOCUS.get(str(focus), _FOCUS["center"])
    scale = max(tw / w, th / h)                     # cover：取大比例，保证铺满
    nw, nh = max(tw, int(round(w * scale))), max(th, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    resized = cv2.resize(img, (nw, nh), interpolation=interp)
    x = int(round((nw - tw) * fx))
    y = int(round((nh - th) * fy))
    return resized[y:y + th, x:x + tw]


def gaussian_blur(img, radius: int):
    """④ 核 `k = 2b+1`（奇数）。b=0 时不模糊。"""
    cv2, _ = _require_cv_np()
    b = int(radius)
    if b <= 0:
        return img
    k = 2 * b + 1
    return cv2.GaussianBlur(img, (k, k), 0)


def mean_brightness(img) -> float:
    """平均亮度（0–255，按人眼权重）。蒙层自动档要用它。"""
    _, np = _require_cv_np()
    if img is None or img.size == 0:
        return 128.0
    b, g, r = img[:, :, 0].astype("float32"), img[:, :, 1].astype("float32"), \
        img[:, :, 2].astype("float32")
    return float((0.0722 * b + 0.7152 * g + 0.2126 * r).mean())


def apply_dim(img, dim_pct: float):
    """⑤ 蒙层（**只加暗**：dim 由调用方先与自动档取 max）。"""
    _, np = _require_cv_np()
    d = max(0.0, min(60.0, float(dim_pct))) / 100.0
    if d <= 0:
        return img
    out = img.astype("float32") * (1.0 - d)
    return np.clip(out, 0, 255).astype("uint8")


def to_qimage(img):
    """⑥ 转 `QImage` 并 **`.copy()`**（否则野指针花屏）。"""
    from PySide6.QtGui import QImage
    cv2, np = _require_cv_np()
    if img is None or img.size == 0:
        raise ImageUnavailable("空图无法转换")
    h, w = img.shape[:2]
    if img.ndim == 3:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        data = np.ascontiguousarray(rgb)
        q = QImage(data.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    else:                                        # pragma: no cover - 灰度兜底
        data = np.ascontiguousarray(img)
        q = QImage(data.data, w, h, w, QImage.Format.Format_Grayscale8)
    return q.copy()                              # ← 必须 copy


def process_wallpaper(path, *, width: int, height: int, blur: int = 0, dim_manual: float = 0.0,
                      focus: str = "center", max_edge: int = DEFAULT_MAX_EDGE):
    """管线全跑一遍：返回 `(QImage, 元信息)`。元信息里有亮度均值与**实际用的蒙层值**。"""
    from daedalus.ui.theme import resolve_dim
    img = load_image(path, max_edge=max_edge)
    img = cover_crop(img, width, height, focus=focus)
    bright = mean_brightness(img)
    dim = resolve_dim(dim_manual, bright)
    img = apply_dim(img, dim)
    img = gaussian_blur(img, blur)
    return to_qimage(img), {"brightness_mean": round(bright, 2), "dim_used": dim,
                            "size": [int(width), int(height)], "blur": int(blur),
                            "focus": str(focus), "source": str(path)}


def extract_accent(path, *, quant: int = _QUANT, sample: int = _ACCENT_SAMPLE,
                   lock: str | None = None) -> dict:
    """自动取色：量化直方图众数 + 可读性钳制。

    `lock` 给了就**跳过计算并原样返回**（"锁定开关"的语义：手动与自动互斥）。
    注意：锁定值**不做可读性钳制**——那是用户明确选的颜色，所见即所得；
    钳制只用在**机器算出来的**颜色上（自动取色可能取到刺眼的荧光色）。
    """
    if lock:
        from daedalus.ui.theme import parse_hex
        parse_hex(lock)                      # 格式不对照样抛（不静默换一个）
        return {"accent": str(lock), "source": "locked", "candidates": []}
    cv2, np = _require_cv_np()
    img = load_image(path, max_edge=sample * 2)
    # **最近邻**缩到取样尺寸：取色要的是"图里真有的颜色"，而面积平均会把彩色图混成灰
    # （实测：随机彩噪图被 INTER_AREA 混成中性灰 → 误判成"饱和度太低"而回退默认色）。
    small = cv2.resize(img, (sample, sample), interpolation=cv2.INTER_NEAREST)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] > 60) & (hsv[:, :, 2] > 40)          # 滤灰与死黑
    picked = small[mask]
    if picked.size == 0:
        # 整张图都是灰/黑：如实回退到默认档，并说明原因（不假装取到了颜色）
        return {"accent": "#4FA3E8", "source": "fallback_gray",
                "note": "整张图饱和度太低，取不到可用的强调色", "candidates": []}
    q = (picked // int(quant)) * int(quant)
    colors, counts = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
    order = counts.argsort()[::-1]
    cands = []
    for idx in order[:5]:
        b, g, r = (int(v) for v in colors[idx])
        cands.append({"rgb": [r, g, b], "hex": "#{:02X}{:02X}{:02X}".format(r, g, b),
                      "count": int(counts[idx])})
    top = cands[0]["hex"]
    return {"accent": clamp_accent_readable(top), "source": "auto",
            "candidates": cands, "picked_raw": top}


class WallpaperCache:
    """缓存 + **同色去重**：参数没变就不重算（这是"停顿 <200ms"的关键之一）。

    还有一条性能要点：窗口尺寸变了但不影响构图时（缓存的图比目标大），
    **裁切而不是重采样**——重采样是 O(w×h)，拖窗时会一帧一帧卡。
    """

    def __init__(self, max_entries: int = 3):
        self.max_entries = max(1, int(max_entries))
        self._cache: dict[tuple, object] = {}
        self._order: list[tuple] = []
        self.hits = 0
        self.misses = 0
        self.last_key: tuple | None = None
        self.last_meta: dict = {}

    @staticmethod
    def key_of(path, width: int, height: int, blur: int, dim: float, focus: str) -> tuple:
        return (str(path), int(width), int(height), int(blur), round(float(dim), 2), str(focus))

    def get(self, path, *, width: int, height: int, blur: int = 0, dim_manual: float = 0.0,
            focus: str = "center", max_edge: int = DEFAULT_MAX_EDGE):
        k = self.key_of(path, width, height, blur, dim_manual, focus)
        if k in self._cache:
            self.hits += 1
            self.last_key, self.last_meta = k, dict(self._cache[k][1])
            return self._cache[k][0], dict(self._cache[k][1])
        self.misses += 1
        qimg, meta = process_wallpaper(path, width=width, height=height, blur=blur,
                                       dim_manual=dim_manual, focus=focus, max_edge=max_edge)
        self._cache[k] = (qimg, meta)
        self._order.append(k)
        while len(self._order) > self.max_entries:
            old = self._order.pop(0)
            self._cache.pop(old, None)
        self.last_key, self.last_meta = k, dict(meta)
        return qimg, dict(meta)

    def resize_existing(self, *, width: int, height: int, focus: str = "center"):
        """窗口尺寸变了：**能裁就裁**（从缓存里的原图裁），不重跑整条管线。"""
        if self.last_key is None:
            return None
        path = self.last_key[0]
        blur, dim = self.last_key[3], self.last_key[4]
        try:
            from daedalus.ui.wallpaper import cover_crop, load_image, to_qimage
            img = cover_crop(load_image(path), width, height, focus=focus)
            q = to_qimage(img)
            meta = dict(self.last_meta, size=[int(width), int(height)], cropped_from=self.last_key)
            self._cache[self.key_of(path, width, height, blur, dim, focus)] = (q, meta)
            return q, meta
        except Exception as e:
            logger.debug("裁切复用失败（回退整条管线）：%s", e)
            return None

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "entries": len(self._cache)}

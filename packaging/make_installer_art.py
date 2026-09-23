# -*- coding: utf-8 -*-
"""生成安装器美术（与 UI 同一套设计语言：深空渐变 + 双光晕 + 点阵 + Logo 芯片 + 版本徽章）

    python packaging/make_installer_art.py            # 生成到 packaging/art/
    python packaging/make_installer_art.py --out DIR

产出（NSIS 要的格式是 **BMP**，尺寸也是 NSIS 规定的）：
    welcome.bmp   164×314  欢迎/完成页左侧横幅
    header.bmp    150×57   内页顶部小图
另出 PNG 预览（给人在文件管理器里看一眼，不参与打包）。

为什么要自己画而不是随便找图：安装器是用户看到的第一眼，它得和界面是一套语言；
而且**版本徽章必须从版本单一来源读**（写死在图里 = 又一个版本漂移点）。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

__all__ = ["make_welcome", "make_header", "main"]


def _np_cv():
    import cv2
    import numpy as np
    return cv2, np


def _base_gradient(w: int, h: int, cv2, np):
    """深空渐变 `#0B0F14 → #131B26`（左上到右下），再叠两道高斯光晕（screen 混合）。"""
    top = np.array([0x14, 0x0F, 0x0B], dtype="float32")      # BGR of #0B0F14
    bottom = np.array([0x26, 0x1B, 0x13], dtype="float32")   # BGR of #131B26
    ramp = np.linspace(0.0, 1.0, h, dtype="float32")[:, None, None]
    img = top[None, None, :] * (1 - ramp) + bottom[None, None, :] * ramp
    img = np.repeat(img, w, axis=1)
    # 双重光晕：强调色的两个同心光斑 → 模糊后 screen 叠加
    glow = np.zeros((h, w), dtype="float32")
    cv2.circle(glow, (int(w * 0.25), int(h * 0.18)), max(8, int(min(w, h) * 0.28)), 1.0, -1)
    cv2.circle(glow, (int(w * 0.80), int(h * 0.86)), max(8, int(min(w, h) * 0.22)), 0.7, -1)
    glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=max(3.0, min(w, h) * 0.06))
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")   # BGR of #4FA3E8
    torch = glow[:, :, None] * accent[None, None, :] * 0.55
    # screen 混合（1-(1-a)(1-b)）
    img = 255.0 - (255.0 - img) * (255.0 - torch) / 255.0
    return np.clip(img, 0, 255).astype("uint8")


def _dot_pattern(img, step: int, alpha: float, cv2, np):
    """14px 点阵（alpha ≈ 10 → 几乎看不见的一层纹理，作用是把纯渐变的"塑料感"打散）。"""
    if step <= 0:
        return img
    h, w = img.shape[:2]
    dots = np.zeros((h, w), dtype="float32")
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dots[y, x] = 1.0
    dots = cv2.GaussianBlur(dots, (0, 0), sigmaX=0.6)
    out = img.astype("float32") + dots[:, :, None] * 255.0 * alpha
    return np.clip(out, 0, 255).astype("uint8")


def _logo_chip(img, icon_path, *, x: int, y: int, size: int, cv2, np):
    """Logo 芯片：圆角方块 + 外发光（放大 6px + Blur(10) 后叠加）+ 图标等比贴入。"""
    h, w = img.shape[:2]
    if not pathlib.Path(icon_path).exists():
        return img, (x, y, size, size)
    icon = cv2.imdecode(np.fromfile(str(icon_path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if icon is None:
        return img, (x, y, size, size)
    if icon.ndim == 2:
        icon = cv2.cvtColor(icon, cv2.COLOR_GRAY2BGRA)
    if icon.shape[2] == 3:
        icon = cv2.cvtColor(icon, cv2.COLOR_BGR2BGRA)
    inner = int(size * 0.78)
    scale = inner / max(icon.shape[:2])
    icon = cv2.resize(icon, (max(1, int(icon.shape[1] * scale)),
                            max(1, int(icon.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    # 外发光
    halo = np.zeros((h, w), dtype="float32")
    cv2.rectangle(halo, (x - 3, y - 3), (x + size + 3, y + size + 3), 1.0, 6)
    halo = cv2.GaussianBlur(halo, (0, 0), sigmaX=10)
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")
    img = 255.0 - (255.0 - img.astype("float32")) * (255.0 - halo[:, :, None] * accent * 0.5) / 255.0
    img = np.clip(img, 0, 255).astype("uint8")
    # 芯片底：圆角 12
    chip = img.copy()
    cv2.rectangle(chip, (x, y), (x + size, y + size), (40, 30, 22), -1, cv2.LINE_AA)
    mask = np.zeros((h, w), dtype="uint8")
    cv2.rectangle(mask, (x, y), (x + size, y + size), 255, -1, cv2.LINE_AA)
    img = np.where(mask[:, :, None] > 0, chip, img)
    # 贴图标（含 alpha）
    oy, ox = y + (size - icon.shape[0]) // 2, x + (size - icon.shape[1]) // 2
    roi = img[oy:oy + icon.shape[0], ox:ox + icon.shape[1]].astype("float32")
    a = (icon[:, :, 3:4].astype("float32") / 255.0)
    bgr = icon[:, :, :3].astype("float32")
    img[oy:oy + icon.shape[0], ox:ox + icon.shape[1]] = (roi * (1 - a) + bgr * a).astype("uint8")
    return img, (x, y, size, size)


def _text(img, text: str, *, org, scale: float, color, thickness: int, cv2):
    """写字：优先用能显中文的字体文件，找不到就退回 Hershey（此时只写 ASCII）。"""
    fonts = [
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhl.ttc",
        r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\meiryo.ttc",
    ]
    for f in fonts:
        if pathlib.Path(f).exists():
            try:
                from PIL import Image, ImageDraw, ImageFont
                pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(pil)
                font = ImageFont.truetype(f, size=int(max(9, scale * 34)))
                draw.text((org[0], org[1] - int(scale * 34)), text, font=font,
                          fill=(color[2], color[1], color[0]))
                return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
            except Exception:
                break
    cv2.putText(img, text.encode("ascii", "ignore").decode() or "Daedalus", org,
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)
    return img


def make_welcome(icon_path, version: str, *, w: int = 164, h: int = 314):
    """欢迎/完成页横幅：渐变 + 点阵 + Logo 芯片 + 名称 + **版本徽章**。"""
    cv2, np = _np_cv()
    img = _base_gradient(w, h, cv2, np)
    img = _dot_pattern(img, 14, 0.04, cv2, np)
    img, (cx, cy, cs, _) = _logo_chip(img, icon_path, x=(w - 56) // 2, y=42, size=56,
                                      cv2=cv2, np=np)
    img = _text(img, "代达罗斯", org=((w - 4 * 14) // 2, cy + cs + 30), scale=0.55,
                color=(0xF0, 0xE6, 0xDA), thickness=1, cv2=cv2)
    # 版本徽章（**从版本单一来源读**，不写死在图里）
    badge = f"v{version}"
    (tw, th), _b = cv2.getTextSize(badge, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
    bx, by = (w - tw - 20) // 2, h - 26
    cv2.rectangle(img, (bx - 8, by - th - 6), (bx + tw + 8, by + 6), (46, 36, 26), -1, cv2.LINE_AA)
    img = _text(img, badge, org=(bx, by), scale=0.38, color=(0xD0, 0xB0, 0x90), thickness=1,
                cv2=cv2)
    # 底部 2px 渐隐强调线
    line = np.zeros((2, w), dtype="float32")
    for x in range(w):
        line[:, x] = (1.0 - abs(x - w / 2) / (w / 2)) ** 2
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")
    img[h - 4:h - 2] = np.clip(img[h - 4:h - 2].astype("float32")
                               + line[:, :, None] * accent * 0.7, 0, 255).astype("uint8")
    return img


def make_header(icon_path, version: str, *, w: int = 150, h: int = 57):
    """内页顶部小图：渐变 + 芯片 + 名称（版本放右上角小字）。"""
    cv2, np = _np_cv()
    img = _base_gradient(w, h, cv2, np)
    img, (cx, cy, cs, _) = _logo_chip(img, icon_path, x=10, y=8, size=40, cv2=cv2, np=np)
    img = _text(img, "代达罗斯", org=(cx + cs + 8, cy + cs // 2 + 6), scale=0.42,
                color=(0xF0, 0xE6, 0xDA), thickness=1, cv2=cv2)
    img = _text(img, f"v{version}", org=(w - 52, h - 8), scale=0.30,
                color=(0xB0, 0x9A, 0x80), thickness=1, cv2=cv2)
    return img


def license_text() -> str:
    """许可页文本：由 `docs/07-能力边界.md` **生成**（单一来源，不手抄两份）。"""
    src = ROOT / "docs" / "07-能力边界.md"
    if not src.exists():
        return ("Daedalus · 使用边界\n\n"
                "只在有权访问且能合法观察的范围内采集；\n"
                "不绕过登录、验证码、风控与签名；\n"
                "被拦截时降速 → 停止 → 报告，不做对抗升级。\n")
    body = src.read_text(encoding="utf-8")
    out = []
    for line in body.splitlines():
        s = line.rstrip()
        if s.startswith("#"):
            out.append(s.lstrip("# ").strip().upper() if s.startswith("## ") else
                       s.lstrip("# ").strip())
            out.append("")
        elif s.startswith(("- ", "* ", "> ")):
            out.append("  " + s[2:].strip())
        else:
            out.append(s)
    return "\n".join(out).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成安装器美术（BMP + 预览 PNG）")
    ap.add_argument("--out", default=str(ROOT / "packaging" / "art"))
    args = ap.parse_args(argv)
    from daedalus import VERSION
    cv2, np = _np_cv()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    icon = ROOT / "assets" / "icon_256.png"
    welcome = make_welcome(str(icon), VERSION)
    header = make_header(str(icon), VERSION)
    made = []
    for name, img in (("welcome", welcome), ("header", header)):
        bmp = out / f"{name}.bmp"
        okk, buf = cv2.imencode(".bmp", img)
        if not okk:
            raise SystemExit(f"编码 {name}.bmp 失败")
        bmp.write_bytes(buf.tobytes())
        png = out / f"{name}.png"                     # 预览用（不参与打包）
        (png).write_bytes(cv2.imencode(".png", img)[1].tobytes())
        made.append(f"{bmp.name} {img.shape[1]}×{img.shape[0]}")
    lic = out / "license.txt"
    lic.write_text(license_text(), encoding="utf-8")
    made.append(f"license.txt（由 docs/07-能力边界.md 生成，{lic.stat().st_size} 字节）")
    print("安装器美术已生成：")
    for m in made:
        print(f"  {m}")
    print(f"输出目录：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

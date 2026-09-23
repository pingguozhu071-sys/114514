# -*- coding: utf-8 -*-
"""设计系统内核：**主题令牌 + 玻璃层生成器 + 强调色**

三层材质（不可省）：
    ① **底图层**：用户图（可关 → 回退主题纯色）
    ② **玻璃层**：卡片/面板（竖向渐变 + 1px 中性描边 + 12px 圆角），透明度由**单一参数**
       `panel_alpha` 控制。它的作用不是好看，是**可读性基础设施**——把底图局部对比度
       压到统一范围，控件才有稳定的对比度基线。
    ③ **控件层**：Fluent 组件库（不逐控件自定义样式）

**铁律：同页所有卡片由同一个函数生成**（`panel_qss(alpha, light, radius)`）——
一旦某张卡片被单独调参，"统一感"就破了，而且以后没人说得清为什么它不一样。
"专家模式"允许单卡覆写，但默认关闭且明确提示会破坏统一感。

描边**固定**：深 `rgba(255,255,255,22)` / 浅 `rgba(0,0,0,14)`，**不随 alpha 变**。
（原因：描边是"卡片边界"的语义，不是装饰；跟着 alpha 变会让边界在高透明时消失。）

**状态色不参与强调色联动**：成功/警告/错误/信息是语义色，必须稳定可预期。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ACCENT_PRESETS", "ThemeError", "tokens", "panel_qss", "card_qss", "status_colors",
           "accent_presets", "clamp_accent_readable", "hex_of", "mix_hex", "ReadableText"]


class ThemeError(ValueError):
    """主题参数不合法（越界/未知预设）。**拒绝静默纠正**：参数错就说错，别偷偷改。"""


# 4 档强调色（默认档在前）
ACCENT_PRESETS: tuple[tuple[str, str], ...] = (
    ("海蓝", "#4FA3E8"),
    ("星紫", "#9B7EDE"),
    ("青碧", "#2FC6C6"),
    ("樱粉", "#F0708A"),
)

# 语义色（**不参与**强调色联动）
_STATUS_DARK = {"ok": "#4CC38A", "warn": "#E0A33E", "error": "#E4695E", "info": "#5AA9E6",
                "muted": "#9AA4B2"}
_STATUS_LIGHT = {"ok": "#1F8A55", "warn": "#A96A00", "error": "#C0392B", "info": "#1B6FB8",
                 "muted": "#5B6672"}

# 底图蒙层自动档：公式写死（可复核），手动只能**加暗**（取 max）
_DIM_AUTO_MIN, _DIM_AUTO_MAX = 8.0, 45.0


def status_colors(light: bool = False) -> dict:
    return dict(_STATUS_LIGHT if light else _STATUS_DARK)


# ── 字体：**按语言选**（不许"中文界面用日文字体"这类蠢问题）────────────
# 为什么必须显式指定：实测（offscreen 渲染截图）发现，Qt 找不到字体时**整屏文字变方块**——
# 之前我们完全依赖系统默认字体，等于把"能不能显示"交给运气。这里按语言给一条**有优先级的
# 字体链**，Qt 会挑第一个装了的：中文→微软雅黑，日文→Meiryo，英文→Segoe UI。
_FONT_CHAIN = {
    "zh-CN": ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑", "SimHei",
              "Noto Sans CJK SC", "Segoe UI"),
    "ja-JP": ("Meiryo UI", "Meiryo", "メイリオ", "Yu Gothic UI", "MS Gothic",
              "Noto Sans CJK JP", "Segoe UI"),
    "en-US": ("Segoe UI", "Helvetica Neue", "Noto Sans", "DejaVu Sans"),
}
_MONO_CHAIN = ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New", "monospace")


def font_chain(locale: str = "zh-CN") -> tuple[str, ...]:
    """该语言的界面字体候选链（按优先级）。未知语言回退 en-US 的链。"""
    return _FONT_CHAIN.get(str(locale), _FONT_CHAIN["en-US"])


def mono_chain() -> tuple[str, ...]:
    """等宽字体链（日志视图用）。"""
    return _MONO_CHAIN


def font_qss(locale: str = "zh-CN", *, size_pt: float = 10.0) -> str:
    """全局字体 QSS：把候选链写进去（Qt 自己挑装了的那一个）。"""
    fams = ", ".join(f'"{f}"' if " " in f else f for f in font_chain(locale))
    mono = ", ".join(f'"{f}"' if " " in f else f for f in mono_chain())
    return (f"* {{ font-family: {fams}; font-size: {float(size_pt):.1f}pt; }}\n"
            f"#logView, #logView * {{ font-family: {mono}; }}\n")


def accent_presets() -> list[dict]:
    return [{"name": n, "hex": h} for n, h in ACCENT_PRESETS]


# ── 颜色小工具（零依赖，纯函数便于测）──────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(_clamp(c, 0, 255)) for c in rgb))


def parse_hex(text: str) -> tuple[int, int, int]:
    t = str(text or "").strip().lstrip("#")
    if len(t) == 3:
        t = "".join(ch * 2 for ch in t)
    if len(t) != 6:
        raise ThemeError(f"颜色格式不对：{text!r}（要 #RRGGBB）")
    try:
        return int(t[0:2], 16), int(t[2:4], 16), int(t[4:6], 16)
    except ValueError as e:
        raise ThemeError(f"颜色不是十六进制：{text!r}") from e


def _hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60.0, (0.0 if mx == 0 else d / mx), mx


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    h = (h % 360.0) / 60.0
    i = int(h)
    f = h - i
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    table = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]
    r, g, b = table[i % 6]
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def clamp_accent_readable(hex_color: str, *, s_max: float = 0.9,
                          v_range: tuple[float, float] = (0.45, 0.85)) -> str:
    """可读性钳制：**饱和度过高的颜色做强调色会刺眼/发糊**，亮度也要在可读区间。

    规则（写死，可复核）：S ≤ `s_max`；V 钳到 `v_range`；色相不动（保住"用户选的颜色像它自己"）。
    """
    h, s, v = _hsv(parse_hex(hex_color))
    s2 = min(s, float(s_max))
    v2 = _clamp(v, v_range[0], v_range[1])
    return hex_of(_hsv_to_rgb(h, s2, v2))


def mix_hex(a: str, b: str, t: float) -> str:
    """线性混合（t=0 取 a，t=1 取 b）。"""
    ra, rb = parse_hex(a), parse_hex(b)
    t = _clamp(float(t), 0.0, 1.0)
    return hex_of(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def dim_auto(brightness_mean: float) -> float:
    """蒙层自动档：`clamp((亮度均值-128)*0.35+20, 8, 45)`（%）。

    亮图自动加暗、暗图少加——目的是让**文字对比度稳定**，而不是"好看"。
    手动档只能在此基础上**更暗**（取 max），不能把它调亮回来（那会让白字糊在白底上）。
    """
    return round(_clamp((float(brightness_mean) - 128.0) * 0.35 + 20.0,
                        _DIM_AUTO_MIN, _DIM_AUTO_MAX), 2)


def resolve_dim(manual: float, brightness_mean: float) -> float:
    """手动与自动取 max（手动只能加暗）。"""
    return round(max(_clamp(float(manual), 0.0, 60.0), dim_auto(brightness_mean)), 2)


@dataclass
class ReadableText:
    """在给定底色上仍可读的文字色（对比度不够就换深/浅色）。"""

    on: str = "#FFFFFF"
    other: str = "#0B0F14"

    def pick(self, bg_hex: str, threshold: float = 0.55) -> str:
        r, g, b = parse_hex(bg_hex)
        lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
        return self.on if lum < threshold else self.other


# ── 主题令牌 ─────────────────────────────────────────────────────
@dataclass
class Tokens:
    """一整套界面令牌（所有尺寸/颜色/时长都从这里来，别在控件里写魔法数字）。"""

    light: bool = False
    accent: str = "#4FA3E8"
    panel_alpha: int = 65              # 单一参数控制玻璃层（40–95）
    radius: int = 12
    font_pt: float = 10.0              # 中文**必须 ≥10pt**（9pt 走点阵渲染发糊）
    density: str = "standard"          # compact | standard | relaxed
    animations: bool = True
    fade_ms: int = 450
    debounce_ms: int = 250
    fps_cap: int = 30
    signature: bool = True
    expert_mode: bool = False

    # 密度 → 间距表（外边距 / 卡片间距 / 卡内边距 / 卡内间距）
    _DENSITY: dict = field(default_factory=lambda: {
        "compact": {"margin": 18, "gap": 10, "pad": (12, 10), "inner": (3, 7, 9)},
        "standard": {"margin": 28, "gap": 14, "pad": (18, 14), "inner": (4, 10, 12)},
        "relaxed": {"margin": 36, "gap": 20, "pad": (22, 18), "inner": (6, 12, 16)},
    })

    # 底图参数
    wallpaper: str = ""                # 路径（空 = 关，回退主题纯色）
    blur: int = 0                      # 0–30 → 核 k=2b+1
    dim_manual: float = 0.0            # 0–60（**手动只能加暗**）
    focus: str = "center"              # 九宫格焦点
    downsample_max: int = 2560         # 长边上限
    # 界面语言（**已解析过的**：设置 > 安装器选择 > 系统 > en-US，见 `ui/i18n.py`）。
    # 令牌带着它，是为了保证"语言与外观在同一次重刷里一致"——不许出现
    # "标题换了日文、导航还是中文"这种半截状态。
    locale: str = "zh-CN"

    def spacing(self) -> dict:
        d = self._DENSITY.get(str(self.density)) or self._DENSITY["standard"]
        out = dict(d)
        out["pad_x"], out["pad_y"] = d["pad"]
        out["inner_gap"], out["inner_top"], out["inner_bottom"] = d["inner"]
        # 铁律：**卡片间距必须大于卡内最大间距**，否则分组感会消失
        if out["gap"] <= max(out["inner"]):
            out["gap"] = max(out["inner"]) + 4
        return out

    def bg_solid(self) -> str:
        return "#F5F6F8" if self.light else "#0B0F14"

    def card_alpha(self) -> int:
        return int(_clamp(self.panel_alpha, 40, 95))

    def validate(self) -> "Tokens":
        if not (40 <= int(self.panel_alpha) <= 95):
            raise ThemeError(f"panel_alpha 越界：{self.panel_alpha}（合法 40–95）")
        if float(self.font_pt) < 10.0:
            raise ThemeError(f"界面字号 {self.font_pt}pt < 10pt（中文会发糊）")
        if self.density not in self._DENSITY:
            raise ThemeError(f"未知密度档：{self.density}")
        if not (0 <= int(self.blur) <= 30):
            raise ThemeError(f"模糊半径越界：{self.blur}（合法 0–30）")
        if not (0 <= float(self.dim_manual) <= 60):
            raise ThemeError(f"蒙层越界：{self.dim_manual}（合法 0–60）")
        parse_hex(self.accent)                     # 颜色格式不对就抛
        return self


def tokens(**kw) -> Tokens:
    """构造并校验一组令牌（参数错直接抛，**不静默纠正**）。"""
    t = Tokens(**kw)
    return t.validate()


# ── 玻璃层 QSS 生成器（**同页所有卡片都走这一个函数**）──────────────
def panel_qss(alpha: int | None = None, light: bool = False, radius: int = 12,
              accent: str = "#4FA3E8", object_name: str = "panel") -> str:
    """生成玻璃层样式。

    * 竖向渐变（上亮下暗一点点）+ 1px **固定**描边 + 圆角；
    * 透明度由**唯一参数** `alpha`（40–95）控制；
    * 深色描边 `rgba(255,255,255,22)`／浅色 `rgba(0,0,0,14)`——**不随 alpha 变**。
    """
    a = int(_clamp(95 if alpha is None else alpha, 40, 95)) / 100.0
    if light:
        top = f"rgba(255,255,255,{a:.3f})"
        bottom = f"rgba(246,247,250,{min(1.0, a + 0.03):.3f})"
        border = "rgba(0,0,0,14)"
        text = "#12161C"
    else:
        top = f"rgba(255,255,255,{a * 0.10:.3f})"
        bottom = f"rgba(12,16,22,{min(1.0, a / 100.0):.3f})"
        border = "rgba(255,255,255,22)"
        text = "#EAF0F6"
    sel = mix_hex(accent, "#FFFFFF", 0.55 if light else 0.25)
    return f"""
#{object_name} {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {top}, stop:1 {bottom});
    border: 1px solid {border};
    border-radius: {int(radius)}px;
    color: {text};
}}
#{object_name}[selected="true"] {{
    border: 1px solid {sel};
}}
"""


def card_qss(t: Tokens, object_name: str = "card") -> str:
    """卡片样式 = 同一生成器 + 令牌（**别在这里另写一套颜色**）。"""
    return panel_qss(t.card_alpha(), light=t.light, radius=t.radius, accent=t.accent,
                     object_name=object_name)

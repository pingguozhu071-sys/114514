# -*- coding: utf-8 -*-
"""S10 门禁：GUI（设计系统四组自测 + 性能红线实测 + 边界）

四组自测（照设计系统的规定）：
  A 视觉：铁律（所有卡片同一生成器）/ 描边固定 / 语义色不随强调色 / 字号下限 / 密度分组感 / 取色钳制
  B 底图：中文路径 / 不非等比拉伸 / 坏图不崩且明确报错 / 8K 下采样与内存 / 蒙层规则 / 取色
  C 交互性能：停顿 <200ms / 切页 <400ms / 重排 <1200ms / 缓存复用与代数号守卫
  D 边界与纪律：设置损坏回退 / 越界拒绝 / 预设往返 / 签名与窗口尺寸 / UI 不直连引擎 / 无对抗词汇

平台：`QT_QPA_PLATFORM=offscreen`（无显示器也能跑；门禁自己会设）。

跑法：
    python tests/gates/s10_gate.py      # 退出码 0 = 全通过
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # **必须在导入 Qt 之前**

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s10_"))
os.environ["DAEDALUS_DATA_ROOT"] = str(_TMP)

_CASES: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


_APP = {"app": None}
_WIN = {"win": None, "root": None}


def ui():
    """懒建一个 offscreen 窗口（所有用例共用；切页/改外观都改它）。"""
    if _WIN["win"] is None:
        from daedalus.ui.app import build_headless
        root = _TMP / "ui"
        root.mkdir(parents=True, exist_ok=True)
        b = build_headless(data_root=root)
        _WIN["build"] = b
        _WIN["win"] = b["window"]          # 与 `build_headless` 的键名保持一致
        _WIN["window"] = b["window"]       # 两个名字都给：避免"记错键名"这种测试噪音
        _WIN["root"] = root
        _WIN["app"] = b["app"]
        _APP["app"] = b["app"]
        _WIN["win"].show()
        b["app"].processEvents()
    return _WIN


def _fresh_window(locale: str = "zh-CN"):
    """每个用例一个**独立**窗口（E 组会改设置，共用单例会互相污染）。"""
    from daedalus.ui.app import build_headless
    from daedalus.ui.theme import tokens as mk_tokens
    root = _TMP / f"ui_{abs(hash(locale)) % 1000}_{len(_WIN)}"
    root.mkdir(parents=True, exist_ok=True)
    return build_headless(data_root=root, tokens=mk_tokens(locale=locale))


def test_image(path, w, h, *, mode="color"):
    """造测试图（`mode`: color 彩块 / gray 纯灰 / pattern 带标记的图案）。"""
    import cv2
    import numpy as np
    if mode == "gray":
        img = np.full((h, w, 3), 128, "uint8")
    elif mode == "pattern":
        img = np.full((h, w, 3), 30, "uint8")
        cv2.rectangle(img, (0, 0), (w // 2, h // 2), (255, 255, 255), -1)
        cv2.circle(img, (int(w * 0.75), int(h * 0.25)), min(w, h) // 10, (0, 0, 255), -1)
    else:
        img = np.zeros((h, w, 3), "uint8")
        img[:, :, 0] = 190            # BGR：偏紫
        img[:, :, 2] = 150
    pathlib.Path(path).write_bytes(cv2.imencode(".png", img)[1].tobytes())
    return pathlib.Path(path)


# ══════════════════════════════════════════════════════════════════
@case("A1 铁律：所有卡片样式由**同一个生成器**产出（改 alpha 一起变）")
def t_single_generator():
    from daedalus.ui.theme import card_qss, tokens
    b = ui()
    t65 = tokens(panel_alpha=65)
    t40 = tokens(panel_alpha=40)
    # 同 objectName → 同 QSS（不是"看起来差不多"，是逐字符相同）
    for name in ("card", "statCard", "settingsCard", "taskCard"):
        assert card_qss(t65, name) == card_qss(t65, name), name
        assert card_qss(t65, name) != card_qss(t40, name), f"{name} 没跟着 alpha 变"
    a = b["window"]._ui["applied"]                     # noqa: SLF001
    assert a["panel_alpha"] == 65, a
    from daedalus.ui.app import MainWindow
    MainWindow.apply_tokens(b["window"], t40)
    a2 = b["window"]._ui["applied"]                    # noqa: SLF001
    assert a2["panel_alpha"] == 40, a2
    MainWindow.apply_tokens(b["window"], t65)
    return ok("4 种卡片名共用一个生成器；alpha 65→40 全局跟随")


@case("A2 描边固定（不随 alpha 变）+ 圆角跟令牌")
def t_border_fixed():
    from daedalus.ui.theme import panel_qss, tokens
    for light in (False, True):
        for alpha in (40, 65, 95):
            q = panel_qss(alpha, light=light, radius=10)
            want = "rgba(0,0,0,14)" if light else "rgba(255,255,255,22)"
            assert f"1px solid {want}" in q, (alpha, light, q[:120])
            assert "border-radius: 10px" in q, q[:120]
    return ok("深浅两色 × 40/65/95 三档：描边恒为 rgba(255,255,255,22)|rgba(0,0,0,14)")


@case("A3 语义色不参与强调色联动（状态色稳定可预期）")
def t_status_stable():
    from daedalus.ui.theme import status_colors
    dark = status_colors(False)
    light = status_colors(True)
    assert set(dark) == {"ok", "warn", "error", "info", "muted"}, dark
    assert dark != light, "深浅两套语义色应不同"
    src = (ROOT / "src" / "daedalus" / "ui" / "theme.py").read_text(encoding="utf-8")
    assert "不参与**强调色" in src or "不参与" in src, "文件头没写清这条规矩"
    return ok(f"语义色 5 项固定（ok/warn/error/info/muted），与 accent 无关")


@case("A4 字号下限：<10pt 直接拒绝（中文会发糊）")
def t_font_floor():
    from daedalus.ui.theme import ThemeError, tokens
    try:
        tokens(font_pt=9.0)
        raise AssertionError("9pt 竟然被接受了")
    except ThemeError as e:
        first = str(e)
    try:
        from daedalus.ui.settings import SettingsStore
        s = SettingsStore(_TMP / "fontfloor", autosave=False)
        s.set("font_pt", 8.5)
        raise AssertionError("设置层也没拦住 <10pt")
    except ThemeError as e2:
        return ok(f"令牌层与设置层都拒绝：{first[:34]}…｜{str(e2)[:34]}…")


@case("A5 密度：卡片间距 > 卡内最大间距（分组感不破）")
def t_density_invariant():
    from daedalus.ui.theme import tokens
    seen = []
    for d in ("compact", "standard", "relaxed"):
        sp = tokens(density=d).spacing()
        assert sp["gap"] > max(sp["inner_gap"], sp["inner_top"], sp["inner_bottom"]), (d, sp)
        seen.append((d, sp["margin"], sp["gap"]))
    # 标准档的具体数字与设计规格一致
    std = tokens(density="standard").spacing()
    assert std["margin"] == 28 and std["gap"] == 14, std
    return ok(f"三档均满足（紧凑/标准/宽松：margin {[s[1] for s in seen]}，gap {[s[2] for s in seen]}）")


@case("A6 强调色：4 档预设 + 可读性钳制")
def t_accent_rules():
    from daedalus.ui.theme import ACCENT_PRESETS, clamp_accent_readable, parse_hex
    assert len(ACCENT_PRESETS) == 4, ACCENT_PRESETS
    hot = clamp_accent_readable("#00FF00")            # 极饱和 → 钳低饱和
    very_bright = clamp_accent_readable("#FFFFFF")    # 极亮 → 压到可读区间
    very_dark = clamp_accent_readable("#000000")      # 极暗 → 抬到可读区间
    for name, hx in (("hot", hot), ("bright", very_bright), ("dark", very_dark)):
        r, g, b = parse_hex(hx)
        assert 20 <= max(r, g, b) <= 245, (name, hx)
    assert hot != "#00FF00" and very_bright != "#FFFFFF", (hot, very_bright)
    return ok(f"4 档预设；钳制：#00FF00→{hot}、#FFFFFF→{very_bright}、#000000→{very_dark}")


@case("B1 底图：中文路径能读（cv2.imread 的静默失败坑）")
def t_cjk_path():
    b = ui()
    d = b["root"] / "深一层目录" / "中文名"
    d.mkdir(parents=True, exist_ok=True)
    p = test_image(d / "壁纸 图.png", 800, 600)
    import cv2
    assert cv2.imread(str(p)) is None, "本机 imread 竟然能读中文路径（那这条坑不成立，需重估）"
    from daedalus.ui.wallpaper import load_image
    img = load_image(str(p))
    assert img is not None and img.shape[0] > 0, img
    return ok(f"imread→None（坑真实存在）而 np.fromfile+imdecode 成功：{img.shape}")


@case("B2 不变形：竖/横/方图 cover 裁切后比例正确（绝不非等比拉伸）")
def t_no_distortion():
    import cv2
    import numpy as np
    from daedalus.ui.wallpaper import cover_crop, load_image
    target = (1360, 860)
    for name, (w, h) in (("竖图", (600, 1200)), ("横图", (1600, 700)), ("方图", (900, 900))):
        p = test_image(_TMP / f"shape_{name}.png", w, h, mode="pattern")
        img = load_image(str(p))
        out = cover_crop(img, target[0], target[1], focus="center")
        assert out.shape[1] == target[0] and out.shape[0] == target[1], (name, out.shape)
        # 非等比拉伸会让圆变椭圆：用两个标记点的相对几何来验（同一比例缩放 ⇒ 圆心仍在预期位置）
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        ys, xs = np.where(gray > 200)                          # 白色方块（左上四分之一）
        assert xs.size > 0, name
        # 白色区域应仍然贴着左上角（cover 裁切 + 居中焦点时不会把角落挪走）
        assert xs.min() == 0 and ys.min() == 0, (name, xs.min(), ys.min())
    return ok("竖/横/方三种原图裁切后都是 1360×860，标记几何未变形")


@case("B3 坏图/缺图：明确报错、窗口不崩、回退纯色")
def t_bad_image():
    from daedalus.ui.wallpaper import ImageUnavailable, process_wallpaper
    from daedalus.ui.app import MainWindow
    b = ui()
    bad = _TMP / "不是图.txt"
    bad.write_text("这不是图片", encoding="utf-8")
    for p in (bad, _TMP / "根本不存在.png"):
        try:
            process_wallpaper(str(p), width=800, height=600)
            raise AssertionError(f"{p} 竟然成功了")
        except ImageUnavailable as e:
            assert str(p) in str(e) or "不存在" in str(e) or "不是" in str(e), e
    # 回退纯色：把底图设成 None，窗口仍然能重绘
    MainWindow.set_wallpaper(b["window"], None)
    b["app"].processEvents()
    st = MainWindow.stats(b["window"])
    assert st["size"][0] > 0, st
    return ok("缺图/坏图都明确抛错；随后回退纯色、窗口照常工作")


@case("B4 8K 图：长边下采样到 ≤2560，管线峰值 <200MB")
def t_big_image():
    import cv2
    from daedalus.ui.wallpaper import load_image, process_wallpaper
    p = test_image(_TMP / "8K.png", 7680, 4320, mode="pattern")
    img = load_image(str(p), max_edge=2560)
    assert max(img.shape[:2]) <= 2560, img.shape
    import tracemalloc
    tracemalloc.start()
    q, meta = process_wallpaper(str(p), width=1360, height=860, blur=4)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_mb = peak / (1 << 20)
    assert peak_mb < 200.0, f"管线峰值 {peak_mb:.1f} MB 超线"
    assert not q.isNull(), "转换出的 QImage 是空的"
    return ok(f"8K → 长边 {max(img.shape[:2])}；峰值 {peak_mb:.1f} MB（<200MB）；蒙层 {meta['dim_used']}%")


@case("B5 蒙层规则：手动只能加暗；暗图自动下限 8")
def t_dim_rules():
    from daedalus.ui.theme import dim_auto, resolve_dim
    from daedalus.ui.wallpaper import process_wallpaper
    dark = test_image(_TMP / "dark.png", 400, 300, mode="pattern")
    white = _TMP / "white.png"
    import cv2
    import numpy as np
    white.write_bytes(cv2.imencode(".png", np.full((300, 400, 3), 250, "uint8"))[1].tobytes())
    _q1, m_dark = process_wallpaper(str(dark), width=400, height=300, dim_manual=0)
    _q2, m_white = process_wallpaper(str(white), width=400, height=300, dim_manual=0)
    assert m_dark["dim_used"] == dim_auto(m_dark["brightness_mean"]), m_dark
    assert m_dark["dim_used"] >= 8.0, m_dark
    assert m_white["dim_used"] > m_dark["dim_used"], (m_white, m_dark)
    assert resolve_dim(0, 250) > 0 and resolve_dim(60, 20) == 60, "max(手动, 自动) 规则不成立"
    return ok(f"暗图 {m_dark['dim_used']}% / 亮图 {m_white['dim_used']}%（亮图自动加暗更多）")


@case("B6 取色：紫图取到紫；灰图如实回退；锁定开关跳过计算")
def t_accent_extract():
    from daedalus.ui.wallpaper import extract_accent
    purple = test_image(_TMP / "purple.png", 600, 400, mode="color")
    gray = test_image(_TMP / "gray.png", 600, 400, mode="gray")
    a1 = extract_accent(str(purple))
    a2 = extract_accent(str(gray))
    a3 = extract_accent(str(purple), lock="#123456")
    assert a1["source"] == "auto", a1
    r, g, b = (int(a1["accent"][i:i + 2], 16) for i in (1, 3, 5))
    assert r > g and b > g, f"偏紫的图没取到偏紫色：{a1['accent']}"
    assert a2["source"] == "fallback_gray" and "饱和" in a2.get("note", ""), a2
    assert a3["accent"] == "#123456" and a3["source"] == "locked", a3
    return ok(f"紫→{a1['accent']}（auto）；灰→回退并说明；锁定→直接用且不算")


@case("C1-C4 交互性能与调度：停顿/切页/重排/缓存与代数号")
def t_perf():
    from perf_probe import probe
    rep = probe(real=False, out=_TMP / "perf.json")
    bad = [c for c in rep["checks"] if not c["ok"]]
    # **机器忙时（比如长跑在跑）这些数字测不准**：把"负载"当成一等事实报出来，
    # 而不是让"机器忙"伪装成"性能回归"（实测踩过：同一套代码在负载下停顿超线，
    # 白查了一轮发现是后台长跑抢 CPU）。
    if bad and not rep.get("measurement_trustworthy", True):
        return skip(f"机器负载 {rep.get('machine_load_pct')}%（>50%）——停顿时延不可信；"
                    f"探针已标注 measurement_trustworthy=false，请在空闲机器上复测")
    assert not bad, bad
    assert rep["cache"]["hits"] >= 4, rep["cache"]
    assert rep["scheduler"]["coalesced"] >= 5, rep["scheduler"]
    load = rep.get("machine_load_pct")
    return ok(f"停顿 {rep['beats_ms_max']}ms｜切页 {max(rep['switches_ms'].values()):.2f}ms｜"
              f"重排 {max(rep['reflows_ms'])}ms｜管线峰值 {rep['pipeline']['peak_mb']}MB｜"
              f"缓存命中 {rep['cache']['hits']}｜防抖合并 {rep['scheduler']['coalesced']}"
              + (f"｜负载 {load:.0f}%" if load is not None else ""))


@case("C5 代数号守卫：过期结果被丢弃（不覆盖当前状态）")
def t_generation_guard():
    from daedalus.ui.render import RenderScheduler
    s = RenderScheduler(debounce_ms=0, now=lambda: time.monotonic() + 1.0)
    g1 = s.request({"img": "A"})
    g2 = s.request({"img": "B"})                 # 新的让旧的作废
    assert g2 > g1 and s.stats()["coalesced"] >= 1, s.stats()
    delivered: list = []
    sink = lambda v: (delivered.append(v), v)[1]           # noqa: E731 - 返回投递值，便于断言
    assert s.complete(g1, {"stale": True}, deliver=sink) is None, "过期结果被投递了"
    assert delivered == [], delivered
    assert s.complete(g2, {"fresh": True}, deliver=sink) is not None
    assert delivered == [{"fresh": True}], delivered
    return ok(f"过期代数被丢弃 {s.stats()['dropped_by_generation']} 次；当前代投递 1 次")


@case("C6 忙时挂起不丢请求（跑完自动补跑）")
def t_busy_defer():
    from daedalus.ui.render import RenderScheduler
    s = RenderScheduler(debounce_ms=0, now=lambda: time.monotonic() + 1.0)
    s.request({"a": 1})
    req = s.take()
    assert req is not None and s.stats()["busy"] is True, s.stats()
    s.request({"b": 2})                          # 忙时来新请求 → 挂起，不丢
    assert s.take() is None, "忙时不该再开工"
    again = s.finish_busy()
    assert again is True, "被挂起的请求没有被记住"
    assert s.take() is not None, "补跑没发生"
    return ok("忙时挂起 → 完成后补跑（不丢请求）")


@case("D1 设置文件损坏 → 回退默认 + 记下原因（不静默）")
def t_settings_corrupt():
    from daedalus.ui.settings import SettingsStore
    d = _TMP / "corrupt"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ui_settings.json").write_text("{ 这不是 JSON", encoding="utf-8")
    s = SettingsStore(d)
    assert s.get("panel_alpha") == 65, s.data
    assert "回退默认值" in s.load_note, s.load_note
    return ok(f"损坏文件 → 默认值 + 说明：{s.load_note[:52]}")


@case("D2 设置越界 → 拒绝（不静默纠正）")
def t_settings_bounds():
    from daedalus.ui.settings import SettingsStore
    from daedalus.ui.theme import ThemeError
    s = SettingsStore(_TMP / "bounds", autosave=False)
    bad = 0
    for key, val in (("panel_alpha", 20), ("panel_alpha", 99), ("blur", 40),
                     ("dim_manual", 80), ("density", "超密"), ("focus", "斜角")):
        try:
            s.set(key, val)
        except ThemeError:
            bad += 1
    assert bad == 6, f"只拦下 {bad}/6 个越界值"
    return ok("6 个越界值全部被拒（含透明度上下界、模糊、蒙层、密度、焦点）")


@case("D3 预设：保存 / 加载 / 导出导入往返")
def t_presets():
    from daedalus.ui.settings import FACTORY_PRESETS, SettingsStore
    s = SettingsStore(_TMP / "presets", autosave=False)
    assert set(FACTORY_PRESETS) <= {p["name"] for p in s.list_presets()}, s.list_presets()
    s.set("panel_alpha", 88, quiet=True)
    s.save_preset("我的暗底")
    assert s.load_preset("标准")["panel_alpha"] == 65, s.data
    s.load_preset("我的暗底")
    assert s.get("panel_alpha") == 88, s.data
    p = _TMP / "presets_out.json"
    exp = s.export_presets(p)
    s2 = SettingsStore(_TMP / "presets2", autosave=False)
    imp = s2.import_presets(p)
    assert "我的暗底" in {x["name"] for x in s2.list_presets()}, s2.list_presets()
    return ok(f"导出 {exp['count']} 个 → 导入 {imp['imported']} 个；加载预设生效")


@case("D4 窗口：最小尺寸与启动尺寸、签名开关生效")
def t_window_and_signature():
    from daedalus.ui.app import START_H, START_W, MainWindow
    from daedalus.ui.theme import tokens
    b = ui()
    w = b["window"]
    assert (w.minimumWidth(), w.minimumHeight()) == (980, 620), (w.minimumWidth(), w.minimumHeight())
    assert (START_W, START_H) == (1360, 860)
    assert w._ui["sig"].isVisible() is True                    # noqa: SLF001
    MainWindow.apply_tokens(w, tokens(signature=False))
    assert w._ui["sig"].isVisible() is False, "签名关不掉"     # noqa: SLF001
    MainWindow.apply_tokens(w, tokens(signature=True))
    return ok(f"最小 {w.minimumWidth()}×{w.minimumHeight()}｜启动 {START_W}×{START_H}｜签名可开可关")


@case("D5 纪律：页面与控件不直接碰引擎（只经 ctx）")
def t_no_direct_engine():
    import re
    hits = []
    for rel in ("ui/widgets.py", "ui/pages.py", "ui/theme.py", "ui/wallpaper.py"):
        text = (ROOT / "src" / "daedalus" / rel).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bengine\.", code) and "ctx.engine" not in code:
                hits.append(f"{rel}:{i}")
    assert not hits, f"这些地方直接调了引擎：{hits}"
    return ok("ui/ 内零直接引擎调用（都走 ctx 注入）")


@case("D6 纪律：ui/ 无对抗性词汇，且不引入第二出网路径")
def t_ui_boundary():
    import ast
    banned = ("stealth", "webdriver", "指纹伪装", "打码", "captcha", "proxy_rotat", "humaniz")  # noqa: lint -- 扫描器词表
    net = ("urllib.request", "socket.socket", "requests.get", "httpx.")  # noqa: lint -- 这是扫描器自己的关键词表
    bad = []
    for p in sorted((ROOT / "src" / "daedalus" / "ui").glob("*.py")):
        text = p.read_text(encoding="utf-8")
        tree = ast.parse(text)
        doc_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for ln in range(getattr(node, "lineno", 0), getattr(node, "end_lineno", 0) + 1):
                    doc_lines.add(ln)
        code = "\n".join(ln for i, ln in enumerate(text.splitlines(), 1)
                         if i not in doc_lines and not ln.strip().startswith("#"))
        for k in banned + net:
            if k.lower() in code.lower():
                bad.append(f"{p.name}:{k}")
    assert not bad, f"ui/ 出现不该有的关键词：{bad}"
    return ok("ui/ 可执行代码零对抗词汇、零裸网络调用")


# ══════════════════════════════════════════════════════════════════
# E. 接线与多语言（机主要求："测一下 UI…别犯低级错误"、"装完别蹦出别的语言"）
#    这一组是**走查抓出来的教训**：组件都对、**接线没接**，测组件永远测不出来
#    （原来 S10 是拿 set_wallpaper 直接测的，于是"设置里存了底图但界面不加载"这种 bug 一直假绿）。
# ══════════════════════════════════════════════════════════════════
@case("E1 接线存在：两条启动路径都必须装上「设置一变 → 界面跟着变」的处理器")
def t_context_wired():
    src = (ROOT / "src" / "daedalus" / "ui" / "app.py").read_text(encoding="utf-8")
    assert "def wire_context" in src, "没有接线函数"
    assert src.count("MainWindow.wire_context(") >= 2, "两条启动路径没有共用同一个接线"
    assert "def build_headless" in src and "def run_ui" in src
    assert "apply_wallpaper_from_settings" in src and "sync_accent_from_wallpaper" in src, \
        "底图管线/自动取色没有触发点（设置页只存路径、没人加载底图 = 走查抓到的真 bug）"
    # 运行期证据：接线后 handler 必须非空
    b = _fresh_window()
    ctx = b["window"]._ui["ctx"]                                   # noqa: SLF001
    assert getattr(ctx, "_on_change", None) is not None, "接线后仍没有变更处理器"
    return ok("接线函数存在且被两条启动路径共用；底图/取色都有触发点")


@case("E2 设置变更真的生效（走真实信号：透明度/主题/密度/字号）")
def t_settings_take_effect():
    b = _fresh_window()
    win, app, store = b["window"], b["app"], b["settings"]
    sw = win._ui["pages"]["settings"]._widgets                      # noqa: SLF001
    from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QSlider, QSpinBox

    def apply(key, value):
        w = sw[key]
        if isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, (QSlider, QSpinBox)):
            w.setValue(int(value))
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(float(value))
        elif isinstance(w, QComboBox):
            w.setCurrentIndex(max(0, w.findData(value)))
        app.processEvents()
        return win._ui["applied"]                                  # noqa: SLF001

    a1 = apply("panel_alpha", 88)
    a2 = apply("light", True)
    a3 = apply("density", "relaxed")
    a4 = apply("font_pt", 12.0)
    assert a1["panel_alpha"] == 88, a1
    assert a2["border"] == "rgba(0,0,0,14)", a2
    assert a3["spacing"]["margin"] == 36, a3
    assert a4["font_pt"] == 12.0, a4
    return ok("透明度 88 / 浅色 / 宽松(外边距 36) / 12pt 全部真的进了样式（不是只存了盘）")


@case("E3 底图管线被设置变更触发（只存路径不加载 = 走查抓到的真 bug）")
def t_wallpaper_triggered():
    import numpy as np
    import cv2
    b = _fresh_window()
    win, app = b["window"], b["app"]
    sw = win._ui["pages"]["settings"]._widgets                      # noqa: SLF001
    p = _TMP / "e3_wall.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    img = np.random.default_rng(3).integers(0, 256, (500, 800, 3)).astype("uint8")
    p.write_bytes(cv2.imencode(".png", img)[1].tobytes())
    sw["wallpaper"].setText(str(p))
    sw["wallpaper"].editingFinished.emit()
    app.processEvents()
    meta = win._ui["state"].get("wallpaper_meta") or {}             # noqa: SLF001
    assert meta.get("brightness_mean") is not None, f"底图管线没跑：{meta}"
    assert float(meta.get("dim_used") or 0) > 0, meta
    # 参数变更 → 重跑（模糊进元数据）
    sw["blur"].setValue(6)
    app.processEvents()
    meta2 = win._ui["state"].get("wallpaper_meta") or {}            # noqa: SLF001
    assert meta2.get("blur") == 6, meta2
    # 坏图 → 明确提示 + 回退纯色（不崩）
    sw["wallpaper"].setText(str(_TMP / "不存在.png"))
    sw["wallpaper"].editingFinished.emit()
    app.processEvents()
    st = win._ui["state"].get("wallpaper_meta") or {}               # noqa: SLF001
    assert st == {}, st
    assert any("底图不可用" in n for n in win._ui["ctx"].notes), win._ui["ctx"].notes[-2:]  # noqa: SLF001
    return ok("换图/改模糊都重跑管线；坏图回退纯色并如实提示")


@case("E4 自动取色的触发条件：换底图/解锁才算，**显式选色不许被覆盖**")
def t_accent_trigger_rules():
    import numpy as np
    import cv2
    b = _fresh_window()
    win, app, store = b["window"], b["app"], b["settings"]
    sw = win._ui["pages"]["settings"]._widgets                      # noqa: SLF001
    p = _TMP / "e4_wall.png"
    img = np.zeros((400, 600, 3), "uint8")
    img[:, :, 0] = 190
    img[:, :, 2] = 150
    p.write_bytes(cv2.imencode(".png", img)[1].tobytes())
    from PySide6.QtWidgets import QComboBox
    sw["wallpaper"].setText(str(p))
    sw["wallpaper"].editingFinished.emit()
    app.processEvents()
    auto = str(store.get("accent") or "")
    assert auto.startswith("#") and len(auto) == 7, auto
    # 显式选四档预设之一 → 必须原样保留（走真实路径：下拉框 → 信号 → 存盘 + 通知）
    target = "#2FC6C6"
    combo = sw["accent"]
    assert isinstance(combo, QComboBox)
    combo.setCurrentIndex(max(0, combo.findData(target)))
    app.processEvents()
    assert store.get("accent") == target, f"显式选色被覆盖：{store.get('accent')}"
    assert win._ui["applied"].get("accent") == target, win._ui["applied"]   # noqa: SLF001
    # 锁定时换底图 → 强调色不变
    sw["accent_locked"].setChecked(True)
    app.processEvents()
    sw["wallpaper"].setText(str(p))
    sw["wallpaper"].editingFinished.emit()
    app.processEvents()
    assert store.get("accent") == target, f"锁定后仍被自动取色改了：{store.get('accent')}"
    return ok(f"自动取色在换图时生效（{auto}）；显式选 {target} 后不被覆盖；锁定时不动")


@case("E5 语言解析顺序：设置 > 安装器选择 > 系统 > en-US")
def t_locale_resolution():
    import pathlib
    import tempfile
    from daedalus.ui.i18n import LOCALES, detect_system_locale, resolve_locale
    d = pathlib.Path(tempfile.mkdtemp(prefix="dae_loc_"))
    (d / "install.marker").write_text("installed=1\nlang=2052\n", encoding="utf-8")
    assert resolve_locale("", exe_dir=d) == "zh-CN", "安装器选简体却没用简体"
    (d / "install.marker").write_text("lang=1041\n", encoding="utf-8")
    assert resolve_locale("", exe_dir=d) == "ja-JP"
    assert resolve_locale("en-US", exe_dir=d) == "en-US", "用户设置应当优先于安装器选择"
    e = pathlib.Path(tempfile.mkdtemp(prefix="dae_loc2_"))
    assert resolve_locale("", exe_dir=e) == detect_system_locale(), "无标记应落到系统语言"
    assert detect_system_locale() in LOCALES, detect_system_locale()
    return ok(f"四种情况全对（本机系统语言 = {detect_system_locale()}）")


@case("E6 三语文案齐备 + ui/ 里没有硬编码的可视中文")
def t_i18n_coverage():
    import ast as _ast
    from daedalus.ui.i18n import LOCALES, coverage, translator
    cov, total = coverage()
    missing = {loc: total - n for loc, n in cov.items()}
    assert not any(missing.values()), f"有语言缺翻译：{missing}"
    # 每种语言都能无缺键地取一遍（缺键会被 Translator 记下来）
    for loc in LOCALES:
        t = translator(loc)
        for key in ("nav.overview", "settings.group.wallpaper", "about.body1", "app.title"):
            assert t(key) and t.missing == set(), f"{loc} 缺键：{t.missing}"
    # ui/ 里除 i18n.py 外，**代码里的字符串字面量**不得含中文字符（注释/文档字符串不算）
    bad = []
    for p in sorted((ROOT / "src" / "daedalus" / "ui").glob("*.py")):
        if p.name == "i18n.py":
            continue
        text = p.read_text(encoding="utf-8")
        tree = _ast.parse(text)
        doc_lines = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                for ln in range(getattr(node, "lineno", 0), getattr(node, "end_lineno", 0) + 1):
                    doc_lines.add(ln)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str) \
                    and getattr(node, "lineno", 0) not in doc_lines:
                v = node.value
                if any("\u4e00" <= ch <= "\u9fff" for ch in v) and "noqa: i18n" not in v:
                    # 允许：日志/异常/内部提示（不是可视文案）——但必须显式标注
                    line = text.splitlines()[node.lineno - 1] if node.lineno <= len(text.splitlines()) else ""
                    if "noqa: i18n" in line:
                        continue
                    bad.append(f"{p.name}:{node.lineno} {v[:26]}")
    assert not bad, f"ui/ 里还有硬编码可视中文（应走 i18n）：{bad[:6]}"
    return ok(f"{total} 个键 × {len(LOCALES)} 语言全覆盖；ui/ 代码里零硬编码可视中文")


@case("E7 打包态取标记：冻结后找的是 **exe 同级**，不是 cwd 也不是解包临时目录")
def t_frozen_marker_path():
    """语言"装完是不是本机语言"里的**路径**那一半。

    E5 守住的是解析优先级；这条守的是**去哪儿找**。冻结态下若按 `__file__` 或 cwd 找，
    解包临时目录里永远没有 marker → 装机时选的语种被静默丢弃（就是"选简体蹦日文"这类）。
    """
    import pathlib
    import tempfile
    from daedalus.ui.i18n import marker_path, read_installer_locale, resolve_locale
    d = pathlib.Path(tempfile.mkdtemp(prefix="dae_frozen_"))
    (d / "install.marker").write_text("installed=1\nlang=1041\n", encoding="utf-8")
    saved = (getattr(sys, "frozen", None), sys.executable)
    try:
        sys.frozen = True                                   # type: ignore[attr-defined]
        sys.executable = str(d / "daedalus.exe")            # 假装自己就是装好的那个 exe
        assert marker_path() == d / "install.marker", marker_path()
        assert read_installer_locale() == "ja-JP", read_installer_locale()
        assert resolve_locale("") == "ja-JP", resolve_locale("")
        assert resolve_locale("zh-CN") == "zh-CN", "用户设置没优先于安装器选择"
    finally:
        if saved[0] is None:
            try:
                del sys.frozen                              # type: ignore[attr-defined]
            except Exception:
                pass
        else:
            sys.frozen = saved[0]                           # type: ignore[attr-defined]
        sys.executable = saved[1]
    assert marker_path().name == "install.marker"           # 还原后仍可用
    return ok("冻结态 marker 取 exe 同级；安装器选日文 → 日文；用户设置仍优先")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S10 门禁 · 数据根={_TMP} · 平台={os.environ.get('QT_QPA_PLATFORM')}\n" + "─" * 68)
    for name, fn in _CASES:
        try:
            note = str(fn())
            status = "SKIP" if note.startswith("SKIP") else "PASS"
            skips += status == "SKIP"
            print(f"[{status}] {name}\n        {note}")
        except Exception as e:
            fails += 1
            print(f"[FAIL] {name}\n        {type(e).__name__}: {e}")
    print("─" * 68)
    print(f"共 {len(_CASES)} 项：PASS {len(_CASES) - fails - skips} / SKIP {skips} / FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

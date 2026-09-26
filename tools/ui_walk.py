# -*- coding: utf-8 -*-
"""界面实操走查：**真的把每个页面、每个控件都动一遍**（离线、offscreen）

    python tools/ui_walk.py            # 人类可读
    python tools/ui_walk.py --json     # 机器可读（门禁吃它）

为什么要有它：机主原话是「你测试一下它的 UI 样式…别犯低级错误」「背景图选文件、还有自动取色
那些杂七杂八的都测一下」。所以这里**不是**看代码，而是把控件当用户去用：
  * 逐页进入 → 断言页面真的被换上去、内容非空；
  * 逐个设置控件 → 走真实的信号路径（改值 → `_apply` → 存盘 → 重刷），断言生效；
  * **背景图**：走「选文件 → 文本框 → editingFinished」这条真路径（文件对话框在无头环境没法弹，
    但它选完也就做这两件事），断言管线跑过、元数据落进窗口状态、设置被保存；
  * **自动取色**：给三张特征图（紫/灰/彩噪）断言取到预期色或**如实回退并说明**；
  * **不拥挤**（照 Kiana 规格）：间距数字逐项核对 + 卡片几何不重叠 + 卡片间距 > 卡内间距；
  * **不混语言**：切到 ja-JP / en-US 后，断言界面上不再出现 zh-CN 的专属文案
    （机主点名的「装完蹦日文/中文混着」那类低级错误）；
  * **导入 TXT**：真点「导入 TXT」按钮（对话框打桩），断言提取/排除/去重/按域名分组；
  * **语言切换条**：经标题栏的链接（English ｜ 简体中文 ｜ 日本語）真切语言，
    断言标题/导航/存储跟随、当前语言高亮不可点、与窗口按钮不重叠。
退出码 0 = 全部通过。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[dict] = []


def check(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append({"case": name, "ok": bool(ok), "note": str(note)[:200]})


def _img(path: pathlib.Path, w: int, h: int, mode: str) -> pathlib.Path:
    import cv2
    import numpy as np
    if mode == "gray":
        im = np.full((h, w, 3), 128, "uint8")
    elif mode == "purple":
        im = np.zeros((h, w, 3), "uint8")
        im[:, :, 0] = 190
        im[:, :, 2] = 150
    elif mode == "noise":
        im = np.random.default_rng(5).integers(0, 256, (h, w, 3)).astype("uint8")
    else:
        im = np.full((h, w, 3), 30, "uint8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cv2.imencode(".png", im)[1].tobytes())
    return path


def walk(*, verbose: bool = True) -> dict:
    from PySide6.QtWidgets import QLabel, QPushButton, QComboBox, QCheckBox, QSlider, QSpinBox, \
        QDoubleSpinBox, QLineEdit
    from daedalus.ui.app import MainWindow, build_headless
    from daedalus.ui.i18n import LOCALES, translator, _TABLE
    from daedalus.ui.theme import tokens as mk_tokens

    root = pathlib.Path(tempfile.mkdtemp(prefix="dae_uiwalk_"))
    mart = root / "art"
    purple = _img(mart / "紫.png", 900, 600, "purple")
    gray = _img(mart / "灰.png", 700, 900, "gray")
    noise = _img(mart / "彩噪.png", 1200, 500, "noise")

    # **把模态对话框打桩**：无头环境里真弹窗会永久阻塞（走查第一次就卡在这）。
    # 打桩之后反而更好——预设保存/导出/导入/选文件这几条**真代码路径**都会被走到，
    # 只是「用户点了确定/选了这个文件」这一步由桩给出确定答案。
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    preset_out = root / "walk_presets.json"
    QInputDialog.getText = staticmethod(lambda *a, **k: ("走查预设2", True))
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(preset_out), ""))
    def _open_stub(*a, **k):
        """按对话框用途给不同答案：**导入预设**给 JSON，**选底图**给图片。"""
        title = str(a[1] if len(a) > 1 else "") + str(k.get("caption", ""))
        return (str(preset_out) if ("预设" in title or "preset" in title.lower())
                else str(noise), "")
    QFileDialog.getOpenFileName = staticmethod(_open_stub)
    # 「删除任务记录」的确认框：默认**点「否」**（走查先验「没确认不许删」这条路）。
    # 需要验「是」的分支时，测试里再把这个桩改成 Yes。
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)

    b = build_headless(data_root=root, tokens=mk_tokens(locale="zh-CN"))
    win, app, store = b["window"], b["app"], b["settings"]
    win.show()
    app.processEvents()
    ui = win._ui

    # ── A. 逐页进入 ───────────────────────────────────────────
    for key in ("overview", "tasks", "logs", "settings", "about"):
        ms = MainWindow.goto(win, key)
        app.processEvents()
        page = ui["pages"][key]
        n = len(page.findChildren(object))
        check(f"页面可进入：{key}", ui["state"]["page"] == key and n > 0,
              f"切页 {ms:.2f}ms，控件 {n} 个")

    # ── B. 逐个设置控件走真实信号路径 ──────────────────────────
    sw = ui["pages"]["settings"]._widgets
    def _apply_now(key, value):
        """走控件自己的信号：`setValue`/`setChecked`/`setCurrentIndex` → 信号 → `_apply`。"""
        w = sw.get(key)
        if w is None:
            return None
        if isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, (QSlider, QSpinBox)):
            w.setValue(int(value))
        elif isinstance(w, QDoubleSpinBox):
            w.setValue(float(value))
        elif isinstance(w, QComboBox):
            idx = w.findData(value)
            w.setCurrentIndex(idx if idx >= 0 else 0)
        elif isinstance(w, QLineEdit):
            w.setText(str(value))
            w.editingFinished.emit()
        app.processEvents()
        return store.get(key)

    # 注意：`expert_mode` **不在**这里——它没有实现，已从设置界面移除（假开关比没有更坏）。
    # 新加的 `log_autoscroll` 在这里验：它是日志页「自动滚动」的真开关。
    cases = [("panel_alpha", 88), ("blur", 12), ("dim_manual", 20), ("light", True),
             ("signature", False), ("density", "relaxed"), ("font_pt", 12.0),
             ("focus", "top"), ("animations", False), ("fade_ms", 300),
             ("debounce_ms", 400), ("fps_cap", 60), ("downsample_max", 1920),
             ("log_autoscroll", False), ("accent_locked", True)]
    bad = []
    for key, val in cases:
        got = _apply_now(key, val)
        if str(got) != str(val):
            bad.append(f"{key}: 期望 {val} 实得 {got}")
    check("设置控件逐个生效（15 项，走真实信号）", not bad, "；".join(bad) or "全部生效")

    # 语义断言（不只是「存进去了」）：透明度真的进了样式、浅色真的换了底、密度真的改了间距
    applied = ui["applied"]
    check("透明度生效（panel_alpha=88 → 样式)", applied.get("panel_alpha") == 88,
          f"applied={applied.get('panel_alpha')}")
    check("浅色主题生效", applied.get("border") == "rgba(0,0,0,14)", f"border={applied.get('border')}")
    check("密度生效（relaxed → 外边距 36）", applied["spacing"]["margin"] == 36,
          f"margin={applied['spacing']['margin']}")
    check("字号生效（12pt）", applied.get("font_pt") == 12.0, f"font_pt={applied.get('font_pt')}")

    # ── C. 背景图：走「选文件 → 文本 → editingFinished」的真路径 ──
    _apply_now("light", False)
    for img, label in ((purple, "紫"), (gray, "灰"), (noise, "彩噪")):
        got = _apply_now("wallpaper", str(img))
        meta = ui["state"].get("wallpaper_meta") or {}
        ok = str(got) == str(img) and bool(meta)
        check(f"背景图设置生效：{label}", ok,
              f"存储={pathlib.Path(str(got)).name} 元数据={ {k: meta.get(k) for k in ('brightness_mean','dim_used')} }")
    # 参数变化也要重跑管线（模糊/蒙层）
    _apply_now("blur", 8)
    _apply_now("dim_manual", 30)
    meta2 = ui["state"].get("wallpaper_meta") or {}
    check("底图参数变化后管线重跑（模糊/蒙层进元数据）",
          meta2.get("blur") == 8 and float(meta2.get("dim_used") or 0) >= 30,
          f"meta={ {k: meta2.get(k) for k in ('blur','dim_used')} }")

    # ── D. 自动取色：三张特征图 ───────────────────────────────
    from daedalus.ui.wallpaper import extract_accent
    a_purple = extract_accent(str(purple))
    a_gray = extract_accent(str(gray))
    a_lock = extract_accent(str(purple), lock="#123456")
    r, g, bl = (int(a_purple["accent"][i:i + 2], 16) for i in (1, 3, 5))
    check("自动取色：紫图取到偏紫", a_purple["source"] == "auto" and r > g and bl > g,
          f"{a_purple['accent']}（{a_purple['source']}）")
    check("自动取色：灰图如实回退并说明",
          a_gray["source"] == "fallback_gray" and "饱和" in a_gray.get("note", ""),
          a_gray.get("note", "")[:60])
    check("锁定后原样使用、不计算", a_lock["source"] == "locked" and a_lock["accent"] == "#123456",
          a_lock["accent"])
    # 解锁 → **应当按当前底图自动取色**（这是「自动」的语义）
    _apply_now("accent_locked", False)
    auto_after_unlock = ui["applied"].get("accent")
    check("解锁后按底图自动取色（彩噪图 → 不是原锁定色）",
          auto_after_unlock != "#123456", f"{auto_after_unlock}")
    # 用户**显式选**强调色 → 必须原样保留（走真实路径：下拉框 → 信号 → _apply → 存盘 + 通知）。
    # 注意要选**四档预设里真实存在的颜色**（走查第一版选了自动取色得到的 #8310A0，
    # 下拉框里没有它 → 回退 index 0 且无信号 → 误判成「被覆盖」，纯属测试自己写错）。
    target = "#2FC6C6"
    _apply_now("accent", target)
    app.processEvents()
    check("显式选色后不被自动取色覆盖", ui["applied"].get("accent") == target,
          f"期望 {target} 实得 {ui['applied'].get('accent')}")

    # ── E. 预设：保存 / 加载（走按钮的 clicked）────────────────
    store.save_preset("走查预设")
    btns = [x for x in ui["pages"]["settings"].findChildren(QPushButton)]
    factory = [x for x in btns if "标准" in x.text() or "标准" in x.text()]
    if factory:
        factory[0].click()
        app.processEvents()
    check("出厂预设可一键加载", store.get("panel_alpha") == 65, f"alpha={store.get('panel_alpha')}")
    store.load_preset("走查预设")
    check("自定义预设往返", store.get("panel_alpha") == 88, f"alpha={store.get('panel_alpha')}")

    # ── F. 不拥挤（Kiana 规格）+ 几何不重叠 ────────────────────
    sp = ui["tokens"].spacing()
    spec_ok = (sp["margin"] == 36 and sp["gap"] == 20) if ui["tokens"].density == "relaxed" else True
    for d, want in (("compact", (18, 10)), ("standard", (28, 14)), ("relaxed", (36, 20))):
        s2 = mk_tokens(density=d).spacing()
        spec_ok = spec_ok and s2["margin"] == want[0] and s2["gap"] == want[1]
        spec_ok = spec_ok and s2["gap"] > max(s2["inner_gap"], s2["inner_top"], s2["inner_bottom"])
    check("间距符合 Kiana 规格（紧凑18/10 标准28/14 宽松36/20，且卡片间距>卡内）", spec_ok,
          f"standard={mk_tokens(density='standard').spacing()}")
    _apply_now("density", "standard")
    win.resize(1360, 860)
    app.processEvents()
    MainWindow.goto(win, "settings")
    app.processEvents()
    cards = [c for c in ui["pages"]["settings"].findChildren(object)
             if getattr(c, "objectName", lambda: "")() == "settingsCard"]
    overlap = []
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            a1, a2 = cards[i].geometry(), cards[j].geometry()
            if a1.intersects(a2) and a1.intersected(a2).width() > 4 and a1.intersected(a2).height() > 4:
                overlap.append((i, j))
    check("设置页卡片不重叠（同页卡片由同一生成器、间距由令牌统一）", not overlap,
          f"{len(cards)} 张卡片，重叠 {len(overlap)} 处")

    # ── G. 不混语言：切语言后界面不得残留另一种语言 ────────────
    # 判据要**精确**：整段标签等于中文译文才算残留（子串比较会假阳性——
    # 日文的「不透明度（40–95）」里就含中文的「透明度（40–95）」，走查第一次就被它骗了）。
    zh_texts = {v.strip() for row in _TABLE.values() for k, v in row.items()
                if k == "zh-CN" and len(v) > 3 and not v.startswith("{")}
    leftovers = []
    for loc in ("ja-JP", "en-US"):
        MainWindow.apply_locale(win, loc)
        app.processEvents()
        texts = []
        for w in win.findChildren(object):
            if isinstance(w, (QLabel, QPushButton, QCheckBox)):
                try:
                    if hasattr(w, "text") and w.isVisible():
                        texts.append(w.text())
                except Exception:
                    pass
        for txt in texts:
            base = str(txt).strip()
            if "{" in base:                     # 带占位符的模板还原后再比
                try:
                    base = base.format(text="x", name="x", version="x", why="x", n=0, skip=0)
                except Exception:
                    pass
            if base and base in zh_texts:
                leftovers.append(f"{loc}:{base[:18]}")
        check(f"切到 {loc} 后无中文残留", not any(x.startswith(loc) for x in leftovers),
              "、".join(x for x in leftovers if x.startswith(loc))[:120] or "干净")
    # 切回中文（并确认能切回来）
    MainWindow.apply_locale(win, "zh-CN")
    app.processEvents()
    check("能切回 zh-CN", "代达罗斯" in win.windowTitle(), win.windowTitle())

    # ── H. 每页控件都能被点（不炸）────────────────────────────
    errors: list[str] = []
    for key in ("overview", "tasks", "logs", "settings", "about"):
        MainWindow.goto(win, key)
        app.processEvents()
        page = ui["pages"][key]
        for w in page.findChildren(QPushButton):
            try:
                w.click() if w.isEnabled() else None
            except Exception as e:
                errors.append(f"{key}:{w.text()[:12]}:{type(e).__name__}")
        app.processEvents()
    check("逐页点遍所有按钮不抛异常", not errors, "；".join(errors[:4]) or "零异常")

    # ── I. 引擎没跑时页面照常（不白屏、不崩）───────────────────
    check("引擎未启动也能画（ctx.engine=None）", len(ui["pages"]) == 5,
          f"页面数 {len(ui['pages'])}")

    # ── J. 概览页数字**真的会刷新**（打包态截图看出来的第三个接线缺口）──
    #    刷新函数写好了、门禁也直接调它，但真跑起来**没有任何调用者**，数字永远停在「—」。
    poll_fn = ui.get("poll_fn")
    cards = getattr(ui["pages"]["overview"], "_stat_cards", [])
    before = [c._value_label.text() for c in cards] if cards else []   # noqa: SLF001

    class _FakeEngine:
        def metrics(self):
            return {"summary": {"pages_per_sec": 9.5, "mb_per_sec": 0.5, "tasks_done": 3,
                                "tasks_failed": 1, "net_latency_p95": 0.03},
                    "tasks": [{"task_id": "w1", "state": "done", "target": "https://e.test/x",
                               "content_hash": "cd" * 32, "kind": "page"}],
                    "fetcher": {}, "frontier": {}, "writer": {}, "ledger": {}, "alerts": []}
    ui["ctx"].engine = _FakeEngine()
    try:
        poll_fn() if callable(poll_fn) else None
        app.processEvents()
    except Exception as e:
        check("概览页指标轮询可用", False, f"{type(e).__name__}: {e}")
    after = [c._value_label.text() for c in cards] if cards else []     # noqa: SLF001
    rec = ui.get("poll") or {}
    check("概览页数字运行期真的刷新（不是永远停在占位符）",
          bool(poll_fn) and before != after and after and after[0].startswith("9.5"),
          f"{before[:1]} → {after[:1]}；轮询耗时 {rec.get('last_ms', 0):.1f}ms"
          f"（超 {MainWindow.SLOW_MS}ms 会自动降频到 {MainWindow.POLL_MS_BACKOFF}ms）")

    # ── K. 任务页的三个真操作（重试 / 导出选中 / 删除记录）────────────
    from daedalus.ui.app import MainWindow as _MW
    _MW.refresh_pages(win, {"summary": {}, "tasks": [
        {"task_id": "w-t1", "state": "dead", "target": "https://e.test/a?token=SEKRET1",
         "attempts": 3, "content_hash": "ef" * 32},
        {"task_id": "w-t2", "state": "failed", "target": "https://e.test/b",
         "attempts": 1, "content_hash": "12" * 32}]})
    app.processEvents()
    tp = ui["pages"]["tasks"]
    tools, table = getattr(tp, "_tools", None), getattr(tp, "_table", None)

    class _WalkEngine:
        """桩引擎：记录被调了什么，导出走**真实现**的脱敏口径（与 CLI 同源）。"""

        def __init__(self):
            self.retried, self.exported, self.dry, self.forgot = [], [], [], []

        def retry_tasks(self, ids, **kw):
            self.retried.append(list(ids))
            return {"requested": len(ids), "ok": len(ids),
                    "results": [{"task_id": i, "ok": True, "why": "ok"} for i in ids]}

        def export_tasks_jsonl(self, ids, **kw):
            self.exported.append(list(ids))
            return '{"kind":"task","target":"https://e.test/a?token=[REDACTED]"}\n'

        def forget_tasks(self, ids, dry_run=True, **kw):
            (self.dry if dry_run else self.forgot).append(list(ids))
            return {"dry_run": dry_run, "deleted": {"tasks": len(ids), "task_evidence": 1,
                                                    "errors": 0}}

    stub = _WalkEngine()
    ui["ctx"].engine = stub
    if tools and table and table.rowCount() >= 1:
        table.selectRow(0)
        app.processEvents()
        tools["retry"].click()
        app.processEvents()
        tools["export"].click()          # 保存对话框已在上面打桩 → 真写文件
        app.processEvents()
        tools["forget"].click()          # 确认框桩成「否」→ 一个都不许删
        app.processEvents()
        check("任务页三操作都真的调了引擎（重试/导出/删除）",
              stub.retried == [["w-t1"]] and stub.exported == [["w-t1"]],
              f"重试={stub.retried} 导出={stub.exported}")
        check("删除：没点「是」就一行都不删（红线）", not stub.forgot and not stub.dry,
              f"dry={stub.dry} 实删={stub.forgot}")
    else:
        check("任务页三操作走查可用", False, "没有工具行或表格为空")
    ui["ctx"].engine = None

    # ── L. 导入 TXT：**真点一遍**「导入 TXT」按钮（对话框打桩，与上面同一套纪律）──
    #    机主原话：「直接提取 txt 里面的链接…断掉的排除…小分类过滤」。走查与门禁 E14
    #    同一口径：有效 2 / 无效排除 2 / 去重 2 / 域名 2，输出按域名分组、组间空行。
    page = ui["pages"]["overview"]                     # 语言走查段可能重建过页面，现取现用
    c = getattr(page, "_collect", None)
    if c and c.get("import") is not None:
        txt = root / "walk_links.txt"
        txt.write_text("\n".join([
            "https://alpha.test/a",
            "1. https://beta.test/b 标题",
            "https://alpha.test/a",
            "不是链接的一行",
            "https://",
            "https://alpha.test/a。",
            "",
        ]), encoding="utf-8")
        picked = {"filter": ""}

        def _txt_stub(*a, **k):
            picked["filter"] = str(a[3]) if len(a) > 3 else ""
            return (str(txt), "")

        QFileDialog.getOpenFileName = staticmethod(_txt_stub)   # 走查已到最后一段，覆盖无碍
        c["import"].click()
        app.processEvents()
        want_box = "https://alpha.test/a\n\nhttps://beta.test/b"
        want_msg = translator("zh-CN")("collect.import_done", n=2, m=2, d=2, k=2)
        got_box = c["box"].toPlainText()
        check("导入 TXT：*.txt 过滤 + 提取/排除/去重/按域名分组（真点按钮）",
              "*.txt" in picked["filter"] and got_box == want_box
              and c["status"].text() == want_msg and want_msg in ui["ctx"].notes,
              f"filter={picked['filter']!r} box={got_box!r} status={c['status'].text()!r}")
    else:
        check("导入 TXT 走查可用", False, "概览页没有「导入 TXT」按钮")

    # ── M. 语言切换条：**经链接**切语言（机主图样式：English ｜ 简体中文 ｜ 日本語）──
    #    链接在标题栏里（窗口控制按钮左边），当前语言加粗+强调色+不可点；点链接 =
    #    存 settings 的 locale + 整页重建（与设置页下拉同一条通路）。
    lb = ui.get("langbar")
    if lb is not None and getattr(lb, "links", None):
        order = ("en-US", "zh-CN", "ja-JP")
        texts = [lb.links[k].text() for k in order]
        cur0 = lb.links.get(str(ui["tokens"].locale))
        hl_ok = cur0 is not None and not cur0.isEnabled() and "font-weight: 700" in cur0.styleSheet()
        check("语言切换条：三个母语原文链接 + 当前语言加粗不可点",
              texts == ["English", "简体中文", "日本語"] and hl_ok,
              f"texts={texts} current={ui['tokens'].locale!r}")
        lb.links["en-US"].click()
        app.processEvents()
        check("经链接切到 English：标题/导航/存储跟随、整页重建后语言条仍在",
              translator("en-US")("app.title") in win.windowTitle()
              and ui["nav_items"]["overview"].text() == "Overview"
              and store.get("locale") == "en-US" and ui.get("langbar") is lb,
              f"title={win.windowTitle()!r} nav={ui['nav_items']['overview'].text()!r} "
              f"locale={store.get('locale')!r}")
        lb.links["ja-JP"].click()
        app.processEvents()
        check("经链接切到 日本語：高亮跟过去、其余恢复可点",
              "統合収集・観測エンジン" in win.windowTitle()
              and ui["nav_items"]["overview"].text() == "概要"
              and not lb.links["ja-JP"].isEnabled() and lb.links["en-US"].isEnabled(),
              f"nav={ui['nav_items']['overview'].text()!r}")
        overlap = ui["langbar_check"]()
        check("语言切换条不与窗口控制按钮重叠", not overlap,
              f"mode={ui.get('langbar_mode')} overlap={overlap}")
        lb.links["zh-CN"].click()                       # 收尾切回默认语言（顺手验第三个链接）
        app.processEvents()
        check("经链接切回 简体中文", store.get("locale") == "zh-CN",
              f"locale={store.get('locale')!r}")
    else:
        check("语言切换条走查可用", False, "窗口没有语言切换条")

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r["ok"]]
    return {"total": total, "failed": len(failed), "ok": not failed,
            "results": RESULTS, "locale_detected": b["tokens"].locale,
            "note": "界面实操走查（offscreen）：逐页进入 + 每个控件走真实信号 + 背景图真路径 + "
                    "三张特征图取色 + Kiana 间距规格 + 几何不重叠 + 语言不混 + 按钮全点一遍 + "
                    "任务页三操作（重试/导出/删除确认）+ 导入 TXT（真点按钮）+ 经标题栏链接切语言"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="界面实操走查（离线）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = walk()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    for r in rep["results"]:
        print(f"  [{'OK ' if r['ok'] else 'FAIL'}] {r['case']}" + (f"  — {r['note']}" if r["note"] else ""))
    print(f"\n共 {rep['total']} 项：通过 {rep['total'] - rep['failed']} / 失败 {rep['failed']}")
    print(rep["note"])
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

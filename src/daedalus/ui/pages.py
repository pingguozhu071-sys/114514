# -*- coding: utf-8 -*-
"""页面：概览 / 任务 / 日志 / 设置 / 关于（设置固定在最下面）

每页都接受 `ctx`（引擎句柄 + 设置 + 刷新回调）；`ctx` 为空时**照样能画出来**（离线测试、
或引擎还没启动时）——UI 不该因为"引擎没跑"就白屏或崩掉。
"""

from __future__ import annotations

import json
import logging
import pathlib

from daedalus.ui.widgets import GlassCard, PageBase, StatCard

logger = logging.getLogger(__name__)

__all__ = ["build_pages", "PAGE_TITLES"]

PAGE_TITLES = (("overview", "概览"), ("tasks", "任务"), ("logs", "日志"),
               ("settings", "设置"), ("about", "关于"))

_SETTINGS_GROUPS = (
    ("主题", (("light", "浅色主题", "bool"),)),
    ("强调色", (("accent", "强调色", "accent"), ("accent_locked", "锁定（关掉自动取色）", "bool"))),
    ("玻璃", (("panel_alpha", "透明度（40–95）", "int"),)),
    ("底图", (("wallpaper", "底图路径", "file"), ("blur", "模糊（0–30）", "int"),
             ("dim_manual", "蒙层（0–60，只能加暗）", "int"), ("focus", "九宫格焦点", "focus"),
             ("downsample_max", "下采样长边上限", "int"))),
    ("排版与密度", (("font_pt", "界面字号（≥10pt）", "float"),
                 ("density", "密度（紧凑/标准/宽松）", "choice"))),
    ("动效", (("animations", "动效总开关", "bool"), ("fade_ms", "交叉溶解（ms）", "int"),
             ("debounce_ms", "渲染防抖（ms）", "int"), ("fps_cap", "限帧（fps）", "int"))),
    ("其它", (("signature", "显示右下角签名", "bool"),
             ("expert_mode", "专家模式（允许单卡覆写玻璃参数）", "bool"))),
)


def build_pages(window, tokens, ctx=None) -> dict:
    """建好所有页面，返回 `{key: page}`（顺序与 `PAGE_TITLES` 一致）。"""
    pages = {
        "overview": _overview(window, tokens),
        "tasks": _tasks(window, tokens),
        "logs": _logs(window, tokens),
        "settings": _settings(window, tokens, ctx),
        "about": _about(window, tokens, ctx),
    }
    return pages


# ── 概览 ─────────────────────────────────────────────────────────
def _overview(window, tokens):
    page, lay = PageBase.make(tokens, name="overview", parent=window)
    cards = [StatCard.make(tokens, "页/秒", "—", name="statCard"),
             StatCard.make(tokens, "MB/s", "—", name="statCard"),
             StatCard.make(tokens, "任务 成功/失败", "—", name="statCard"),
             StatCard.make(tokens, "请求 p95", "—", name="statCard")]
    row = GlassCard.make(tokens, name="card")
    rl = GlassCard.body(row, tokens)
    from PySide6.QtWidgets import QHBoxLayout, QWidget
    holder = QWidget(row)
    hl = QHBoxLayout(holder)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(tokens.spacing()["gap"])
    for c in cards:
        hl.addWidget(c)
    rl.addWidget(holder)
    # 内存自证与资源计划：把"嘴上说的 ≤4GB"贴到界面上
    plan = GlassCard.make(tokens, name="card")
    pl = GlassCard.body(plan, tokens)
    from PySide6.QtWidgets import QLabel
    from daedalus.core.limits import ResourcePlan
    plan_txt = ResourcePlan.from_config(None).memory_arithmetic()
    pl.addWidget(_label(f"资源计划：{plan_txt}", tokens))
    pl.addWidget(_label("队列全有界｜写盘单线程 + 批提交｜缺省即拒绝", tokens, muted=True))
    lay.insertWidget(0, row)
    lay.insertWidget(1, plan)
    page._stat_cards = cards          # noqa: SLF001 - 刷新时要用
    return page


def _label(text: str, tokens, *, muted: bool = False):
    from PySide6.QtWidgets import QLabel
    from daedalus.ui.theme import status_colors
    lab = QLabel(str(text))
    lab.setWordWrap(True)
    col = status_colors(tokens.light)["muted"] if muted else "inherit"
    if muted:
        lab.setStyleSheet(f"color: {col}; font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    return lab


# ── 任务 ─────────────────────────────────────────────────────────
def _tasks(window, tokens):
    page, lay = PageBase.make(tokens, name="tasks", parent=window)
    card = GlassCard.make(tokens, name="taskCard")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label("任务下钻：每一行都能说清「看到了什么 → 决定了什么 → 为什么」", tokens))
    from PySide6.QtWidgets import QTableWidget
    table = QTableWidget(0, 5, card)
    table.setObjectName("taskTable")
    table.setHorizontalHeaderLabels(["状态", "目标", "尝试/限流/转移", "证据", "字节"])
    table.setStyleSheet("background: transparent; border: none;")
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    cl.addWidget(table)
    lay.insertWidget(0, card)
    page._table = table             # noqa: SLF001
    return page


# ── 日志 ─────────────────────────────────────────────────────────
def _logs(window, tokens):
    page, lay = PageBase.make(tokens, name="logs", parent=window)
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label("结构化日志（JSON 行）：字段稳定，可直接喂 jq；脱敏在落盘之前完成", tokens))
    from PySide6.QtWidgets import QPlainTextEdit
    view = QPlainTextEdit(card)
    view.setObjectName("logView")
    view.setReadOnly(True)
    view.setMinimumHeight(360)
    view.setStyleSheet(f"background: transparent; border: none;"
                       f" font-family: Consolas, 'Cascadia Mono', monospace;"
                       f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    cl.addWidget(view)
    lay.insertWidget(0, card)
    page._view = view               # noqa: SLF001
    return page


# ── 设置（固定在最下面）───────────────────────────────────────────
def _settings(window, tokens, ctx):
    page, lay = PageBase.make(tokens, name="settings", parent=window)
    lay.insertWidget(0, _label("外观设置**即改即存**（没有「应用」按钮）；"
                              "所有卡片由同一个生成器出样式，保证统一感", tokens))
    store = getattr(ctx, "settings", None) if ctx is not None else None
    widgets: dict = {}

    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                                   QLineEdit, QPushButton, QSlider, QSpinBox)
    from daedalus.ui.theme import ACCENT_PRESETS, ThemeError

    for group, fields in _SETTINGS_GROUPS:
        card = GlassCard.make(tokens, name="settingsCard")
        cl = GlassCard.body(card, tokens)
        title = _label(group, tokens)
        title.setStyleSheet(f"color: {tokens.accent}; font-weight: 700;")
        cl.addWidget(title)
        for key, caption, kind in fields:
            row = QHBoxLayout()
            row.addWidget(_label(caption, tokens))
            cur = store.get(key) if store is not None else None
            if kind == "bool":
                w = QCheckBox(card)
                w.setChecked(bool(cur) if cur is not None else False)
                w.toggled.connect(lambda v, k=key: _apply(store, ctx, {k: bool(v)}))
            elif kind == "int":
                lo, hi = {"panel_alpha": (40, 95), "blur": (0, 30), "dim_manual": (0, 60),
                          "downsample_max": (480, 7680), "fade_ms": (0, 2000),
                          "debounce_ms": (0, 2000), "fps_cap": (5, 120)}.get(key, (0, 10000))
                if key in ("panel_alpha", "blur", "dim_manual"):
                    w = QSlider(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Orientation.Horizontal, card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}))
                else:
                    w = QSpinBox(card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}))
            elif kind == "float":
                w = QDoubleSpinBox(card)
                w.setRange(10.0, 24.0)
                w.setSingleStep(0.5)
                w.setValue(float(cur) if cur is not None else 10.0)
                w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: float(v)}))
            elif kind == "accent":
                w = QComboBox(card)
                for nm, hx in ACCENT_PRESETS:
                    w.addItem(f"{nm} {hx}", hx)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}))
            elif kind == "choice":
                w = QComboBox(card)
                for nm, val in (("紧凑", "compact"), ("标准", "standard"), ("宽松", "relaxed")):
                    w.addItem(nm, val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}))
            elif kind == "focus":
                w = QComboBox(card)
                for nm, val in (("居中", "center"), ("上", "top"), ("下", "bottom"),
                                ("左", "left"), ("右", "right")):
                    w.addItem(nm, val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}))
            else:                                   # file
                box = QHBoxLayout()
                w = QLineEdit(card)
                w.setText(str(cur or ""))
                w.setPlaceholderText("留空 = 关闭底图（回退主题纯色）")
                pick = QPushButton("选择…", card)
                pick.clicked.connect(lambda _c=False, e=w: _pick_file(e))
                w.editingFinished.connect(lambda k=key, e=w: _apply(store, ctx, {k: e.text()}))
                box.addWidget(w)
                box.addWidget(pick)
                row.addLayout(box)
                cl.addLayout(row)
                widgets[key] = w
                continue
            row.addWidget(w)
            cl.addLayout(row)
            widgets[key] = w
        # 插在 stretch 之前（页面基类在末尾放了一个 stretch，保证内容不拉伸）
        lay.insertWidget(max(0, lay.count() - 1), card)

    # 预设：保存 / 加载 / 导入导出
    pre = GlassCard.make(tokens, name="settingsCard")
    pl = GlassCard.body(pre, tokens)
    pl.addWidget(_label("预设", tokens))
    from PySide6.QtWidgets import QInputDialog
    from daedalus.ui.settings import FACTORY_PRESETS
    for name in FACTORY_PRESETS:
        btn = QPushButton(f"加载「{name}」", pre)
        btn.clicked.connect(lambda _c=False, n=name: _load_preset(store, ctx, n))
        pl.addWidget(btn)

    def _save_current():
        if store is None:
            return
        name, okk = QInputDialog.getText(pre, "保存预设", "预设名：")
        if okk and name.strip():
            store.save_preset(name.strip())
            _toast(ctx, f"预设「{name.strip()}」已保存")

    b1 = QPushButton("保存当前为预设…", pre)
    b1.clicked.connect(_save_current)
    b2 = QPushButton("导出预设 JSON…", pre)
    b2.clicked.connect(lambda: _export_presets(store, ctx))
    b3 = QPushButton("导入预设 JSON…", pre)
    b3.clicked.connect(lambda: _import_presets(store, ctx))
    for b in (b1, b2, b3):
        pl.addWidget(b)
    lay.insertWidget(max(0, lay.count() - 1), pre)
    page._widgets = widgets         # noqa: SLF001
    page._error_note = None         # noqa: SLF001
    return page


def _apply(store, ctx, patch: dict) -> None:
    """设置变更的统一入口：校验 → 保存 → 让窗口重刷（**即改即存**）。"""
    from daedalus.ui.theme import ThemeError
    try:
        if store is not None:
            store.update(patch)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(patch)
    except ThemeError as e:
        _toast(ctx, f"设置未生效：{e}", error=True)


def _load_preset(store, ctx, name: str) -> None:
    from daedalus.ui.theme import ThemeError
    try:
        body = store.load_preset(name)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(body)
        _toast(ctx, f"已加载预设「{name}」")
    except ThemeError as e:
        _toast(ctx, str(e), error=True)


def _export_presets(store, ctx) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getSaveFileName(None, "导出预设", "daedalus-presets.json",
                                      "JSON (*.json)")
    if p:
        _toast(ctx, f"已导出 {store.export_presets(p)['count']} 个预设")


def _import_presets(store, ctx) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, "导入预设", "", "JSON (*.json)")
    if p:
        try:
            r = store.import_presets(p)
            _toast(ctx, f"导入 {r['imported']} 个（跳过 {r['skipped']}）")
        except Exception as e:
            _toast(ctx, f"导入失败：{e}", error=True)


def _pick_file(edit) -> None:
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, "选择底图", "",
                                       "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
    if p:
        edit.setText(p)
        edit.editingFinished.emit()


def _toast(ctx, msg: str, *, error: bool = False) -> None:
    """提示出口：有 ctx 就交给它（窗口状态栏/浮层）；没有就记日志（**不静默**）。"""
    if ctx is not None and hasattr(ctx, "notify"):
        try:
            ctx.notify(str(msg), error=error)
            return
        except Exception:
            pass
    logger.warning("界面提示：%s", msg)


# ── 关于 ─────────────────────────────────────────────────────────
def _about(window, tokens, ctx):
    page, lay = PageBase.make(tokens, name="about", parent=window)
    from daedalus import about
    info = about()
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(f"{info['name']}（{info['name_zh']} / {info['name_ja']}）v{info['version']}",
                        tokens))
    cl.addWidget(_label(info["tagline_zh"], tokens, muted=True))
    cl.addWidget(_label("不是爬虫：三种采集环境（直连网络 / 浏览器运行时 / 制品与媒体）"
                        "由证据驱动选择；先捕获后理解；事实永不丢失。", tokens))
    cl.addWidget(_label("边界：只在有权访问且能合法观察的范围内采集；不绕过登录/验证码/风控/签名；"
                        "被拦截时降速 → 停止 → 报告。", tokens, muted=True))
    lay.insertWidget(0, card)
    return page

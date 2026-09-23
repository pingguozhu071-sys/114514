# -*- coding: utf-8 -*-
"""页面：概览 / 任务 / 日志 / 设置 / 关于（设置固定在最下面）

两条纪律（机主明确点过的两条）：
  * **界面文案一律走 i18n**（`ui/i18n.py` 的键），这里**不许出现裸的可视文案**——
    否则"安装时选了简体中文、装完蹦日文"这类问题会从任何一个漏网的字符串里冒出来；
    门禁会扫本文件里是否还有未走文案表的中文。
  * **布局照 Kiana 的规格**（外边距 28 / 卡片间距 14 / 卡内 18,14 / 卡内间距 4,10,12），
    宁可留白也不要挤：卡片间距必须大于卡内间距，页面内容不拉伸（`PageBase` 末尾有 stretch）。

每页都接受 `ctx`（引擎句柄 + 设置 + 刷新回调）；`ctx` 为空时**照样能画出来**（离线测试、
或引擎还没启动时）——UI 不该因为"引擎没跑"就白屏或崩掉。
"""

from __future__ import annotations

import logging

from daedalus.ui.i18n import translator
from daedalus.ui.widgets import GlassCard, PageBase, StatCard

logger = logging.getLogger(__name__)

__all__ = ["build_pages", "PAGE_KEYS", "PAGE_TITLES"]

# 页面键与**文案键**（标题从文案表取，不再写死）
PAGE_KEYS = ("overview", "tasks", "logs", "settings", "about")

# 设置项分组：`(分组文案键, ((设置键, 文案键, 控件类型), …))`
_SETTINGS_GROUPS = (
    ("settings.group.theme", (("light", "settings.light", "bool"),)),
    ("settings.group.accent", (("accent", "settings.accent", "accent"),
                               ("accent_locked", "settings.accent_locked", "bool"))),
    ("settings.group.glass", (("panel_alpha", "settings.alpha", "int"),)),
    ("settings.group.wallpaper", (("wallpaper", "settings.wallpaper", "file"),
                                  ("blur", "settings.blur", "int"),
                                  ("dim_manual", "settings.dim", "int"),
                                  ("focus", "settings.focus", "focus"),
                                  ("downsample_max", "settings.downsample", "int"))),
    ("settings.group.type", (("font_pt", "settings.font", "float"),
                             ("density", "settings.density", "choice"))),
    ("settings.group.motion", (("animations", "settings.animations", "bool"),
                               ("fade_ms", "settings.fade", "int"),
                               ("debounce_ms", "settings.debounce", "int"),
                               ("fps_cap", "settings.fps", "int"))),
    ("settings.group.other", (("signature", "settings.signature", "bool"),
                              ("expert_mode", "settings.expert", "bool"))),
)


def page_titles(t) -> tuple[tuple[str, str], ...]:
    """按当前语言给出 `((页面键, 标题), …)`。"""
    return tuple((k, t(f"nav.{k}")) for k in PAGE_KEYS)


def build_pages(window, tokens, ctx=None) -> dict:
    """建好所有页面，返回 `{key: page}`。语言从 `tokens.locale` 来（由 app 解析后注入）。"""
    t = translator(getattr(tokens, "locale", "en-US"))
    return {
        "overview": _overview(window, tokens, t),
        "tasks": _tasks(window, tokens, t),
        "logs": _logs(window, tokens, t),
        "settings": _settings(window, tokens, ctx, t),
        "about": _about(window, tokens, t),
    }


# ── 概览 ─────────────────────────────────────────────────────────
def _overview(window, tokens, t):
    page, lay = PageBase.make(tokens, name="overview", parent=window)
    cards = [StatCard.make(tokens, t("stat.pages_per_sec"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.mb_per_sec"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.tasks_ok_fail"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.request_p95"), "—", name="statCard")]
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
    from daedalus.core.limits import ResourcePlan
    plan_txt = ResourcePlan.from_config(None).memory_arithmetic()
    pl.addWidget(_label(t("overview.plan", text=plan_txt), tokens))
    pl.addWidget(_label(t("overview.plan_note"), tokens, muted=True))
    lay.insertWidget(0, row)
    lay.insertWidget(1, plan)
    page._stat_cards = cards          # noqa: SLF001 - 刷新时要用
    return page


def _label(text: str, tokens, *, muted: bool = False):
    from PySide6.QtWidgets import QLabel
    from daedalus.ui.theme import status_colors
    lab = QLabel(str(text))
    lab.setWordWrap(True)
    if muted:
        lab.setStyleSheet(f"color: {status_colors(tokens.light)['muted']};"
                          f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    return lab


# ── 任务 ─────────────────────────────────────────────────────────
def _tasks(window, tokens, t):
    page, lay = PageBase.make(tokens, name="tasks", parent=window)
    card = GlassCard.make(tokens, name="taskCard")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("tasks.hint"), tokens))
    from PySide6.QtWidgets import QTableWidget
    table = QTableWidget(0, 6, card)
    table.setObjectName("taskTable")
    table.setHorizontalHeaderLabels([t("tasks.col.state"), t("tasks.col.target"),
                                     t("tasks.col.counters"), t("tasks.col.evidence"),
                                     t("tasks.col.fingerprint"), t("tasks.col.bytes")])
    table.setStyleSheet("background: transparent; border: none;")
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    cl.addWidget(table)
    lay.insertWidget(0, card)
    page._table = table             # noqa: SLF001
    return page


# ── 日志 ─────────────────────────────────────────────────────────
def _logs(window, tokens, t):
    page, lay = PageBase.make(tokens, name="logs", parent=window)
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("logs.hint"), tokens))
    from PySide6.QtWidgets import QPlainTextEdit
    view = QPlainTextEdit(card)
    view.setObjectName("logView")
    view.setReadOnly(True)
    view.setMinimumHeight(360)
    view.setStyleSheet("background: transparent; border: none;"
                       " font-family: Consolas, 'Cascadia Mono', monospace;"
                       f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    cl.addWidget(view)
    lay.insertWidget(0, card)
    page._view = view               # noqa: SLF001
    return page


# ── 设置（固定在最下面）───────────────────────────────────────────
def _settings(window, tokens, ctx, t):
    page, lay = PageBase.make(tokens, name="settings", parent=window)
    lay.insertWidget(0, _label(t("settings.intro"), tokens))
    store = getattr(ctx, "settings", None) if ctx is not None else None
    widgets: dict = {}

    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                                   QLineEdit, QPushButton, QSlider, QSpinBox)
    from PySide6.QtWidgets import QInputDialog
    from daedalus.ui.i18n import LOCALE_AUTO, LOCALE_NAMES, LOCALES
    from daedalus.ui.theme import ACCENT_PRESETS

    def _add_card(title_key: str):
        card = GlassCard.make(tokens, name="settingsCard")
        cl = GlassCard.body(card, tokens)
        title = _label(t(title_key), tokens)
        title.setStyleSheet(f"color: {tokens.accent}; font-weight: 700;")
        cl.addWidget(title)
        lay.insertWidget(max(0, lay.count() - 1), card)
        return card, cl

    for group_key, fields in _SETTINGS_GROUPS:
        card, cl = _add_card(group_key)
        for key, label_key, kind in fields:
            row = QHBoxLayout()
            row.addWidget(_label(t(label_key), tokens))
            cur = store.get(key) if store is not None else None
            if kind == "bool":
                w = QCheckBox(card)
                w.setChecked(bool(cur) if cur is not None else False)
                w.toggled.connect(lambda v, k=key: _apply(store, ctx, {k: bool(v)}, t))
            elif kind == "int":
                lo, hi = {"panel_alpha": (40, 95), "blur": (0, 30), "dim_manual": (0, 60),
                          "downsample_max": (480, 7680), "fade_ms": (0, 2000),
                          "debounce_ms": (0, 2000), "fps_cap": (5, 120)}.get(key, (0, 10000))
                if key in ("panel_alpha", "blur", "dim_manual"):
                    from PySide6.QtCore import Qt as _Qt
                    w = QSlider(_Qt.Orientation.Horizontal, card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}, t))
                else:
                    w = QSpinBox(card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}, t))
            elif kind == "float":
                w = QDoubleSpinBox(card)
                w.setRange(10.0, 24.0)
                w.setSingleStep(0.5)
                w.setValue(float(cur) if cur is not None else 10.0)
                w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: float(v)}, t))
            elif kind == "accent":
                w = QComboBox(card)
                for nm, hx in ACCENT_PRESETS:
                    w.addItem(f"{nm} {hx}", hx)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            elif kind == "choice":
                w = QComboBox(card)
                for val in ("compact", "standard", "relaxed"):
                    w.addItem(t(f"density.{val}"), val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            elif kind == "focus":
                w = QComboBox(card)
                for val in ("center", "top", "bottom", "left", "right"):
                    w.addItem(t(f"focus.{val}"), val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            else:                                   # file：底图路径 + "选择…"
                box = QHBoxLayout()
                w = QLineEdit(card)
                w.setText(str(cur or ""))
                w.setPlaceholderText(t("settings.wallpaper_placeholder"))
                pick = QPushButton(t("settings.pick"), card)
                pick.clicked.connect(lambda _c=False, e=w: _pick_file(e, t))
                w.editingFinished.connect(lambda k=key, e=w: _apply(store, ctx, {k: e.text()}, t))
                box.addWidget(w)
                box.addWidget(pick)
                row.addLayout(box)
                cl.addLayout(row)
                widgets[key] = w
                continue
            row.addWidget(w)
            cl.addLayout(row)
            widgets[key] = w

    # 语言（**放在外观里**：机主要"装完就是本机语言"，这里也能手动改，即改即生效）
    card, cl = _add_card("settings.group.other")
    row = QHBoxLayout()
    row.addWidget(_label(t("settings.language"), tokens))
    lang = QComboBox(card)
    lang.addItem(t("settings.language_auto"), LOCALE_AUTO)
    for loc in LOCALES:
        lang.addItem(LOCALE_NAMES.get(loc, loc), loc)
    cur_loc = str(store.get("locale") or "") if store is not None else ""
    idx = lang.findData(cur_loc)
    lang.setCurrentIndex(idx if idx >= 0 else 0)
    lang.currentIndexChanged.connect(
        lambda _i, cb=lang: _apply(store, ctx, {"locale": cb.currentData()}, t))
    row.addWidget(lang)
    cl.addLayout(row)
    widgets["locale"] = lang

    # 预设：保存 / 加载 / 导入导出
    pre = GlassCard.make(tokens, name="settingsCard")
    pl = GlassCard.body(pre, tokens)
    pl.addWidget(_label(t("settings.presets"), tokens))
    from daedalus.ui.settings import FACTORY_PRESETS
    for name in FACTORY_PRESETS:
        btn = QPushButton(t("settings.load_preset", name=name), pre)
        btn.clicked.connect(lambda _c=False, n=name: _load_preset(store, ctx, n, t))
        pl.addWidget(btn)

    def _save_current():
        if store is None:
            return
        name, okk = QInputDialog.getText(pre, t("settings.save_preset_title"),
                                        t("settings.preset_name"))
        if okk and name.strip():
            store.save_preset(name.strip())
            _toast(ctx, t("toast.preset_saved", name=name.strip()))

    b1 = QPushButton(t("settings.save_preset"), pre)
    b1.clicked.connect(_save_current)
    b2 = QPushButton(t("settings.export"), pre)
    b2.clicked.connect(lambda: _export_presets(store, ctx, t))
    b3 = QPushButton(t("settings.import"), pre)
    b3.clicked.connect(lambda: _import_presets(store, ctx, t))
    for b in (b1, b2, b3):
        pl.addWidget(b)
    lay.insertWidget(max(0, lay.count() - 1), pre)
    page._widgets = widgets         # noqa: SLF001
    return page


def _apply(store, ctx, patch: dict, t) -> None:
    """设置变更的统一入口：校验 → 保存 → 让窗口重刷（**即改即存**）。"""
    from daedalus.ui.theme import ThemeError
    try:
        if store is not None:
            store.update(patch)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(patch)
    except ThemeError as e:
        _toast(ctx, t("toast.setting_rejected", why=e), error=True)


def _load_preset(store, ctx, name: str, t) -> None:
    from daedalus.ui.theme import ThemeError
    try:
        body = store.load_preset(name)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(body)
        _toast(ctx, t("toast.preset_loaded", name=name))
    except ThemeError as e:
        _toast(ctx, str(e), error=True)


def _export_presets(store, ctx, t) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getSaveFileName(None, t("dialog.export_presets"),
                                      "daedalus-presets.json", f"{t('dialog.json')} (*.json)")
    if p:
        _toast(ctx, t("toast.exported", n=store.export_presets(p)["count"]))


def _import_presets(store, ctx, t) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, t("dialog.import_presets"), "",
                                      f"{t('dialog.json')} (*.json)")
    if p:
        try:
            r = store.import_presets(p)
            _toast(ctx, t("toast.imported", n=r["imported"], skip=r["skipped"]))
        except Exception as e:
            _toast(ctx, t("toast.import_failed", why=e), error=True)


def _pick_file(edit, t) -> None:
    """背景图选文件：选完**立刻生效**（走 `editingFinished` → `_apply` → 重刷）。"""
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, t("dialog.pick_wallpaper"), "",
                                       f"{t('dialog.images')} (*.png *.jpg *.jpeg *.webp *.bmp)")
    if p:
        edit.setText(p)
        edit.editingFinished.emit()


def _toast(ctx, msg: str, *, error: bool = False) -> None:
    """提示出口：有 ctx 就交给它；没有就记日志（**不静默**）。"""
    if ctx is not None and hasattr(ctx, "notify"):
        try:
            ctx.notify(str(msg), error=error)
            return
        except Exception:
            pass
    logger.warning("界面提示：%s", msg)


# ── 关于 ─────────────────────────────────────────────────────────
def _about(window, tokens, t):
    page, lay = PageBase.make(tokens, name="about", parent=window)
    from daedalus import about
    info = about()
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(f"{info['name']}（{info['name_zh']} / {info['name_ja']}）v{info['version']}",
                        tokens))
    cl.addWidget(_label(info["tagline_zh"] if t.locale == "zh-CN" else
                        (info["tagline_ja"] if t.locale == "ja-JP" else info["tagline_en"]),
                        tokens, muted=True))
    cl.addWidget(_label(t("about.body1"), tokens))
    cl.addWidget(_label(t("about.body2"), tokens, muted=True))
    lay.insertWidget(0, card)
    return page

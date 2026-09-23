# -*- coding: utf-8 -*-
"""主窗口与启动：无边框 + 自绘标题栏 + 左侧导航（**设置固定底部**）+ 页面栈 + 签名

启动路径（`run_ui`）与测试路径（`build_headless`）共用 `MainWindow` —— 门禁在 offscreen
平台下把窗口真的建出来、真的切页、真的应用外观参数，而不是"相信它能跑"。

性能红线（设计系统的一部分，`tools/perf_probe.py` 会实测）：
    停顿 <200ms｜切页 <400ms｜最大化重排 <1200ms｜图像管线单次 <200MB
手法：页面懒建 + 渲染防抖 250ms + 忙时挂起 + 代数号守卫 + 底图缓存裁切复用
（见 `ui/render.py` 与 `ui/wallpaper.py`）。
"""

from __future__ import annotations

import logging
import pathlib
import time

logger = logging.getLogger(__name__)

__all__ = ["MainWindow", "UI", "run_ui", "build_headless", "MIN_W", "MIN_H", "START_W", "START_H"]

MIN_W, MIN_H = 980, 620
START_W, START_H = 1360, 860


class UI:
    """给页面用的上下文（引擎 + 设置 + 提示出口）。**窗口不直接碰引擎**，都从这里走。"""

    def __init__(self, *, engine=None, settings=None, data_root=None):
        self.engine = engine
        self.settings = settings
        self.data_root = pathlib.Path(str(data_root)) if data_root else None
        self.notes: list[str] = []
        self._on_change = None

    def notify(self, msg: str, *, error: bool = False) -> None:
        self.notes.append(("[错误] " if error else "") + str(msg))
        if len(self.notes) > 200:
            del self.notes[:100]
        logger.warning("界面提示：%s", msg) if error else logger.info("界面提示：%s", msg)

    def on_settings_changed(self, patch: dict) -> None:
        if self._on_change is not None:
            self._on_change(dict(patch))

    def set_change_handler(self, fn) -> None:
        self._on_change = fn

    def refresh(self):
        """拉一次引擎指标（引擎没起就返回空）。"""
        if self.engine is None:
            return {}
        try:
            return self.engine.metrics()
        except Exception as e:
            self.notify(f"指标读取失败：{type(e).__name__}: {e}", error=True)
            return {}


class MainWindow:
    """主窗口：无边框、自绘标题栏、左导航（功能在上、设置固定底部）、右下角签名。"""

    @staticmethod
    def make(*, ctx: UI, tokens, app=None):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                                       QStackedWidget, QVBoxLayout, QWidget)
        from daedalus.ui.i18n import translator
        from daedalus.ui.pages import build_pages, page_titles
        from daedalus.ui.widgets import (GlassCard, SignatureLabel, TitleBar, WallpaperWidget,
                                        apply_theme)
        from daedalus import display_name

        # 语言：**已由调用方解析好**（设置 > 安装器选择 > 系统 > en-US，见 ui/i18n.py）
        locale = str(getattr(tokens, "locale", "") or "en-US")
        t = translator(locale)

        win = QWidget()
        win.setObjectName("mainWindow")
        win.setWindowTitle(f"{display_name(locale)} · {t('app.title')}")
        win.setMinimumSize(MIN_W, MIN_H)
        win.resize(START_W, START_H)
        win.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)

        stack_root = QVBoxLayout(win)
        stack_root.setContentsMargins(0, 0, 0, 0)
        stack_root.setSpacing(0)

        # ① 底图层（最底下的自立控件；内容都画在它上面）
        bg = WallpaperWidget.make(tokens, win)
        bg.setGeometry(0, 0, START_W, START_H)

        # ② 标题栏
        title, title_buttons = TitleBar.make(tokens, win,
                                            title=f"{display_name(locale)} "
                                                  f"v{__import__('daedalus').VERSION}")
        stack_root.addWidget(title)

        # ③ 主体：左导航 + 页面栈
        body = QWidget(win)
        body.setObjectName("bodyRow")
        body.setAutoFillBackground(False)
        row = QHBoxLayout(body)
        sp = tokens.spacing()
        row.setContentsMargins(sp["margin"] // 2, sp["gap"], sp["margin"] // 2, sp["gap"])
        row.setSpacing(sp["gap"])

        nav = GlassCard.make(tokens, name="navPanel")
        nav.setFixedWidth(190)
        nav_l = GlassCard.body(nav, tokens)
        stack = QStackedWidget(body)
        stack.setObjectName("pageStack")
        stack.setAutoFillBackground(False)

        pages = build_pages(win, tokens, ctx)
        order: list[str] = []
        buttons: dict[str, QPushButton] = {}
        labels: dict[str, str] = dict(page_titles(t))

        def _add_nav(key: str, label: str, *, suffix: str = "") -> None:
            stack.addWidget(pages[key])
            order.append(key)
            b = QPushButton(label + suffix, nav)
            b.setObjectName(f"nav_{key}")
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setChecked(not order[1:])                 # 第一项默认选中
            b.setStyleSheet(_nav_style(tokens, b.objectName()))
            b.clicked.connect(lambda _c=False, k=key: _goto(k))
            nav_l.addWidget(b)
            buttons[key] = b

        # 功能项在上（概览/任务/日志）→ stretch → 关于 → **设置固定最下面**
        for key, label in page_titles(t):
            if key in ("settings", "about"):
                continue
            _add_nav(key, label)
        nav_l.addStretch(1)                             # ← 把下面两项压到底部
        _add_nav("about", labels["about"])
        _add_nav("settings", labels["settings"], suffix="  ⚙")

        row.addWidget(nav)
        row.addWidget(stack, 1)
        stack_root.addWidget(body, 1)

        sig = SignatureLabel.make(tokens, win)

        state = {"page": order[0], "switch_ms": {}, "applied": {}, "tokens": tokens}

        def _goto(key: str) -> None:
            t0 = time.monotonic()
            idx = order.index(key) if key in order else 0
            stack.setCurrentIndex(idx)
            if key in buttons:
                buttons[key].setChecked(True)
            state["page"] = key
            state["switch_ms"][key] = round((time.monotonic() - t0) * 1000, 3)
            ctx.settings.set("page", key) if ctx.settings is not None else None

        def _sync_vars() -> dict:
            """窗口几何变化后同步子层（底图铺满、签名贴右下）。"""
            r = win.rect()
            bg.setGeometry(0, 0, r.width(), r.height())
            sig.adjustSize()
            sig.move(max(0, r.width() - sig.width() - 16),
                     max(0, r.height() - sig.height() - 10))
            sig.raise_()
            return {"w": r.width(), "h": r.height()}

        def _on_resize(event) -> None:
            _sync_vars()
            if state.get("sched") is not None:
                state["sched"].request({"reason": "resize"}, reason="resize")

        def _on_show(event) -> None:
            _sync_vars()
            sig.raise_()

        win.resizeEvent = _on_resize
        win.showEvent = _on_show
        win.changeEvent = lambda ev: (sig.raise_(), bg.lower()) if ev.type() in (
            ev.Type.ActivationChange, ev.Type.WindowStateChange) else None

        win.paintEvent = lambda ev: WallpaperWidget.paint(bg, ev)

        win._ui = {"ctx": ctx, "tokens": tokens, "pages": pages, "order": order,
                   "stack": stack, "nav": nav, "nav_layout": nav_l, "buttons": buttons,
                   "bg": bg, "sig": sig,
                   "title": title, "state": state, "goto": _goto, "sync": _sync_vars,
                   "apply_theme": apply_theme}          # noqa: SLF001

        # 应用外观（**所有卡片走同一个生成器**）
        info = win._ui                                  # noqa: SLF001
        info["applied"] = apply_theme(win, tokens)
        _sync_vars()
        # 渲染调度：底图重算走防抖 + 代数号守卫
        from daedalus.ui.render import RenderScheduler
        state["sched"] = RenderScheduler(debounce_ms=tokens.debounce_ms)
        info["sched"] = state["sched"]              # 对外用 `_ui["sched"]`（探针/门禁都读它）
        return win

    # ── 运行时操作（门禁与真实使用共用）─────────────────────────
    @staticmethod
    def apply_tokens(win, tokens) -> dict:
        """换一套令牌并**就地重刷**（主题/强调色/玻璃参数都走这里）。"""
        info = win._ui                                  # noqa: SLF001
        info["tokens"] = tokens
        from daedalus.ui.pages import build_pages
        info["applied"] = info["apply_theme"](win, tokens)
        # 导航按钮样式跟着换
        from PySide6.QtWidgets import QPushButton
        for key, btn in info["buttons"].items():
            btn.setStyleSheet(_nav_style(tokens, btn.objectName()))
        info["sig"].setVisible(bool(tokens.signature))
        info["sig"].setStyleSheet(f"color: {tokens.accent}; background: transparent;")
        info["sync"]()
        return dict(info["applied"])

    @staticmethod
    def wire_context(win, store, ctx=None) -> dict:
        """把"设置一变 → 界面跟着变"这条线**接一次**（两条启动路径共用，避免漂移）。

        走查发现的两个真问题（都是"组件都对、接线没接"，测组件测不出来）：
          * `build_headless` 根本没人装变更处理器 → **所有设置只存了盘、界面纹丝不动**；
          * **背景图管线与自动取色从来没有任何代码去触发**（设置页只存路径，没人加载底图、没人取色）。

        所以这里按顺序接好四件事：
          ① `locale` 变 → **整页重建**（只重刷样式会留下半旧语言）；
          ② 底图相关（路径/模糊/蒙层/焦点/下采样）→ **跑图像管线**（缓存与代数号守卫都在管线里）；
          ③ `accent` / `accent_locked` → 未锁定时按底图**自动取色**；锁定时原样保留；
          ④ 其余 → 重刷令牌。
        任何一步失败都**如实提示**，绝不静默；坏图回退纯色且不崩。
        """
        info = win._ui                                   # noqa: SLF001
        ctx = ctx or info["ctx"]

        def _on_change(patch: dict) -> None:
            patch = dict(patch or {})
            from daedalus.ui.i18n import resolve_locale
            if "locale" in patch:
                MainWindow.apply_locale(win, resolve_locale(patch.pop("locale")))
            try:
                tokens_now = store.to_tokens()
            except Exception as e:
                ctx.notify(f"外观参数未生效：{e}", error=True)
                return
            bg_changed = any(k in patch for k in ("wallpaper", "blur", "dim_manual", "focus",
                                                 "downsample_max"))
            if bg_changed:
                MainWindow.apply_wallpaper_from_settings(win, store, tokens_now)
            # 自动取色只在两种情况下跑：**换了底图** 或 **刚解除锁定**。
            # ⚠️ 用户显式选了强调色（`accent` 在 patch 里）时**绝不能覆盖**——那是明确选择；
            #    走查抓到的 bug 就是"我选了颜色它自己变回去了"（自动取色盖掉显式选择）。
            unlocked = not store.get("accent_locked")
            if unlocked and (bg_changed or ("accent_locked" in patch)):
                MainWindow.sync_accent_from_wallpaper(win, store, tokens_now)
            MainWindow.apply_tokens(win, store.to_tokens())   # 取色可能刚改了 accent

        ctx.set_change_handler(_on_change)
        info["change_handler"] = _on_change
        # 启动时先按**已保存的设置**把底图与取色跑一遍（否则存档里的底图要等用户再点一次才出现）
        MainWindow.apply_wallpaper_from_settings(win, store, store.to_tokens())
        MainWindow.sync_accent_from_wallpaper(win, store, store.to_tokens())
        MainWindow.apply_tokens(win, store.to_tokens())
        return {"wired": True}

    @staticmethod
    def apply_wallpaper_from_settings(win, store, tokens) -> dict:
        """按设置里的路径跑**图像管线**并把结果贴到窗口上（失败如实提示、回退纯色，不崩）。"""
        info = win._ui                                   # noqa: SLF001
        ctx = info["ctx"]
        path = str(store.get("wallpaper") or "").strip()
        if not path:
            MainWindow.set_wallpaper(win, None, {})
            return {"wallpaper": "", "meta": {}}
        try:
            from daedalus.ui.wallpaper import WallpaperCache
            cache = info.get("wp_cache")
            if cache is None:
                cache = WallpaperCache()
                info["wp_cache"] = cache
            q, meta = cache.get(path, width=max(320, win.width()), height=max(240, win.height()),
                                blur=int(store.get("blur") or 0),
                                dim_manual=float(store.get("dim_manual") or 0),
                                focus=str(store.get("focus") or "center"),
                                max_edge=int(store.get("downsample_max") or 2560))
            MainWindow.set_wallpaper(win, q, meta)
            info["state"]["wp_stats"] = cache.stats()
            return {"wallpaper": path, "meta": meta}
        except Exception as e:
            MainWindow.set_wallpaper(win, None, {})      # 坏图/被删 → 回退纯色
            ctx.notify(f"底图不可用（已回退纯色）：{type(e).__name__}: {e}", error=True)
            return {"wallpaper": path, "error": f"{type(e).__name__}: {e}"}

    @staticmethod
    def sync_accent_from_wallpaper(win, store, tokens) -> dict:
        """未锁定时按底图**自动取色**（锁定就原样保留，不算、不覆盖）。"""
        info = win._ui                                   # noqa: SLF001
        path = str(store.get("wallpaper") or "").strip()
        if not path or bool(store.get("accent_locked")):
            return {"accent": str(store.get("accent") or ""), "source": "locked-or-none"}
        try:
            from daedalus.ui.wallpaper import extract_accent
            got = extract_accent(path, lock=None)
            if got.get("accent") and got["accent"] != store.get("accent"):
                store.set("accent", got["accent"])       # 即改即存
            info["state"]["accent_source"] = got.get("source", "")
            return got
        except Exception as e:
            info["ctx"].notify(f"自动取色失败（保留当前强调色）：{type(e).__name__}: {e}",
                              error=True)
            return {"accent": str(store.get("accent") or ""), "source": "error"}

    @staticmethod
    def apply_locale(win, locale: str) -> dict:
        """**换语言 = 整页重建**（不是重刷样式）。

        为什么必须重建：语言一变，导航文字、页面标题、设置项标签、表头、提示语全都得换——
        只重刷 QSS 会留下"标题日文、导航中文"的半截状态。重建的代价很小（都是本地控件），
        换来的是**不会出现混语言界面**（机主点名的"愚蠢问题"就是这个）。
        """
        info = win._ui                                   # noqa: SLF001
        from daedalus import VERSION, display_name
        from daedalus.ui.i18n import translator
        from daedalus.ui.pages import build_pages, page_titles
        t = translator(locale)
        if hasattr(info["tokens"], "locale"):
            info["tokens"].locale = locale               # 令牌是可变 dataclass（刻意如此）
        stack = info["stack"]
        while stack.count():                             # 清掉旧页（Qt 会接管对象生命周期）
            w = stack.widget(0)
            stack.removeWidget(w)
            w.setParent(None)
            w.deleteLater()
        new_pages = build_pages(win, info["tokens"], info["ctx"])
        order: list[str] = []
        for key, _label in page_titles(t):               # 顺序与 `make` 一致：功能项 → 关于 → 设置
            if key in ("about", "settings"):
                continue
            stack.addWidget(new_pages[key])
            order.append(key)
        stack.addWidget(new_pages["about"])
        order.append("about")
        stack.addWidget(new_pages["settings"])
        order.append("settings")
        info["pages"], info["order"] = new_pages, order
        for key, btn in info["buttons"].items():         # 导航文字（含设置那枚 ⚙）
            btn.setText(t(f"nav.{key}") + ("  ⚙" if key == "settings" else ""))
        win.setWindowTitle(f"{display_name(locale)} · {t('app.title')}")
        info["sig"].setText(t("app.signature", name=display_name(locale), version=VERSION))
        cur = info["state"].get("page")
        info["goto"](cur if cur in order else order[0])
        return {"locale": locale, "order": order,
                "title": win.windowTitle(), "nav": [b.text() for b in info["buttons"].values()]}

    @staticmethod
    def goto(win, key: str) -> float:
        info = win._ui                                  # noqa: SLF001
        info["goto"](key)
        return float(info["state"]["switch_ms"].get(key, 0.0))

    @staticmethod
    def set_wallpaper(win, image, meta: dict | None = None) -> None:
        """设置底图（QImage；None = 回退纯色）。**只在主线程调**。"""
        from daedalus.ui.widgets import WallpaperWidget
        info = win._ui                                  # noqa: SLF001
        WallpaperWidget.set_image(info["bg"], image)
        info["state"]["wallpaper_meta"] = dict(meta or {})
        info["sync"]()

    @staticmethod
    def refresh_pages(win, metrics: dict | None = None) -> dict:
        """把最新指标刷进概览页；顺带把任务表填上（含**指纹列**）。"""
        info = win._ui                                  # noqa: SLF001
        pages = info["pages"]
        m = dict(metrics or {})
        s = dict(m.get("summary") or {})
        from daedalus.ui.widgets import StatCard
        cards = getattr(pages["overview"], "_stat_cards", [])
        if len(cards) == 4:
            StatCard.set_value(cards[0], f"{s.get('pages_per_sec', 0):.1f}")
            StatCard.set_value(cards[1], f"{s.get('mb_per_sec', 0):.2f}")
            StatCard.set_value(cards[2], f"{int(s.get('tasks_done', 0))}/"
                                        f"{int(s.get('tasks_failed', 0))}")
            StatCard.set_value(cards[3], f"{s.get('net_latency_p95', 0) * 1000:.0f} ms")
        EngineApp_ = None
        _fill_task_table(win, m.get("tasks") or [])
        return s

    @staticmethod
    def stats(win) -> dict:
        info = win._ui                                  # noqa: SLF001
        st = info["sched"].stats() if info.get("sched") else {}
        return {"page": info["state"]["page"], "order": list(info["order"]),
                "applied": dict(info["applied"]), "switch_ms": dict(info["state"]["switch_ms"]),
                "scheduler": st, "wallpaper_meta": dict(info["state"].get("wallpaper_meta") or {}),
                "size": [win.width(), win.height()],
                "signature_visible": bool(info["sig"].isVisible())}


def _fill_task_table(win, rows: list) -> None:
    """填任务表（含**指纹列**）。行数有界（只显示最近 N 条），控件树不重建。

    放在类**外面**：它是模块级辅助函数，插进类中间会把后面 `@staticmethod` 的方法
    吞进它的函数体里（S10 门禁 B3 当场报 `MainWindow has no attribute 'stats'`）。
    """
    try:
        table = getattr(win._ui["pages"]["tasks"], "_table", None)     # noqa: SLF001
        if table is None:
            return
        rows = list(rows)[:200]
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            fp = str(r.get("content_hash") or "")
            values = [str(r.get("state") or ""), str(r.get("target") or "")[:80],
                      f"{r.get('attempts', 0)}/{r.get('throttles', 0)}/{r.get('transitions', 0)}",
                      str(r.get("evidence_n", 0)), fp[:16] or "—", str(r.get("bytes_done", 0))]
            from PySide6.QtWidgets import QTableWidgetItem
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if table.item(i, c) is None:
                    table.setItem(i, c, item)          # 只在空位新建控件（不重建）
                else:
                    table.item(i, c).setText(v)        # 有就改文本
        table.resizeColumnsToContents()
    except Exception as e:
        logger.debug("任务表刷新失败：%s", e)


def _nav_style(tokens, object_name: str) -> str:
    from daedalus.ui.theme import status_colors, mix_hex
    st = status_colors(tokens.light)
    sp = tokens.spacing()
    return (f"QPushButton#{object_name} {{ text-align: left; padding: {sp['inner_top']}px "
            f"{sp['inner_bottom']}px; border: 1px solid transparent; border-radius: "
            f"{tokens.radius // 2}px; background: transparent; color: {st['muted']}; }}\n"
            f"QPushButton#{object_name}:hover {{ background: rgba(128,128,128,40); }}\n"
            f"QPushButton#{object_name}:checked {{ color: {tokens.accent}; "
            f"border: 1px solid {mix_hex(tokens.accent, '#FFFFFF', 0.4)}; "
            f"background: rgba(128,128,128,30); }}")


def build_headless(*, engine=None, data_root=None, tokens=None):
    """在**无显示器**环境把窗口真建出来（门禁用；`QT_QPA_PLATFORM=offscreen`）。"""
    from PySide6.QtWidgets import QApplication
    from daedalus.ui.settings import SettingsStore
    from daedalus.ui.theme import tokens as make_tokens
    app = QApplication.instance() or QApplication([])
    root = pathlib.Path(str(data_root or (pathlib.Path.home() / ".daedalus_ui_probe")))
    root.mkdir(parents=True, exist_ok=True)
    store = SettingsStore(root)
    t = tokens or store.to_tokens()
    ctx = UI(engine=engine, settings=store, data_root=root)
    win = MainWindow.make(ctx=ctx, tokens=t, app=app)
    MainWindow.wire_context(win, store, ctx)      # ← 接线（两条路径共用，见 wire_context 的说明）
    return {"app": app, "window": win, "ctx": ctx, "settings": store, "tokens": t}


def run_ui(*, cfg: dict | None = None, data_root=None, start_engine: bool = True) -> int:
    """真正的启动入口（GUI 进程）：建窗口 → 可选启动引擎 → 进事件循环。"""
    from PySide6.QtWidgets import QApplication
    from daedalus.ui.settings import SettingsStore
    app = QApplication.instance() or QApplication([])
    root = pathlib.Path(str(data_root or pathlib.Path.home() / "Daedalus"))
    store = SettingsStore(root)
    engine = None
    if start_engine:
        try:
            from daedalus.core.app import EngineApp
            engine = EngineApp.build(cfg, data_root=root, enable_browser=False)
        except Exception as e:
            logger.error("引擎启动失败（界面仍可用）：%s", e)
    ctx = UI(engine=engine, settings=store, data_root=root)
    win = MainWindow.make(ctx=ctx, tokens=store.to_tokens(), app=app)
    # 设置一变就重刷（语言→整页重建；底图→跑管线；强调色→自动取色；其余→重刷令牌）
    MainWindow.wire_context(win, store, ctx)
    win.show()
    try:
        code = app.exec()
    finally:
        if engine is not None:
            try:
                engine.shutdown()
            except Exception as e:
                logger.warning("引擎收尾失败：%s", e)
    return int(code)

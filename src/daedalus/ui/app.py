# -*- coding: utf-8 -*-
"""主窗口与启动：无边框 + 自绘标题栏 + 左侧导航（**设置固定底部**）+ 页面栈 + 签名

启动路径（`run_ui`）与测试路径（`build_headless`）共用 `MainWindow` —— 门禁在 offscreen
平台下把窗口真的建出来、真的切页、真的应用外观参数，而不是「相信它能跑」。

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
    """主窗口：**库的 FluendWindow 骨架**（48px 标题栏 + 可折叠导航 + 300ms 切页动画）
    + 我们自己的底图绘制 + 右下角签名。

    为什么要换骨架（原来是自己画的标题栏 + 玻璃卡片导航）：自绘的那套**没有任何动效**
    （切页是硬切、导航是硬切、悬停是硬切），机主的原话是「太硬了、一点动向都没有」。
    FluentWindow 自带的导航展开（150ms）、切页（300ms InQuad）、全控件悬停（120ms）
    正是设计系统报告里那句「其余交给 Fluent 组件库」的落点。
    """

    @staticmethod
    def make(*, ctx: UI, tokens, app=None):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QColor, QPainter
        from PySide6.QtWidgets import QApplication, QWidget
        from qfluentwidgets import (FluentIcon, FluentWindow, NavigationItemPosition,
                                    Theme, setTheme, setThemeColor)
        from daedalus import VERSION, display_name
        from daedalus.ui.i18n import translator
        from daedalus.ui.pages import build_pages, page_titles
        from daedalus.ui.widgets import SignatureLabel, WallpaperWidget, apply_theme

        # 语言：**已由调用方解析好**（设置 > 安装器选择 > 系统 > en-US，见 ui/i18n.py）
        locale = str(getattr(tokens, "locale", "") or "en-US")
        t = translator(locale)

        class _Win(FluentWindow):
            """库窗口 + 我们自己的背景：**不调 `super().paintEvent()`**——库会把样式背景
            画在上面，把底图盖掉；所以背景由我们全权负责（有底图画底图，没有就填主题色）。"""

            def paintEvent(self, ev):                        # noqa: N802 - Qt 命名
                if WallpaperWidget.paint(self, ev):
                    return
                p = QPainter(self)
                p.fillRect(self.rect(), QColor(str(getattr(self, "_solid", "#0B0F14"))))
                p.end()

            def _geometry_changed(self) -> None:
                cb = getattr(self, "_on_geometry", None)
                if cb:
                    cb()

            def resizeEvent(self, ev):                       # noqa: N802
                super().resizeEvent(ev)
                self._geometry_changed()

            def showEvent(self, ev):                         # noqa: N802
                super().showEvent(ev)
                self._geometry_changed()

            def changeEvent(self, ev):                       # noqa: N802
                super().changeEvent(ev)
                if ev.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
                    self._geometry_changed()

        win = _Win()
        win.setObjectName("mainWindow")
        win.setWindowTitle(f"{display_name(locale)} · {t('app.title')}")
        win.setMinimumSize(MIN_W, MIN_H)
        win.resize(START_W, START_H)
        win._solid = tokens.bg_solid()                        # noqa: SLF001
        WallpaperWidget.tokens_of(win, tokens)                # 底图动效要读令牌

        icons = {"overview": FluentIcon.HOME, "tasks": FluentIcon.DOCUMENT,
                 "logs": FluentIcon.SCROLL, "about": FluentIcon.INFO,
                 "settings": FluentIcon.SETTING}
        labels = dict(page_titles(t))
        pages = build_pages(win, tokens, ctx)
        order: list[str] = []
        nav_items: dict[str, object] = {}

        for key, label in page_titles(t):                     # 功能项在上
            if key in ("about", "settings"):
                continue
            nav_items[key] = win.addSubInterface(pages[key], icons.get(key, FluentIcon.HOME),
                                                 label, NavigationItemPosition.TOP)
            order.append(key)
        # **设置固定最下面**：库的 BOTTOM 组是「后加的在上」，所以先加关于、再加设置
        nav_items["about"] = win.addSubInterface(pages["about"], icons["about"],
                                                labels.get("about", "About"),
                                                NavigationItemPosition.BOTTOM)
        nav_items["settings"] = win.addSubInterface(pages["settings"], icons["settings"],
                                                   labels.get("settings", "Settings"),
                                                   NavigationItemPosition.BOTTOM)
        order += ["settings", "about"]
        # 导航**启动就是展开态**（带文字的导航才叫「排版」；库默认是 48px 图标条，
        # 只有图标、没有文字，看着很空）。展开宽度照 Kiana 的 312；菜单按钮留着让用户能折叠。
        try:
            win.navigationInterface.setExpandWidth(312)
            win.navigationInterface.setCollapsible(True)
            win.navigationInterface.setMenuButtonVisible(True)
            win.navigationInterface.expand(useAni=False)
        except Exception as e:
            logger.debug("导航展开失败（保持库默认的图标条）：%s", e)

        sig = SignatureLabel.make(tokens, win)
        state = {"page": order[0], "switch_ms": {}, "applied": {}, "tokens": tokens}

        def _goto(key: str) -> None:
            # ⚠️ 必须每次从 `win._ui["pages"]` 现读：语言切换会**整页重建**，
            #    闭包里抓着的旧字典指向的是已经被摘掉的旧页（实测 `switchTo` 报 index -1）。
            t0 = time.monotonic()
            page = (getattr(win, "_ui", None) or {}).get("pages", {}).get(key)
            if page is not None:
                win.switchTo(page)                 # 库自带 300ms 淡入淡出（动效源头）
            state["page"] = key
            state["switch_ms"][key] = round((time.monotonic() - t0) * 1000, 3)
            if ctx.settings is not None:
                ctx.settings.set("page", key)

        def _sync_vars() -> dict:
            """窗口几何变化后同步子层（签名贴右下 + 底图重绘）。"""
            r = win.rect()
            sig.adjustSize()
            sig.move(max(0, r.width() - sig.width() - 16),
                     max(0, r.height() - sig.height() - 10))
            sig.raise_()
            return {"w": r.width(), "h": r.height()}

        def _on_change_handler(event=None) -> None:
            _sync_vars()
            if state.get("sched") is not None:
                state["sched"].request({"reason": "geometry"}, reason="resize")

        win._on_geometry = _on_change_handler                  # noqa: SLF001

        win._ui = {"ctx": ctx, "tokens": tokens, "pages": pages, "order": order,
                   "stack": getattr(win, "stackedWidget", None), "nav": win.navigationInterface,
                   "nav_items": nav_items, "buttons": nav_items, "bg": win, "sig": sig,
                   "state": state, "goto": _goto, "sync": _sync_vars,
                   "apply_theme": apply_theme, "icons": icons}   # noqa: SLF001

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
        """换一套令牌并**就地重刷**（主题/强调色/玻璃参数都走这里）。

        导航样式不在这里刷了——导航是库的 `NavigationInterface`（它自己跟着
        `setTheme`/`setThemeColor` 走 120ms 过渡）。这里只负责：令牌 → 样式表 + 底图色 + 签名。
        """
        info = win._ui                                  # noqa: SLF001
        info["tokens"] = tokens
        from daedalus.ui.widgets import WallpaperWidget
        info["applied"] = info["apply_theme"](win, tokens)
        win._solid = tokens.bg_solid()                  # noqa: SLF001 - 没底图时填的纯色
        WallpaperWidget.tokens_of(win, tokens)           # 底图动效（溶解/限帧）读这份令牌
        info["sig"].setVisible(bool(tokens.signature))
        info["sig"].setStyleSheet(f"color: {tokens.accent}; background: transparent;")
        info["sync"]()
        win.update()
        return dict(info["applied"])

    @staticmethod
    def wire_context(win, store, ctx=None) -> dict:
        """把"设置一变 → 界面跟着变"这条线**接一次**（两条启动路径共用，避免漂移）。

        走查发现的两个真问题（都是「组件都对、接线没接」，测组件测不出来）：
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
        MainWindow.start_metrics_poll(win, ctx)
        return {"wired": True}

    # 指标轮询：**默认 1 秒一拉**；单次超过 `SLOW_MS` 就自动降频（宁可少刷，不许卡手）
    POLL_MS = 1000
    SLOW_MS = 250          # 与"停顿 < 200ms"的性能红线同源；超了就说明这一拉太重
    POLL_MS_BACKOFF = 5000

    @staticmethod
    def start_metrics_poll(win, ctx) -> dict:
        """把指标**周期性**刷进概览页与任务表（走查抓到的第三个接线缺口）。

        真 bug：`refresh_pages()` 写好了、门禁也直接调它验收，但**全仓没有一个调用者**——
        真跑起来四张统计卡永远停在占位符「—」、任务表永远是空的。测组件的用例发现不了这个，
        所以这里配套加了门禁断言（「存在调用者」+「真拉一次真的变了」）。

        代价控制：`engine.metrics()` 里有 `tasks_overview(limit=50)`（一次读库）。
        所以 ① 只有拿到引擎才拉；② 记下每次耗时，> `SLOW_MS` 就**自动降到 5 秒一拉**并如实记在
        `_ui["poll"]` 里（门禁与探针都读它）——绝不为了「数字好看」把界面卡住。
        """
        from PySide6.QtCore import QTimer
        info = win._ui                                   # noqa: SLF001
        rec = {"count": 0, "last_ms": 0.0, "max_ms": 0.0, "slow": 0, "errors": 0,
               "interval_ms": MainWindow.POLL_MS, "last_summary": {}}
        info["poll"] = rec

        def _poll() -> None:
            import time as _t
            t0 = _t.perf_counter()
            try:
                m = ctx.refresh()                        # 走 ctx（页面不直连引擎，窗口也不）
                if m:
                    rec["last_summary"] = dict(m.get("summary") or {})
                    MainWindow.refresh_pages(win, m)
            except Exception as e:                       # 读数失败不该让界面崩，但也**不静默**
                rec["errors"] += 1
                logger.debug("指标刷新失败：%s", e)
            dt = (_t.perf_counter() - t0) * 1000.0
            rec["count"] += 1
            rec["last_ms"] = dt
            rec["max_ms"] = max(rec["max_ms"], dt)
            if dt > MainWindow.SLOW_MS:
                rec["slow"] += 1
                if rec["interval_ms"] != MainWindow.POLL_MS_BACKOFF and timer is not None:
                    rec["interval_ms"] = MainWindow.POLL_MS_BACKOFF
                    timer.setInterval(MainWindow.POLL_MS_BACKOFF)
                    logger.warning("指标刷新单次耗时 %.0fms（超过红线 %dms）→ 自动降频到 %dms",
                                   dt, MainWindow.SLOW_MS, MainWindow.POLL_MS_BACKOFF)

        timer = None
        try:
            timer = QTimer(win)
            timer.setInterval(MainWindow.POLL_MS)
            timer.timeout.connect(_poll)
            timer.start()
        except Exception as e:                           # 无头环境没有事件循环也照样能用（门禁直接调 _poll）
            logger.debug("指标定时器没起来（无事件循环？）：%s", e)
        info["poll_fn"] = _poll                          # 门禁/走查直接调它，不必等 1 秒
        info["poll_timer"] = timer
        _poll()                                          # 立刻拉一次，别让用户先看一秒占位符
        return rec

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
        只重刷 QSS 会留下「标题日文、导航中文」的半截状态。重建的代价很小（都是本地控件），
        换来的是**不会出现混语言界面**（机主点名的「愚蠢问题」就是这个）。
        """
        info = win._ui                                   # noqa: SLF001
        from daedalus import VERSION, display_name
        from daedalus.ui.i18n import translator
        from daedalus.ui.pages import build_pages, page_titles
        from qfluentwidgets import NavigationItemPosition
        t = translator(locale)
        if hasattr(info["tokens"], "locale"):
            info["tokens"].locale = locale               # 令牌是可变 dataclass（刻意如此）
        # 摘旧页时**屏蔽页栈信号**：库的 `_onCurrentInterfaceChanged` 会在 currentWidget 变 None
        # 时抛 `AttributeError`（库内部没做 None 判断），刷一地红色栈回溯但功能不受影响。
        sw = getattr(win, "stackedWidget", None)
        try:
            if sw is not None:
                sw.blockSignals(True)
            for old in list(info["pages"].values()):     # 先把旧页从导航上摘掉
                try:
                    win.removeInterface(old, isDelete=True)
                except Exception as e:
                    logger.debug("摘旧页失败（继续）：%s", e)
        finally:
            if sw is not None:
                sw.blockSignals(False)
        new_pages = build_pages(win, info["tokens"], info["ctx"])
        icons = info.get("icons") or {}
        labels = dict(page_titles(t))
        order: list[str] = []
        nav_items: dict[str, object] = {}
        for key, label in page_titles(t):                # 顺序与 `make` 一致：功能项 → 设置 → 关于
            if key in ("about", "settings"):
                continue
            nav_items[key] = win.addSubInterface(new_pages[key],
                                                 icons.get(key), label,
                                                 NavigationItemPosition.TOP)
            order.append(key)
        nav_items["about"] = win.addSubInterface(new_pages["about"], icons.get("about"),
                                                 labels.get("about", "About"),
                                                 NavigationItemPosition.BOTTOM)
        nav_items["settings"] = win.addSubInterface(new_pages["settings"], icons.get("settings"),
                                                    labels.get("settings", "Settings"),
                                                    NavigationItemPosition.BOTTOM)
        order += ["settings", "about"]
        info["pages"], info["order"] = new_pages, order
        info["nav_items"] = nav_items
        info["buttons"] = nav_items                      # 兼容旧键名（门禁/走查读它）
        win.setWindowTitle(f"{display_name(locale)} · {t('app.title')}")
        info["sig"].setText(t("app.signature", name=display_name(locale), version=VERSION))
        cur = info["state"].get("page")
        info["goto"](cur if cur in order else order[0])
        return {"locale": locale, "order": order, "title": win.windowTitle(),
                "nav": [str(labels.get(k, k)) for k in order]}

    @staticmethod
    def goto(win, key: str) -> float:
        info = win._ui                                  # noqa: SLF001
        info["goto"](key)
        return float(info["state"]["switch_ms"].get(key, 0.0))

    @staticmethod
    def set_wallpaper(win, image, meta: dict | None = None) -> None:
        """设置底图（QImage；None = 回退纯色）。**只在主线程调**。

        走 `WallpaperWidget.set_image`：换图时会有 **450ms 交叉溶解**（动效总开关管着），
        溶解信息记在 `state["wallpaper_fade"]` 里，门禁/探针可查。
        """
        from daedalus.ui.widgets import WallpaperWidget
        info = win._ui                                  # noqa: SLF001
        info["state"]["wallpaper_fade"] = WallpaperWidget.set_image(info["bg"], image)
        info["state"]["wallpaper_meta"] = dict(meta or {})
        info["sync"]()
        win.update()

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
        # 快速采集的"运行中 N 秒"也在这一波里刷（不然状态会一直停在初始值）
        try:
            fn = getattr(pages["overview"], "_refresh_collect", None)
            if callable(fn):
                fn()
        except Exception as e:
            logger.debug("采集状态刷新失败：%s", e)
        # 任务表空/非空时切换空态提示（空表旁边总要有一句话解释「为什么空」）
        try:
            rows = list(m.get("tasks") or [])
            empty = getattr(pages["tasks"], "_empty_label", None)
            if empty is not None:
                empty.setVisible(not rows)
            # 任务页三个操作的可用状态也跟着同步：引擎起得比窗口晚时，按钮不该永远是灰的
            tools = getattr(pages["tasks"], "_tools", None)
            if isinstance(tools, dict) and callable(tools.get("sync")):
                tools["sync"]()
        except Exception as e:
            logger.debug("任务空态/工具行同步失败：%s", e)
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
            # 把 task_id 挂在第 0 列的 UserRole 上：双击下钻读的就是它（pages.py 的约定）
            from PySide6.QtCore import Qt as _Qt
            first = table.item(i, 0)
            if first is not None:
                first.setData(_Qt.ItemDataRole.UserRole, str(r.get("task_id") or ""))
        table.resizeColumnsToContents()
    except Exception as e:
        logger.debug("任务表刷新失败：%s", e)


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

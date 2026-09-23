# -*- coding: utf-8 -*-
"""控件层：**玻璃卡片 / 统计卡 / 页面基类 / 底图窗口 / 自绘标题栏 / 签名**

三条必须守住的实现细节（都是「看起来能用、实则糊/裂/花」的坑）：
  1) **透明皮肤三件套**（页面基类）：QSS 用 `objectName` 限定 + `setAutoFillBackground(False)`
     在 `setWidget()` **前后各调一次** + 卡片样式由**同一个函数**生成。少一步就会出现
     「页面白底盖住底图」或「只有滚到某处才透明」这类玄学现象。
  2) **统计卡**：数字 26px/700 在上、灰色标签在下（信息层级固定，别一张卡一个样）。
  3) **签名**：右下角，颜色随强调色，`Show/Resize/Activate` 时 `raise_()`（否则会被后创建的
     子控件盖住——这是「签名突然不见了」的常见原因）。

底图窗口的绘制策略（性能）：缓存已处理好的图，尺寸不一致时**优先裁切**（见 `wallpaper.py`）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["GlassCard", "StatCard", "PageBase", "SignatureLabel", "WallpaperWidget",
           "TitleBar", "apply_theme", "PAGE_OBJECT"]

PAGE_OBJECT = "pageRoot"


def _qt():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget
    return Qt, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget


class GlassCard:
    """玻璃卡片的**工厂**（不是基类）：保证同页所有卡片走同一套参数。

    为什么用工厂：设计系统的铁律是「同页所有卡片由同一个函数生成」。做成基类容易被人
    顺手 override 几个属性，统一感就慢慢烂掉了。

    **悬停反馈（120ms）**：QSS 没有 transition，所以这里用 `QVariantAnimation` 插值一个
    0→1 的 hover 值，每帧重新生成一次 QSS（只重刷**这一张卡**，实测开销可忽略）。
    动效总开关关掉时直接落终值——**不是"不响应悬停"，而是"不做过渡"**。
    """

    @staticmethod
    def make(tokens, *, name: str = "card", parent=None):
        _, QFrame, _, _, _, _ = _qt()
        frame = QFrame(parent)
        frame.setObjectName(name)                       # QSS 靠 objectName 命中
        frame._tokens = tokens                          # noqa: SLF001 - 工厂产物，挂引用最省事
        frame._hover_t = 0.0                            # noqa: SLF001
        frame._hover_anim = None                        # noqa: SLF001
        frame.setStyleSheet(_card_style(tokens, name))
        frame.setAutoFillBackground(False)
        frame._set_hover = lambda v: GlassCard._apply_hover(frame, float(v))   # noqa: SLF001
        frame.enterEvent = lambda ev: GlassCard._hover_to(frame, 1.0)
        frame.leaveEvent = lambda ev: GlassCard._hover_to(frame, 0.0)
        return frame

    @staticmethod
    def _apply_hover(frame, v: float) -> None:
        """把插值出来的 hover 值落成样式（只动这一张卡）。"""
        frame._hover_t = v                              # noqa: SLF001
        frame.setStyleSheet(_card_style(frame._tokens, frame.objectName(), hover=v))  # noqa: SLF001

    @staticmethod
    def _hover_to(frame, target: float) -> None:
        tokens = getattr(frame, "_tokens", None)        # noqa: SLF001
        ms = tokens.hover_ms() if tokens is not None else 0
        cur = float(getattr(frame, "_hover_t", 0.0))    # noqa: SLF001
        if abs(cur - target) < 0.01:
            return
        if not ms:                                      # 动效关：立刻到终态
            GlassCard._apply_hover(frame, target)
            return
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation
            anim = QVariantAnimation(frame)
            anim.setDuration(ms)
            anim.setStartValue(cur)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(frame._set_hover)             # noqa: SLF001
            anim.finished.connect(lambda: setattr(frame, "_hover_anim", None))  # noqa: SLF001
            frame._hover_anim = anim                    # noqa: SLF001
            anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)
        except Exception as e:
            logger.debug("悬停动效没起来（回退为直接落值）：%s", e)
            GlassCard._apply_hover(frame, target)

    @staticmethod
    def body(frame, tokens, *, spacing: str = "inner"):
        """给卡片装上统一的内边距布局（返回那个 layout）。"""
        _, _, _, _, QVBoxLayout, _ = _qt()
        sp = tokens.spacing()
        lay = QVBoxLayout(frame)
        if spacing == "outer":
            lay.setContentsMargins(sp["margin"], sp["margin"], sp["margin"], sp["margin"])
            lay.setSpacing(sp["gap"])
        else:
            lay.setContentsMargins(sp["pad_x"], sp["pad_y"], sp["pad_x"], sp["pad_y"])
            lay.setSpacing(sp["inner_gap"] + 2)
        return lay


def _card_style(tokens, name: str, hover: float = 0.0) -> str:
    from daedalus.ui.theme import card_qss
    return card_qss(tokens, object_name=name, hover=hover)


class StatCard:
    """统计卡：数字在上（26px/700）、灰色标签在下。`set_value` 只更新数字。

    数字颜色用**主题文字色**，不用强调色——强调色是「当前位置/开关/主按钮」的语义色，
    整屏数字都染上它，重点就没了（强调色面积占比要 <5%）。
    `set_value(..., animate=True)` 会**滚数字**（360ms / OutCubic / 限帧），
    从旧值滚到新值；动效总开关关掉、或旧值不是数字时，直接落值。
    """

    @staticmethod
    def make(tokens, label: str, value: str = "—", *, name: str = "statCard", parent=None):
        Qt, _, QLabel, _, _, _ = _qt()
        from daedalus.ui.theme import text_color
        card = GlassCard.make(tokens, name=name, parent=parent)
        lay = GlassCard.body(card, tokens)
        num = QLabel(str(value), card)
        num.setObjectName(f"{name}Value")
        num.setStyleSheet(f"color: {text_color(tokens.light)}; font-size: 26px;"
                          f" font-weight: 700; background: transparent;")
        lab = QLabel(str(label), card)
        lab.setObjectName(f"{name}Label")
        st = _status(tokens)
        lab.setStyleSheet(f"color: {st['muted']}; font-size: {int(tokens.font_pt)}pt;"
                          f" background: transparent;")
        lay.addWidget(num)
        lay.addWidget(lab)
        card._value_label = num          # noqa: SLF001 - 工厂产物，直接挂引用最省事
        card._caption_label = lab        # noqa: SLF001
        card._roll_anim = None           # noqa: SLF001
        return card

    @staticmethod
    def set_value(card, value, note: str = "", *, animate: bool | None = None) -> None:
        """更新数字。`animate` 默认跟随令牌里的动效总开关。"""
        try:
            lab = card._value_label                                  # noqa: SLF001
            Text = str(value)
            if note:
                card._caption_label.setText(str(note))               # noqa: SLF001
            tokens = getattr(card, "_tokens", None)                   # noqa: SLF001
            want = bool(tokens.animations) if tokens is not None else False
            if animate is not None:
                want = bool(animate)
            ms = tokens.roll_ms() if tokens is not None else 0
            old = StatCard._number(lab.text())
            new = StatCard._number(Text)
            if not want or not ms or old is None or new is None or old == new:
                lab.setText(Text)
                return
            StatCard._roll(card, lab, old, new, Text, ms, tokens)
        except Exception as e:
            logger.debug("统计卡更新失败：%s", e)

    @staticmethod
    def _number(text: str) -> float | None:
        """从「12.3」「0/0」「42 ms」里抠出可滚动的数字；抠不出就返回 None。"""
        import re
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)", str(text))
        try:
            return float(m.group(1)) if m else None
        except Exception:
            return None

    @staticmethod
    def _roll(card, lab, old: float, new: float, final_text: str, ms: int, tokens) -> None:
        """把数字从 old 滚到 new；小数点位数沿用**目标文案**的写法。"""
        from PySide6.QtCore import QEasingCurve, QElapsedTimer, QVariantAnimation
        import re
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)(.*)$", final_text, re.S)
        head, tail = m.group(1), m.group(2)
        decimals = len(head.split(".")[1]) if "." in head else 0
        last = QElapsedTimer()
        last.start()
        frame_min = tokens.frame_min_ms() if tokens is not None else 33

        def _tick(v):
            if last.elapsed() < frame_min:      # 限帧：别让全窗重绘跟着每个中间值跑
                return
            last.restart()
            lab.setText(f"{float(v):.{decimals}f}{tail}")

        try:
            anim = QVariantAnimation(card)
            anim.setDuration(int(ms))
            anim.setStartValue(float(old))
            anim.setEndValue(float(new))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(_tick)
            anim.finished.connect(lambda: lab.setText(final_text))
            card._roll_anim = anim              # noqa: SLF001
            anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)
        except Exception as e:
            logger.debug("数字滚动没起来（直接落值）：%s", e)
            lab.setText(final_text)


def _status(tokens):
    from daedalus.ui.theme import status_colors
    return status_colors(tokens.light)


class PageBase:
    """页面基类工厂：ScrollArea + **透明皮肤三件套** + 页面级间距。

    返回 `(page, content_layout)`：所有内容都加到那个 layout 上。
    """

    @staticmethod
    def make(tokens, *, name: str, parent=None):
        Qt, _, _, QScrollArea, QVBoxLayout, QWidget = _qt()
        # ⚠️ 每页的 objectName **必须唯一**：库的 `addSubInterface` 拿它当**路由键**，
        #    五页共用一个名字时只有第一个能建出导航项（其余静默返回 None，
        #    而切页走的是页栈所以照样能用——这个 bug 因此藏了很久，直到看真窗口截图才发现
        #    「导航里只剩概览一项」）。前缀保留 `pageRoot`，样式选择器跟着用这个唯一名。
        obj = f"{PAGE_OBJECT}_{name}"
        page = QScrollArea(parent)
        page.setObjectName(obj)
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 三件套第 1 步：QSS 只按 objectName 命中（不写通配选择器，否则会波及子控件）
        page.setStyleSheet(
            f"#{obj} {{ background: transparent; border: none; }}\n"
            f"#{obj} > QWidget > QWidget {{ background: transparent; }}\n")
        # 三件套第 2 步（前）：容器不要自绘背景
        holder = QWidget()
        holder.setObjectName(f"{name}Holder")
        holder.setAutoFillBackground(False)
        sp = tokens.spacing()
        lay = QVBoxLayout(holder)
        # 下边距小于上边距（规格 28/28/28/20）：滚到底别留一大块空白
        lay.setContentsMargins(sp["margin"], sp["margin"], sp["margin"],
                               sp.get("margin_bottom", sp["margin"]))
        lay.setSpacing(sp["gap"])
        page.setWidget(holder)
        # 三件套第 2 步（后）：**setWidget() 之后再调一次**（Qt 会在这时重置属性）
        holder.setAutoFillBackground(False)
        page.setAutoFillBackground(False)
        lay.addStretch(1)                 # 内容不满一屏时不要拉伸卡片
        return page, lay

    @staticmethod
    def subtitle(parent, tokens, text: str):
        """页面标题下那行 **12px 灰描述句**（规格里「破掉一上来就是卡的拥挤感」的那一行）。"""
        _, _, QLabel, _, _, _ = _qt()
        lab = QLabel(str(text), parent)
        lab.setObjectName("pageSubtitle")
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color: {_status(tokens)['muted']}; font-size: 12px;"
                          f" background: transparent;")
        return lab


class SignatureLabel:
    """右下角签名（可关；颜色随强调色；Show/Resize/Activate 时 raise）。"""

    @staticmethod
    def make(tokens, parent, *, text: str = "", locale: str = ""):
        _, _, QLabel, _, _, _ = _qt()
        from daedalus import display_name, VERSION
        from daedalus.ui.i18n import translator
        loc = locale or getattr(tokens, "locale", "") or "en-US"
        t = translator(loc)
        label = QLabel(text or t("app.signature", name=display_name(loc), version=VERSION), parent)
        label.setObjectName("signature")
        label.setStyleSheet(f"color: {tokens.accent}; background: transparent;"
                            f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
        label.setAttribute(_qt()[0].WidgetAttribute.WA_TransparentForMouseEvents, True)
        label.setVisible(bool(tokens.signature))
        return label


class WallpaperWidget:
    """底图窗口：画缓存好的图；没有底图就画主题纯色。"""

    @staticmethod
    def make(tokens, parent=None):
        Qt, _, _, _, _, QWidget = _qt()
        from PySide6.QtGui import QColor, QPalette
        w = QWidget(parent)
        w.setObjectName("wallpaperLayer")
        w.setAutoFillBackground(True)
        pal = w.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(tokens.bg_solid()))
        w.setPalette(pal)
        w._pixmap = None              # noqa: SLF001 - 缓存的底图（QImage/QPixmap）
        w._meta = {}                  # noqa: SLF001
        return w

    @staticmethod
    def set_image(wallpaper_widget, image, *, animate: bool | None = None) -> dict:
        """设置底图（`image` 是 QImage/QPixmap；None = 回退纯色）。**只在主线程调**。

        **交叉溶解 450ms / OutCubic / 限帧**：换底图时把旧图留在 `_old` 上淡出，
        新图淡入——没有这一步，界面会「啪」地一下换掉，就是机主说的「太硬、一点动向都没有」。
        动效总开关关掉、或本来就没有旧图时，直接落图（不做过渡）。
        """
        from PySide6.QtGui import QPixmap
        info = {"fade_ms": 0, "animated": False}
        try:
            new_pm = QPixmap.fromImage(image) if image is not None else None
        except Exception:
            new_pm = image
        tokens = getattr(wallpaper_widget, "_tokens", None)
        old_pm = getattr(wallpaper_widget, "_pixmap", None)
        want = bool(tokens.animations) if tokens is not None else False
        if animate is not None:
            want = bool(animate)
        ms = tokens.anim_ms(getattr(tokens, "fade_ms", 450)) if tokens is not None else 0
        # 只有当「旧图上屏过」且新旧都非空、且动效开着时,才值得做溶解
        if want and ms and old_pm is not None and new_pm is not None and not old_pm.isNull():
            wallpaper_widget._old = old_pm                    # noqa: SLF001
            wallpaper_widget._pixmap = new_pm                 # noqa: SLF001
            info["animated"] = True
            info["fade_ms"] = ms
            WallpaperWidget._start_fade(wallpaper_widget, ms)
        else:
            wallpaper_widget._old = None                      # noqa: SLF001
            wallpaper_widget._pixmap = new_pm                 # noqa: SLF001
            wallpaper_widget.update()
        return info

    @staticmethod
    def _start_fade(wallpaper_widget, ms: int) -> None:
        from PySide6.QtCore import QEasingCurve, QElapsedTimer, QVariantAnimation
        tokens = getattr(wallpaper_widget, "_tokens", None)
        frame_min = tokens.frame_min_ms() if tokens is not None else 33
        last = QElapsedTimer()
        last.start()
        wallpaper_widget._fade = 0.0                          # noqa: SLF001

        def _tick(v):
            wallpaper_widget._fade = float(v)                 # noqa: SLF001
            if last.elapsed() >= frame_min:                   # 限帧：全窗重绘很贵
                last.restart()
                wallpaper_widget.update()

        try:
            anim = QVariantAnimation(wallpaper_widget)
            anim.setDuration(int(ms))
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.valueChanged.connect(_tick)
            anim.finished.connect(lambda: WallpaperWidget._end_fade(wallpaper_widget))
            wallpaper_widget._fade_anim = anim                # noqa: SLF001
            anim.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)
        except Exception as e:
            logger.debug("底图溶解没起来（直接落图）：%s", e)
            WallpaperWidget._end_fade(wallpaper_widget)

    @staticmethod
    def _end_fade(wallpaper_widget) -> None:
        wallpaper_widget._fade = 1.0                          # noqa: SLF001
        wallpaper_widget._old = None                          # noqa: SLF001
        wallpaper_widget.update()

    @staticmethod
    def _paint_one(p, rect, pm) -> None:
        """把一张图画进 rect：尺寸不匹配时**优先裁切**（cover 语义），绝不做整幅拉伸。"""
        from PySide6.QtCore import QRect
        pw, ph = pm.width(), pm.height()
        rw, rh = rect.width(), rect.height()
        if pw <= 0 or ph <= 0 or rw <= 0 or rh <= 0:
            return
        if pw >= rw and ph >= rh:
            src = pm.copy((pw - rw) // 2, (ph - rh) // 2, rw, rh)
            p.drawPixmap(0, 0, src)
            return
        # 图比框小：按 cover 比例取一块**等比**区域再缩一次（不是拉伸变形）
        # ⚠️ `drawPixmap(QRect, QPixmap, QRect)` 的第三个参数是**源矩形**；
        #    曾经把 `pm.copy(...)`（返回 QPixmap）直接传进去 → TypeError、底图整条路径崩。
        scale = max(rw / pw, rh / ph)
        sw, sh = max(1, min(pw, int(rw / scale))), max(1, min(ph, int(rh / scale)))
        sx, sy = max(0, (pw - sw) // 2), max(0, (ph - sh) // 2)
        p.drawPixmap(rect, pm, QRect(sx, sy, sw, sh))

    @staticmethod
    def paint(wallpaper_widget, event) -> bool:
        """在 `paintEvent` 里调它。返回 True 表示画过了（含正在溶解的旧图）。"""
        from PySide6.QtGui import QPainter
        pm = getattr(wallpaper_widget, "_pixmap", None)
        old = getattr(wallpaper_widget, "_old", None)
        if (pm is None or pm.isNull()) and (old is None or old.isNull()):
            return False
        p = QPainter(wallpaper_widget)
        rect = wallpaper_widget.rect()
        if old is not None and not old.isNull():
            p.setOpacity(1.0 - float(getattr(wallpaper_widget, "_fade", 1.0)))
            WallpaperWidget._paint_one(p, rect, old)
            p.setOpacity(1.0)
        if pm is not None and not pm.isNull():
            WallpaperWidget._paint_one(p, rect, pm)
        p.end()
        return True

    @staticmethod
    def tokens_of(wallpaper_widget, tokens) -> None:
        """把令牌挂到图上（动效时长/限帧/主题色都要读它）。"""
        wallpaper_widget._tokens = tokens                     # noqa: SLF001


class TitleBar:
    """自绘标题栏（无边框窗口拖拽 + 最小化/最大化/关闭）。"""

    @staticmethod
    def make(tokens, window, *, title: str = ""):
        Qt, _, QLabel, _, _, QWidget = _qt()
        from PySide6.QtWidgets import QHBoxLayout, QPushButton
        bar = QWidget(window)
        bar.setObjectName("titleBar")
        bar.setFixedHeight(40)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 6, 10, 6)
        lay.setSpacing(8)
        st = _status(tokens)
        lab = QLabel(title, bar)
        lab.setStyleSheet(f"color: {st['muted']}; background: transparent;")
        lay.addWidget(lab)
        lay.addStretch(1)
        buttons = {}
        for key, text in (("min", "—"), ("max", "□"), ("close", "×")):
            b = QPushButton(text, bar)
            b.setObjectName(f"titleBtn_{key}")
            b.setFixedSize(34, 26)
            b.setStyleSheet(
                f"QPushButton#{b.objectName()} {{ background: transparent; border: none;"
                f" color: {st['muted']}; border-radius: 6px; }}"
                f"QPushButton#{b.objectName()}:hover {{ background: rgba(128,128,128,60); }}")
            buttons[key] = b
            lay.addWidget(b)
        buttons["min"].clicked.connect(window.showMinimized)
        buttons["max"].clicked.connect(lambda: window.showNormal() if window.isMaximized()
                                      else window.showMaximized())
        buttons["close"].clicked.connect(window.close)

        bar._drag = {"pos": None}          # noqa: SLF001

        def press(ev):
            if ev.button() == Qt.MouseButton.LeftButton:
                bar._drag["pos"] = ev.globalPosition().toPoint() - window.frameGeometry().topLeft()

        def move(ev):
            if bar._drag["pos"] is not None and ev.buttons() & Qt.MouseButton.LeftButton:
                window.move(ev.globalPosition().toPoint() - bar._drag["pos"])

        def release(ev):
            bar._drag["pos"] = None

        bar.mousePressEvent = press
        bar.mouseMoveEvent = move
        bar.mouseReleaseEvent = release
        return bar, buttons


def apply_theme(app_or_window, tokens) -> dict:
    """把一组令牌**一次性**应用到整棵树（所有卡片都由这一个函数生成样式）。

    做三件事：
      ① 把主题与强调色交给**组件库**（`setTheme`/`setThemeColor`）——导航、按钮、开关、
         滚动条这些库控件的 120ms 过渡与悬停态全由库负责，我们**不重写**它们；
      ② 给我们的对象名刷一份 QSS（字体链 + 玻璃层，全部出自同一个生成器）；
      ③ 把新令牌挂回每张卡片（悬停/数字滚动读的就是它，不挂就会用旧时长/旧颜色）。

    返回本次应用的事实（给调试/GUI 显示「当前生效参数」）。
    """
    from daedalus.ui.theme import card_qss, font_qss, panel_qss, text_color
    st = _status(tokens)
    sp = tokens.spacing()
    base = font_qss(getattr(tokens, "locale", "zh-CN"), size_pt=tokens.font_pt)
    # ① 库的主题与强调色（库控件的动效/悬停都挂在这上面）
    try:
        from qfluentwidgets import Theme, setTheme, setThemeColor
        setTheme(Theme.LIGHT if tokens.light else Theme.DARK)
        setThemeColor(tokens.accent)
    except Exception as e:
        logger.debug("设置库主题失败（不影响自绘部分）：%s", e)
    # ② 所有卡片名走**同一个**生成器（铁律）
    for name in ("card", "statCard", "panel", "settingsCard", "taskCard", "navPanel"):
        base += card_qss(tokens, object_name=name)
    try:
        if hasattr(app_or_window, "setStyleSheet"):
            app_or_window.setStyleSheet(base)
    except Exception as e:
        logger.debug("应用样式失败：%s", e)
    # ③ 令牌挂回每张卡片（悬停插值、数字滚动都要读最新值）
    try:
        from PySide6.QtWidgets import QFrame, QLabel
        names = ("card", "statCard", "panel", "settingsCard", "taskCard")
        for w in app_or_window.findChildren(QFrame):
            if w.objectName() in names:
                w._tokens = tokens                       # noqa: SLF001
                w.setStyleSheet(_card_style(tokens, w.objectName()))
        # 统计卡数字与标签的颜色（主题切换时文字颜色必须跟着换）
        for lab in app_or_window.findChildren(QLabel):
            nm = lab.objectName()
            if nm.endswith("Value"):
                lab.setStyleSheet(f"color: {text_color(tokens.light)}; font-size: 26px;"
                                  f" font-weight: 700; background: transparent;")
            elif nm.endswith("Label"):
                lab.setStyleSheet(f"color: {st['muted']}; font-size: {int(tokens.font_pt)}pt;"
                                  f" background: transparent;")
    except Exception as e:
        logger.debug("卡片令牌回挂失败：%s", e)
    return {"panel_alpha": tokens.card_alpha(), "radius": tokens.radius,
            "border": ("rgba(0,0,0,14)" if tokens.light else "rgba(255,255,255,22)"),
            "accent": tokens.accent, "font_pt": tokens.font_pt, "density": tokens.density,
            "spacing": sp, "status": st, "expert_mode": tokens.expert_mode,
            "animations": bool(tokens.animations), "fade_ms": tokens.anim_ms(tokens.fade_ms),
            "fps_cap": tokens.fps_cap}

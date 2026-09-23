# -*- coding: utf-8 -*-
"""控件层：**玻璃卡片 / 统计卡 / 页面基类 / 底图窗口 / 自绘标题栏 / 签名**

三条必须守住的实现细节（都是"看起来能用、实则糊/裂/花"的坑）：
  1) **透明皮肤三件套**（页面基类）：QSS 用 `objectName` 限定 + `setAutoFillBackground(False)`
     在 `setWidget()` **前后各调一次** + 卡片样式由**同一个函数**生成。少一步就会出现
     "页面白底盖住底图"或"只有滚到某处才透明"这类玄学现象。
  2) **统计卡**：数字 26px/700 在上、灰色标签在下（信息层级固定，别一张卡一个样）。
  3) **签名**：右下角，颜色随强调色，`Show/Resize/Activate` 时 `raise_()`（否则会被后创建的
     子控件盖住——这是"签名突然不见了"的常见原因）。

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

    为什么用工厂：设计系统的铁律是"同页所有卡片由同一个函数生成"。做成基类容易被人
    顺手 override 几个属性，统一感就慢慢烂掉了。
    """

    @staticmethod
    def make(tokens, *, name: str = "card", parent=None):
        _, QFrame, _, _, _, _ = _qt()
        frame = QFrame(parent)
        frame.setObjectName(name)                       # QSS 靠 objectName 命中
        frame.setStyleSheet(_card_style(tokens, name))
        frame.setAutoFillBackground(False)
        return frame

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


def _card_style(tokens, name: str) -> str:
    from daedalus.ui.theme import card_qss
    return card_qss(tokens, object_name=name)


class StatCard:
    """统计卡：数字在上（26px/700）、灰色标签在下。`set_value` 只更新数字。"""

    @staticmethod
    def make(tokens, label: str, value: str = "—", *, name: str = "statCard", parent=None):
        Qt, _, QLabel, _, _, _ = _qt()
        card = GlassCard.make(tokens, name=name, parent=parent)
        lay = GlassCard.body(card, tokens)
        num = QLabel(str(value), card)
        num.setObjectName(f"{name}Value")
        num.setStyleSheet(f"color: {tokens.accent}; font-size: 26px; font-weight: 700;")
        lab = QLabel(str(label), card)
        lab.setObjectName(f"{name}Label")
        st = _status(tokens)
        lab.setStyleSheet(f"color: {st['muted']}; font-size: {int(tokens.font_pt)}pt;")
        lay.addWidget(num)
        lay.addWidget(lab)
        card._value_label = num          # noqa: SLF001 - 工厂产物，直接挂引用最省事
        card._caption_label = lab        # noqa: SLF001
        return card

    @staticmethod
    def set_value(card, value, note: str = "") -> None:
        try:
            card._value_label.setText(str(value))
            if note:
                card._caption_label.setText(str(note))
        except Exception as e:
            logger.debug("统计卡更新失败：%s", e)


def _status(tokens):
    from daedalus.ui.theme import status_colors
    return status_colors(tokens.light)


class PageBase:
    """页面基类工厂：ScrollArea + **透明皮肤三件套**。

    返回 `(page, content_layout)`：所有内容都加到那个 layout 上。
    """

    @staticmethod
    def make(tokens, *, name: str, parent=None):
        Qt, _, _, QScrollArea, QVBoxLayout, QWidget = _qt()
        page = QScrollArea(parent)
        page.setObjectName(PAGE_OBJECT)
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.Shape.NoFrame)
        page.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 三件套第 1 步：QSS 只按 objectName 命中（不写通配选择器，否则会波及子控件）
        page.setStyleSheet(
            f"#{PAGE_OBJECT} {{ background: transparent; border: none; }}\n"
            f"#{PAGE_OBJECT} > QWidget > QWidget {{ background: transparent; }}\n")
        # 三件套第 2 步（前）：容器不要自绘背景
        holder = QWidget()
        holder.setObjectName(f"{name}Holder")
        holder.setAutoFillBackground(False)
        sp = tokens.spacing()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(sp["margin"], sp["margin"], sp["margin"], sp["margin"])
        lay.setSpacing(sp["gap"])
        page.setWidget(holder)
        # 三件套第 2 步（后）：**setWidget() 之后再调一次**（Qt 会在这时重置属性）
        holder.setAutoFillBackground(False)
        page.setAutoFillBackground(False)
        lay.addStretch(1)                 # 内容不满一屏时不要拉伸卡片
        return page, lay


class SignatureLabel:
    """右下角签名（可关；颜色随强调色；Show/Resize/Activate 时 raise）。"""

    @staticmethod
    def make(tokens, parent, *, text: str = "", locale: str = "zh-CN"):
        _, _, QLabel, _, _, _ = _qt()
        from daedalus import display_name, VERSION
        label = QLabel(text or f"{display_name(locale)} v{VERSION}", parent)
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
    def set_image(wallpaper_widget, image) -> None:
        """设置底图（`image` 是 QImage/QPixmap；None = 回退纯色）。**只在主线程调**。"""
        from PySide6.QtGui import QPixmap
        try:
            wallpaper_widget._pixmap = QPixmap.fromImage(image) if image is not None else None
        except Exception:
            wallpaper_widget._pixmap = image
        wallpaper_widget.update()

    @staticmethod
    def paint(wallpaper_widget, event) -> bool:
        """在 `paintEvent` 里调它。返回 True 表示画过了。"""
        pm = getattr(wallpaper_widget, "_pixmap", None)
        if pm is None or pm.isNull():
            return False
        from PySide6.QtGui import QPainter
        p = QPainter(wallpaper_widget)
        rect = wallpaper_widget.rect()
        # 尺寸不一致时优先**裁切**（同比例直接用；差得远才缩一次）
        if pm.width() >= rect.width() and pm.height() >= rect.height():
            src = pm.copy((pm.width() - rect.width()) // 2, (pm.height() - rect.height()) // 2,
                          rect.width(), rect.height())
            p.drawPixmap(0, 0, src)
        else:
            p.drawPixmap(rect, pm, pm.rect())
        p.end()
        return True


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

    返回本次应用的事实（给调试/GUI 显示"当前生效参数"）：透明度、描边、圆角、强调色。
    """
    from daedalus.ui.theme import card_qss, panel_qss
    st = _status(tokens)
    sp = tokens.spacing()
    base = f"""
QWidget {{ font-size: {tokens.font_pt}pt; }}
#titleBar {{ background: transparent; }}
"""
    # 所有卡片名走**同一个**生成器（铁律）
    for name in ("card", "statCard", "panel", "settingsCard", "taskCard"):
        base += card_qss(tokens, object_name=name)
    base += panel_qss(tokens.card_alpha(), light=tokens.light, radius=tokens.radius,
                      accent=tokens.accent, object_name="navPanel")
    try:
        if hasattr(app_or_window, "setStyleSheet"):
            app_or_window.setStyleSheet(base)
    except Exception as e:
        logger.debug("应用样式失败：%s", e)
    return {"panel_alpha": tokens.card_alpha(), "radius": tokens.radius,
            "border": ("rgba(0,0,0,14)" if tokens.light else "rgba(255,255,255,22)"),
            "accent": tokens.accent, "font_pt": tokens.font_pt, "density": tokens.density,
            "spacing": sp, "status": st, "expert_mode": tokens.expert_mode}

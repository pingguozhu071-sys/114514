# -*- coding: utf-8 -*-
"""Daedalus · 统一采集与感知引擎（不是爬虫）

名称与版本的**唯一来源**（不许在别处硬编码）：界面文案、CLI、安装向导、包元数据
全部从这里读。三种语言按你的要求固定为 zh-CN / ja-JP / en-US(en-US)，拼写用 en-US。

名字怎么来的（写给未来的自己）：
    Daedalus（代达罗斯）是希腊神话里的匠人——他的名字在英语里生出一个形容词
    **daedal**：*精工细作的、精巧的、错综复杂的*（正是你想要的"精致"，
    而不是 Odysseus/Odyssey 那条"旅程/经过"的意思）。
    整段神话与本工程的对应：
      迷宫          = 多种架构并存的多路径系统
      阿里阿德涅之线 = 血缘与可重放（顺着线永远能退出来 → 事实不会丢）
      翅膀          = 能力有边界，飞太高蜡会化（→ 不越界、不突破对方防护）
"""

from __future__ import annotations

__all__ = ["NAME", "NAME_EN", "NAME_JA", "NAME_ZH", "LOCALES", "VERSION", "about", "display_name"]

VERSION = "0.1.0.dev0"

NAME_EN = "Daedalus"          # en-US 拼写（不用 en-GB）
NAME_ZH = "代达罗斯"
NAME_JA = "ダイダロス"

LOCALES = ("zh-CN", "ja-JP", "en-US")
_LOCALE_NAMES = {"en-US": NAME_EN, "zh-CN": NAME_ZH, "ja-JP": NAME_JA}

# 兼容别名：需要"当前语言下的名字"时用 display_name(locale)
NAME = NAME_EN


def display_name(locale: str = "en-US") -> str:
    """按语言取名字（未知语言回退 en-US）。"""
    return _LOCALE_NAMES.get(str(locale), NAME_EN)


def about() -> dict:
    """给 CLI / GUI / 安装向导共用的元信息（避免各处硬编码）。"""
    return {
        "name": NAME_EN,
        "name_zh": NAME_ZH,
        "name_ja": NAME_JA,
        "version": VERSION,
        "locales": list(LOCALES),
        "tagline_en": "Unified acquisition & observation engine (not a crawler)",
        "tagline_zh": "统一采集与感知引擎（不是爬虫）",
        "tagline_ja": "統合収集・観測エンジン（クローラーではない）",
    }

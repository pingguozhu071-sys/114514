# -*- coding: utf-8 -*-
"""内置解析器包：HTML / 订阅与站点地图 / JSON 与文本 / 制品元数据

**新增解析器 = 只加一个文件**（`docs/08-扩展路径.md` §二 承诺的那条）：
  1) 在本目录新建 `xxx.py`；
  2) 模块级导出 `SPEC = ParserSpec(...)`（`parse(data, meta)` 必须**同构返回**
     `{"ok": bool, "error"?: str, ...}`）；
  3) 完 —— `understand/registry.py` 的 `default_registry()` 会**扫描本目录**自动注册。

**这里没有注册点**：不要在本文件里维护「有哪些解析器」的清单。
以前 `default_registry()` 是手写清单，结果 `mediainfo.py` 的 `SPEC` 漏了注册、
引擎路径永远到不了它 —— 手写清单必然漏，这就是那次事故的形状。

`__all__` 由**目录内容**算出（只数名字，不 import 子模块），供 `from ... import *`
与静态检查用；它**不是**注册点，所以也不会变成「新增解析器要手改的第二处」。
"""

from __future__ import annotations

import importlib
import pkgutil

__all__ = sorted(m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_"))


def __getattr__(name: str):
    """PEP 562：`包.<模块名>` 按需加载（`__all__` 只算名字，所以这里仍要能懒加载）。"""
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        # 只有「这个子模块不存在」才转成 AttributeError；子模块**自己缺依赖**要原样抛出，
        # 否则真因会被这句「没有模块」盖掉（那正是最难查的一类假报错）。
        if str(getattr(e, "name", "")) == f"{__name__}.{name}":
            raise AttributeError(f"{__name__} 里没有模块 {name}") from e
        raise

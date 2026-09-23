# -*- coding: utf-8 -*-
"""日志脱敏的**正确挂载方式**（纯同步，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/config.py`（`_SanitizeLogFilter` /
`attach_log_sanitizer` / `_install_handler_autosanitize`），重写并补齐注释。
Daedalus 内的改动：
  1) 导入改为包内绝对导入；`_AUTOSANITIZE_FLAG` 改名 `_DAEDALUS_AUTOSANITIZED`（去掉 Kiana 遗留命名）。
  2) **补上结构化字段与 traceback 的脱敏**——原实现只看 `record.getMessage()`，
     `extra={...}` 里的结构化字段与 `Formatter.format()` 拼进来的 traceback **完全没被覆盖**
     （这是真实的泄漏面，见下）。
  3) `install_auto_sanitize(sanitize_formatted=True)` 默认再包一层 `Formatter.format`，
     让"最终落盘的整行文本"也被脱敏（traceback、异常内嵌 URL 都在其中）。
     ⚠️ 每类数据的开关由 S1 的策略层（`SanitizationPolicy`）决定，本文件只提供机制。

────────────────────────────────────────────────────────────────
这段代码解决的是 Kiana 上**长期存在且无人察觉**的一个洞
    脱敏过滤器原本挂在 **root logger** 上。
    而 Python logging 的传播机制是：`Logger.callHandlers` 遍历祖先的 **handlers**，
    `Logger.filter` **只在记录经该 logger 自身处理时**才被调用
    → 于是全仓 `logging.getLogger(__name__)` 的子 logger 记录**完全没有脱敏**，
      含 `?token=` 的代理 URL / API Key 形态直接明文落盘。

正确做法（本文件提供三条）
    ① 给每个 handler 显式挂：`attach_log_sanitizer(handler)`
    ② **结构性消除"忘记挂"**：包装 `logging.Handler.__init__`，
       让**任何**新建 handler（含第三方库自建）自动带上过滤器 —— `install_auto_sanitize()`
    ③ 覆盖"非 logging 出口"与"结构化字段"：`scrub()` / 记录字段脱敏 / Formatter 兜底

────────────────────────────────────────────────────────────────
用法（在 Daedalus 的日志初始化处调用一次即可，越早越好）

    import logging
    from daedalus.obs.logging_sanitizer import install_auto_sanitize, scrub

    install_auto_sanitize()                   # 在创建任何 handler 之前
    logging.basicConfig(level=logging.INFO)   # 之后创建的 handler 自动获得脱敏

    # 非 logging 出口（print 桥 / GUI 日志 / 异常回显）要手动过一遍：
    print(scrub(f"fetch failed: {url}"))
"""

from __future__ import annotations

import logging as _logging

from daedalus.obs.policy import SanitizationPolicy
from daedalus.obs.sanitize import sanitize_record, sanitize_text, sanitize_url

__all__ = ["SanitizeFilter", "attach_log_sanitizer", "install_auto_sanitize", "scrub",
           "active_policy"]

_AUTOSANITIZE_FLAG = "_daedalus_autosanitized"
_FORMAT_PATCHED_FLAG = "_daedalus_format_sanitized"

# 生效中的脱敏策略（由 install_auto_sanitize(policy=...) 设置；未设置时用默认值——日志类全开）
_ACTIVE_POLICY: SanitizationPolicy | None = None


def active_policy() -> SanitizationPolicy:
    """当前生效的脱敏策略（未显式设置时返回默认策略：日志类全开）。"""
    return _ACTIVE_POLICY or SanitizationPolicy()

# LogRecord 的标准属性：这些不是"业务字段"，不按记录级脱敏处理（msg 会单独过）
_STANDARD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
})


class SanitizeFilter(_logging.Filter):
    """脱敏过滤器（幂等、绝不抛异常）。**按策略的日志类开关**决定脱到什么程度。

    覆盖三处：
      * `record.getMessage()` 的结果（消息 + 参数）
      * `record.__dict__` 里的**自定义字段**（`extra={...}` 传进来的）
      * 已有的 `exc_text`（若已被某个 Formatter 渲染过）
    """

    def __init__(self, policy: SanitizationPolicy | None = None):
        super().__init__()
        self._policy = policy

    @property
    def policy(self) -> SanitizationPolicy:
        return self._policy or active_policy()

    def filter(self, record) -> bool:      # noqa: A003 - logging 的固定接口名
        pol = self.policy
        try:
            msg = record.getMessage()
            if msg and isinstance(msg, str):
                safe = pol.scrub_log(msg)
                if safe != msg:
                    record.msg = safe
                    record.args = ()           # 已把参数烤进 msg，避免二次格式化
        except Exception:
            pass                               # 日志系统自身绝不能因此崩溃

        try:
            for key, val in list(record.__dict__.items()):
                if key in _STANDARD_ATTRS or key.startswith("_"):
                    continue
                if isinstance(val, str):
                    if val:
                        record.__dict__[key] = pol.scrub_log(val)
                elif isinstance(val, (dict, list)):
                    if pol.log_text or pol.log_url:
                        record.__dict__[key] = sanitize_record(val)
            if getattr(record, "exc_text", None):
                record.exc_text = pol.scrub_log(record.exc_text)
        except Exception:
            pass
        return True


def attach_log_sanitizer(handler: _logging.Handler) -> None:
    """把脱敏过滤器挂到**指定 handler** 上（幂等）。"""
    try:
        if handler is not None and not any(
                isinstance(f, SanitizeFilter) for f in handler.filters):
            handler.addFilter(SanitizeFilter())
    except Exception:
        pass


def _install_format_sanitize() -> None:
    """包一层 `Formatter.format`：最终整行文本（含 traceback）再兜一次。

    为什么需要：`exc_info` 的 traceback 是 `Formatter.format()` 渲染出来的，
    过滤器在渲染**之前**执行，拿不到那部分。包一层后，"落盘的那一行"必然过 scrub。
    """
    try:
        if getattr(_logging.Formatter, _FORMAT_PATCHED_FLAG, False):
            return
        orig_format = _logging.Formatter.format

        def _patched_format(self, record):
            return scrub(orig_format(self, record))

        _logging.Formatter.format = _patched_format          # type: ignore[method-assign]
        setattr(_logging.Formatter, _FORMAT_PATCHED_FLAG, True)
    except Exception:
        pass


def install_auto_sanitize(policy: SanitizationPolicy | None = None,
                         sanitize_formatted: bool = True) -> None:
    """包装 `logging.Handler.__init__`：**任何**新建 handler 自动获得脱敏。

    动机：靠"调用方记得挂"是脆弱约定，漏挂一次的代价是凭据明文落盘且无人察觉。
    包装后这个失效模式被结构性消除。对第三方库的日志同样生效——这正是期望行为
    （凭据泄漏不分来源）。

    `policy`：按数据类型分开关的脱敏策略（见 `docs/11`）；不传则用默认（日志类全开）。
    `sanitize_formatted=True` 时同时包 `Formatter.format`（覆盖 traceback），
    代价是每行日志多一趟正则（幂等，二次通过不会再改）。
    """
    global _ACTIVE_POLICY
    if policy is not None:
        _ACTIVE_POLICY = policy
    try:
        if not getattr(_logging.Handler, _AUTOSANITIZE_FLAG, False):
            orig_init = _logging.Handler.__init__

            def _patched_init(self, level=_logging.NOTSET):
                orig_init(self, level)
                try:
                    attach_log_sanitizer(self)
                except Exception:
                    pass

            _logging.Handler.__init__ = _patched_init            # type: ignore[method-assign]
            setattr(_logging.Handler, _AUTOSANITIZE_FLAG, True)

            # 已存在的 handler（含 root 与 lastResort）补挂一次
            root = _logging.getLogger()
            for h in list(root.handlers):
                attach_log_sanitizer(h)
            if getattr(root, "lastResort", None) is not None:
                attach_log_sanitizer(root.lastResort)
    except Exception:
        pass

    if sanitize_formatted:
        _install_format_sanitize()


def scrub(text) -> str:
    """非 logging 出口的脱敏（print 桥 / GUI / traceback 回显）——**按当前策略**处理。

    ⚠️ 这些出口**不走 logging**，过滤器覆盖不到；尤其 `Formatter.format()` 会
    把 `exc_info` 的 traceback 自动拼进来，而过滤器只看 `getMessage()` 拿不到那部分。
    """
    try:
        return active_policy().scrub_log(str(text))
    except Exception:
        return str(text)

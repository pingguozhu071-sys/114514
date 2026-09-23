# -*- coding: utf-8 -*-
"""结构化日志：**字段稳定、可轮转、先脱敏后落盘**

三条硬要求（对应清单 L2）与四个真实坑：

要求
  1) **JSON 字段稳定**：`ts / level / logger / event / msg / thread / task_id / fields / exc`。
     字段名一旦发布就是契约——GUI 的日志面板、`jq` 脚本、回归对比都靠它。
  2) **轮转**：`RotatingFileHandler`，默认 10MB × 5（值来自配置，不写死）。
  3) **带脱敏**：handler 上挂 `obs.logging_sanitizer`（S1 已实现），且**先脱敏后写文件**。

坑（都是踩过才知道的）
  * `logging.basicConfig()` 重复调用**不会**去重：多次 setup 会写出 N 份重复日志。
    这里自己建 handler 且带幂等标记。
  * **Windows 默认编码是 cp936**：日志里出现日文/emoji 会 `UnicodeEncodeError` 或写成乱码。
    文件 handler 一律显式 `encoding="utf-8"`，并 `errors="replace"` 兜底。
  * **中文路径**：日志目录含中文时 `os.makedirs` 是安全的，但**别用 `os.path` 拼字符串**，
    一律 `pathlib.Path`（本工程统一约定）。
  * 目录不可写时**不许静默**：降级到只打控制台，并明确告诉用户"文件日志没起来"。
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import pathlib
import sys
import threading
import time

from daedalus.obs.logging_sanitizer import attach_log_sanitizer, install_auto_sanitize

__all__ = ["JsonFormatter", "TextFormatter", "setup_logging", "log_event", "logging_state"]

# 幂等标记：防止多次 setup 挂出多份 handler（basicConfig 做不到这件事）
_SETUP_LOCK = threading.Lock()
_STATE: dict = {"configured": False, "file": None, "json": False, "level": None, "why": ""}

# 已知的 LogRecord 属性（其余一切进 `fields`）——**这是字段契约的一部分**
_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime",
})


class JsonFormatter(logging.Formatter):
    """一行一条 JSON。异常走 `exc` 字段（不破坏单行结构，方便 jq）。"""

    def __init__(self, *, ensure_ascii: bool = False):
        super().__init__()
        self.ensure_ascii = bool(ensure_ascii)

    def format(self, record: logging.LogRecord) -> str:
        body: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "thread": record.threadName,
        }
        ev = getattr(record, "event", "")
        if ev:
            body["event"] = ev
        task_id = getattr(record, "task_id", "")
        if task_id:
            body["task_id"] = task_id
        # 其余自定义字段（`logger.info("x", extra={"fields": {...}})` 或直接摊平）
        fields: dict = {}
        given = getattr(record, "fields", None)
        if isinstance(given, dict):
            fields.update(given)
        for k, v in record.__dict__.items():
            if k not in _RESERVED and k not in ("event", "task_id", "fields"):
                fields[k] = v
        if fields:
            body["fields"] = fields
        if record.exc_info:
            body["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(body, ensure_ascii=self.ensure_ascii, sort_keys=False,
                              default=str)
        except Exception:
            # 格式化器**绝不抛**：抛了会丢日志（还会连带打坏 logging 内部状态）
            return json.dumps({"ts": body["ts"], "level": "ERROR", "logger": record.name,
                               "msg": "日志序列化失败", "thread": record.threadName},
                              ensure_ascii=self.ensure_ascii)


class TextFormatter(logging.Formatter):
    """人看的格式（控制台默认）。带线程名与 logger 短名——单机多线程排查靠它。"""

    def __init__(self):
        super().__init__(fmt="%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
                         datefmt="%H:%M:%S")


def setup_logging(cfg: dict | None = None, *, force: bool = False) -> dict:
    """按配置装配日志。返回状态 dict（**幂等**；重复调用只更新级别）。

    `cfg` 是 `config.merged()` 的结果（或它的 `[logging]` 段）。键：
      `level`（DEBUG/INFO/WARNING/ERROR）/ `dir`（空=只控制台）/ `json`（true=文件写 JSON）/
      `max_bytes` / `backup_count`
    """
    from daedalus.config import merged
    c = merged(cfg if (cfg and "logging" in cfg) else {"logging": cfg or {}})
    lg = c["logging"]
    level_name = str(lg.get("level") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_dir = lg.get("dir") or None
    as_json = bool(lg.get("json", True))

    with _SETUP_LOCK:
        root = logging.getLogger()
        # 幂等：已经装过就只调级别，不再挂第二份 handler
        if _STATE["configured"] and not force:
            root.setLevel(level)
            for h in list(root.handlers):
                if getattr(h, "_daedalus_handler", ""):
                    h.setLevel(level)
            _STATE["level"] = level_name
            return dict(_STATE)

        # 任何**新建**的 handler 都自动获得脱敏（S1 的全局保险）
        install_auto_sanitize()
        for h in list(root.handlers):        # 清掉别人先挂的（避免重复行）
            if not getattr(h, "_daedalus_handler", ""):
                root.removeHandler(h)

        # ① 控制台（人读；`json=true` 时也走 JSON，便于管道处理）
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(JsonFormatter() if as_json else TextFormatter())
        _tag_handler(stream, "console")
        attach_log_sanitizer(stream)
        root.addHandler(stream)

        # ② 文件（带轮转；**显式 UTF-8**，否则 Windows 上中文/日文必炸）
        file_path = None
        why = ""
        if log_dir:
            try:
                d = pathlib.Path(str(log_dir))
                d.mkdir(parents=True, exist_ok=True)
                file_path = d / "daedalus.log.jsonl" if as_json else d / "daedalus.log"
                fh = logging.handlers.RotatingFileHandler(
                    str(file_path), maxBytes=int(lg.get("max_bytes") or 10 * 1024 * 1024),
                    backupCount=int(lg.get("backup_count") or 5), encoding="utf-8",
                    errors="replace", delay=True)
                fh.setFormatter(JsonFormatter() if as_json else TextFormatter())
                _tag_handler(fh, "file")
                attach_log_sanitizer(fh)
                root.addHandler(fh)
            except Exception as e:           # 目录不可写**不许静默**：如实降级并告知
                why = f"文件日志未启用（{type(e).__name__}: {e}）"
                file_path = None

        root.setLevel(level)
        _STATE.update(configured=True, file=str(file_path) if file_path else None,
                      json=as_json, level=level_name, why=why)
        logger = logging.getLogger(__name__)
        if file_path:
            logger.info("日志已装配", extra={"event": "logging.ready",
                                           "fields": {"file": str(file_path),
                                                      "level": level_name, "json": as_json}})
        elif why:
            logger.warning(why, extra={"event": "logging.degraded"})
        return dict(_STATE)


def _tag_handler(handler: logging.Handler, kind: str) -> None:
    handler._daedalus_handler = kind        # type: ignore[attr-defined]


def logging_state() -> dict:
    """当前日志装配状态（CLI/GUI 自检用：路径、级别、是不是 JSON、有没有降级原因）。"""
    return dict(_STATE)


def log_event(logger: logging.Logger, event: str, msg: str = "", level: int = logging.INFO,
              task_id: str = "", **fields) -> None:
    """记一条**结构化事件**：`event` 是稳定的机器可读键，`msg` 是给人看的补充。"""
    logger.log(level, msg or event,
               extra={"event": str(event), "task_id": str(task_id or ""), "fields": dict(fields)})

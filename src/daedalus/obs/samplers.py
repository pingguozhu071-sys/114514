# -*- coding: utf-8 -*-
"""采样器：把"进程/磁盘"这类**没法挂在热路径上**的量定期采进指标面

只做三件事：读值 → 写指标 → 能被**优雅关闭**（不泄漏线程/句柄）。

读数策略（诚实降级，不假装有数据）：
  1) `psutil` 可用 → 用它（rss / 线程数 / 句柄数 / 磁盘）；
  2) 没有 psutil（打包时可能不带）→ Windows 上退回 `ctypes` 调
     `GetProcessMemoryInfo` 与 `GetProcessHandleCount`；其余平台退到 `resource.getrusage`；
  3) 都拿不到 → 该指标**不写**（宁可缺，也不要编）。`SamplerReport.unavailable` 会说明原因。

为什么采样要有上限：采样线程每隔 `interval` 秒醒一次，跨度过大（比如 1h）时它采到的点数
与内存无关——但**采样器本身不能成为句柄泄漏源**：`stop()` 必须 join 到线程真的退出。
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import threading
import time

from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["ProcessSampler", "read_process_stats", "disk_stats"]

try:                                    # 可选依赖：有就用，没有就走 ctypes
    import psutil as _psutil
except Exception:                       # pragma: no cover
    _psutil = None

_PROC = None
if _psutil is not None:
    try:
        _PROC = _psutil.Process(os.getpid())
    except Exception:
        _PROC = None


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def read_process_stats() -> dict:
    """读本进程的 rss / 线程数 / 句柄数。取不到的键**不出现**（不编造）。"""
    out: dict = {}
    if _PROC is not None:
        try:
            mi = _PROC.memory_info()
            out["rss_bytes"] = int(mi.rss)
        except Exception:
            pass
        try:
            out["threads"] = int(_PROC.num_threads())
        except Exception:
            pass
        try:
            if hasattr(_PROC, "num_handles"):
                out["handles"] = int(_PROC.num_handles())
        except Exception:
            pass
    elif os.name == "nt":
        try:
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters),
                    counters.cb):
                out["rss_bytes"] = int(counters.WorkingSetSize)
        except Exception:
            pass
        try:
            n = ctypes.c_ulong(0)
            if ctypes.windll.kernel32.GetProcessHandleCount(
                    ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(n)):
                out["handles"] = int(n.value)
        except Exception:
            pass
        try:
            out["threads"] = int(threading.active_count())
        except Exception:
            pass
    else:                                # pragma: no cover - 非 Windows 兜底
        try:
            import resource
            ru = resource.getrusage(resource.RUSAGE_SELF)
            out["rss_bytes"] = int(ru.ru_maxrss) * (1024 if os.uname().sysname != "Darwin" else 1)
        except Exception:
            pass
        out["threads"] = int(threading.active_count())
    return out


def disk_stats(path) -> dict:
    """数据盘剩余空间（字节 / 百分比）。路径不存在时向上找最近的已存在目录。"""
    import pathlib
    p = pathlib.Path(str(path))
    for _ in range(8):                   # 目录可能还没建：往上找到存在的祖先
        try:
            if p.exists():
                break
        except Exception:
            break
        if p.parent == p:
            break
        p = p.parent
    try:
        u = shutil.disk_usage(str(p))
        return {"free_bytes": int(u.free), "total_bytes": int(u.total),
                "free_pct": (u.free / u.total * 100.0) if u.total else 0.0}
    except Exception:
        return {}


class ProcessSampler:
    """后台采样线程（daemon）。**必须能停**：`stop()` 会 join 到线程退出。"""

    def __init__(self, *, interval: float = 5.0, disk_path=None):
        self.interval = max(0.05, float(interval))
        self.disk_path = disk_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples = 0
        self.unavailable: list[str] = []

    def sample_once(self) -> dict:
        st = read_process_stats()
        for k, v in st.items():
            METRICS.set(f"proc.{k}", v)
        if "rss_bytes" in st:
            METRICS.set("proc.rss_mb", st["rss_bytes"] / (1 << 20))
        if self.disk_path is not None:
            ds = disk_stats(self.disk_path)
            for k, v in ds.items():
                METRICS.set(f"disk.{k}", v)
        if not st:
            self.unavailable = ["psutil 与 ctypes 都取不到进程统计（rss/handles 缺失）"]
        self.samples += 1
        return st

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as e:        # 采样失败绝不影响主业务
                logger.debug("采样失败：%s", e)
            self._stop.wait(self.interval)

    def start(self) -> "ProcessSampler":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="dae-sampler", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 3.0) -> dict:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        alive = bool(t is not None and t.is_alive())
        self._thread = None
        return {"samples": self.samples, "thread_alive": alive,
                "unavailable": list(self.unavailable)}

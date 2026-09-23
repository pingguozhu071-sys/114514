# -*- coding: utf-8 -*-
"""子进程执行面：外部工具（ffmpeg / ffprobe / 未来别的）的**统一封装**

四条规矩（都是踩出来的）：
  1) **不改进程级 locale**：Kiana 曾用 `locale.setlocale` 防子进程输出解码崩溃——那是**进程级副作用**，
     多线程宿主里很危险。正确做法：给子进程传 `env`（UTF-8）+ 显式 `encoding="utf-8", errors="replace"`。
  2) **输出文件名用纯 ASCII（哈希）**：Windows 下子进程常按 GBK 解码参数/输出，中文路径会直接崩，
     表现为"下载完目录是空的"。
  3) **缺件硬报错 + 就绪探测**：工具不在 PATH 里要**明确说**（`ToolMissing` / `readiness()` 报告），
     不许静默降级成"功能看起来有其实没有"。
  4) **超时与产物验证**：超时取消；**退出码 0 不等于成功**——产物交给
     `capture/artifacts.py` 的契约去验。
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["ToolMissing", "ToolResult", "run_tool", "which", "readiness", "safe_output_name",
           "REQUIRED_TOOLS", "OPTIONAL_TOOLS"]

REQUIRED_TOOLS: tuple[str, ...] = ()            # 引擎核心不依赖任何外部工具
OPTIONAL_TOOLS = ("ffmpeg", "ffprobe", "aria2c", "yt-dlp")


class ToolMissing(RuntimeError):
    """需要的工具不存在（**缺件硬报错**，不做静默降级）。"""

    def __init__(self, tool: str):
        self.tool = tool
        super().__init__(f"缺少外部工具 {tool!r}：未在 PATH 中找到（请安装或配置，不要静默跳过）")


@dataclass
class ToolResult:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    timed_out: bool = False
    cmd: tuple = ()

    def brief(self, n: int = 160) -> str:
        tail = (self.stderr or self.stdout or "").strip().replace("\n", " ")
        return f"rc={self.returncode}{'(超时)' if self.timed_out else ''} {tail[:n]}"


def which(tool: str) -> str | None:
    return shutil.which(tool)


def readiness(tools: tuple[str, ...] = OPTIONAL_TOOLS) -> dict:
    """就绪探测：哪些工具在、哪些不在。**缺件要能一眼看到**（GUI/CLI 都读它）。"""
    found = {t: bool(which(t)) for t in tools}
    missing = [t for t, okk in found.items() if not okk]
    return {"tools": found, "missing": missing,
            "all_present": not missing,
            "note": ("缺少的可选工具会禁用对应能力（例如没有 ffmpeg → 不能合成媒体），"
                     "但**不会**让引擎崩；缺件时相关环境会被明确告知不可用")}


def safe_output_name(base: str, suffix: str, *, salt: str = "") -> str:
    """给子进程用的**纯 ASCII 输出名**（内容哈希 + 固定后缀），避免中文路径导致的解码崩。"""
    h = hashlib.sha256(f"{base}|{suffix}|{salt}".encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{h}{suffix}"


def run_tool(tool: str, args: list[str], *, timeout: float = 300.0, workdir=None,
             check_exists: bool = True, env_extra: dict | None = None,
             text: bool = True) -> ToolResult:
    """跑一个外部工具（**同步**；要并发就用执行面的线程池包它）。

    * `check_exists=True` 时缺件直接抛 `ToolMissing`（硬报错）；
    * 子进程环境：`LC_ALL=C.UTF-8` + `PYTHONIOENCODING=utf-8`（**不改自身进程的 locale**）；
    * 输出一律以 UTF-8 解码、坏字节替换（不因中文输出崩）。
    """
    exe = which(tool) if check_exists else (tool or None)
    if check_exists and not exe:
        raise ToolMissing(tool)
    cmd = [exe or tool] + [str(a) for a in (args or [])]
    env = dict(os.environ)
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(env_extra or {})
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=float(timeout),
                             cwd=str(workdir) if workdir else None, env=env,
                             encoding="utf-8" if text else None,
                             errors="replace" if text else None)
        out = proc.stdout if text else ""
        err = proc.stderr if text else ""
        return ToolResult(ok=proc.returncode == 0, returncode=int(proc.returncode),
                          stdout=str(out or ""), stderr=str(err or ""),
                          elapsed=time.monotonic() - t0, cmd=tuple(cmd))
    except subprocess.TimeoutExpired as e:
        return ToolResult(ok=False, returncode=-1,
                          stdout=str(e.stdout or ""), stderr=f"超时（{timeout}s）",
                          elapsed=time.monotonic() - t0, timed_out=True, cmd=tuple(cmd))
    except FileNotFoundError as e:                     # 显式给路径但不存在
        raise ToolMissing(tool) from e
    except Exception as e:
        return ToolResult(ok=False, returncode=-2, stderr=f"{type(e).__name__}: {e}",
                          elapsed=time.monotonic() - t0, cmd=tuple(cmd))

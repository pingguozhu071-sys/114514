# -*- coding: utf-8 -*-
"""`python -m daedalus ...` 与打包后的**同一个入口**（源码态与打包态行为一致）

打包出两个 EXE 共用这一份入口（见 `packaging/daedalus.spec`）：
  * `daedalus.exe`     —— **窗口子系统**（双击即开图形界面，不弹黑窗）
  * `daedalus-cli.exe` —— **控制台子系统**（能在管道里用、能打印 JSON）

怎么区分？Windows 下窗口子系统的进程**没有控制台**，于是 `sys.stdout` 是 `None`——
这就是判据（不靠文件名、不靠参数，双击与命令行都自洽）：
  * 有控制台 → 走 CLI（`daedalus-cli collect ...`）
  * 没有控制台 → 起图形界面（双击 `daedalus.exe`）
"""

from __future__ import annotations

import sys


def _has_console() -> bool:
    """窗口子系统进程没有控制台：`sys.stdout`/`sys.stderr` 会是 None。"""
    return (getattr(sys, "stdout", None) is not None
            and getattr(sys, "stderr", None) is not None)


def main() -> int:
    # 显式子命令优先：即便有人给窗口版传了参数，也照 CLI 语义执行（不会静默忽略参数）
    argv = list(sys.argv[1:])
    if argv:
        from daedalus.cli import main as cli_main
        return int(cli_main(argv))
    if not _has_console():
        from daedalus.ui.app import run_ui
        return int(run_ui())
    from daedalus.cli import main as cli_main
    return int(cli_main(argv))


if __name__ == "__main__":
    sys.exit(main())

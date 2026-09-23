# -*- coding: utf-8 -*-
# 由 tools/build.py 调用：python -m PyInstaller packaging/daedalus.spec --noconfirm
#
# 三条刻意的取舍（都写在 README 与本文件里，免得以后有人"顺手优化"掉）：
#   1) **one-dir 优先**：one-file 每次启动都要把几百 MB 解包到临时目录（慢，且杀软爱报毒）。
#      分发用安装器打包整个目录，用户看到的就是一个快捷方式——体验与单文件一样。
#   2) **两个 EXE 共用一份依赖**：`daedalus.exe` 是窗口子系统（GUI，无黑窗），
#      `daedalus-cli.exe` 是控制台子系统（CLI 要能打印/被管道读）。Windows 下一个 EXE
#      只能是其中一种，硬合成一个反而两边都难用。
#   3) **数据根绝不用 `sys._MEIPASS`**：那是解包临时目录，升级时会被清掉（Kiana 的真实事故）。
#      便携版靠 exe 同级的 `portable.flag`/`DaedalusData/` 判定（见 privacy/secrets.py）。

import pathlib

ROOT = pathlib.Path(SPECPATH).parent          # noqa: F821 - PyInstaller 注入
SRC = ROOT / "src"
ASSETS = ROOT / "assets"

# 显式收集的**数据文件**（资源缺失会让界面缺图/样式失效，所以宁可多收、并在构建期核对）
datas = [
    (str(ASSETS / "icon.ico"), "assets"),
    (str(ASSETS / "icon_256.png"), "assets"),
    (str(ROOT / "config.example.toml"), "."),
    (str(ROOT / "docs"), "docs"),             # 文档随包（用户能翻；也便于"随包自证"）
]

# 依赖里那些"靠隐式导入工作"的东西（不写进来，打包后会运行时缺件）
hiddenimports = [
    "qfluentwidgets",
    "PySide6.QtSvg", "PySide6.QtNetwork", "PySide6.QtPrintSupport",
    "playwright", "playwright.sync_api",
    "cv2", "numpy", "PIL", "PIL.Image",
    "sqlite3", "tomllib", "ctypes.wintypes", "email.utils", "html.parser",
    "daedalus.ui.app", "daedalus.ui.pages", "daedalus.ui.widgets",
    "daedalus.env.browser", "daedalus.understand.parsers.mediainfo",
]

# 明确排除（体积与启动时间）：这些被依赖间接带进来但用不到
excludes = [
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2",          # 只用 PySide6，别的 Qt 绑定一律不打包
    "pytest", "_pytest", "setuptools", "pip", "wheel",
    # 注意：**不要**排除 `playwright._impl._driver`——浏览器环境就靠它启动驱动进程
]

block_cipher = None

a = Analysis(                               # noqa: F821
    [str(SRC / "daedalus" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)     # noqa: F821

# ① GUI（窗口子系统：不弹黑窗）
exe_gui = EXE(                             # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="daedalus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                             # UPX 压缩会被杀软误报，宁可不压
    console=False,
    disable_windowed_traceback=False,      # 崩溃要能看到回溯（写入日志）
    icon=str(ASSETS / "icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

# ② CLI（控制台子系统：能被管道读、能打印 JSON）
exe_cli = EXE(                             # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="daedalus-cli",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ASSETS / "icon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(                            # noqa: F821
    exe_gui, exe_cli, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="daedalus",
)

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

# 插件子模块必须**逐个点名**：注册表改成运行时动态扫描后，静态分析看不见它们——
# 不点名就根本不进包（真实事故：冻结态解析器注册表 4 → 1，台账 B20-2）。
# 名单从源码树现算，所以「加一个解析器 = 只加一个文件」在打包态依然成立。
_pkg_dir = SRC / "daedalus" / "understand" / "parsers"
_ext_dir = SRC / "daedalus" / "adapters" / "extractors"
hiddenimports += [f"daedalus.understand.parsers.{p.stem}"
                  for p in sorted(_pkg_dir.glob("*.py")) if p.name != "__init__.py"]
hiddenimports += [f"daedalus.adapters.extractors.{p.stem}"
                  for p in sorted(_ext_dir.glob("*.py")) if p.name != "__init__.py"]

# 构建期插件清单（tools/build.py 生成）：冻结态运行时靠它**枚举**插件
# （pkgutil 扫不到 PYZ）。没生成就不打进去（build.py 的打包态冒烟会当场抓住）。
_build_gen = ROOT / "packaging" / "build_gen" / "plugin_manifest.json"
if _build_gen.exists():
    datas.append((str(_build_gen), "daedalus/understand"))

# 明确排除（体积与启动时间）：这些被依赖间接带进来但用不到
excludes = [
    "tkinter", "matplotlib", "pandas", "scipy", "IPython", "jupyter", "notebook",
    "PyQt5", "PyQt6", "PySide2",          # 只用 PySide6，别的 Qt 绑定一律不打包
    "pytest", "_pytest", "setuptools", "pip", "wheel",
    # ⚠️ **必须排除反检测改装的 playwright 分支**（patchright / rebrowser / undetected 等）：
    #    踩过的坑（重新打包时发现）：patchright 装了一个**名叫 `hook-playwright.sync_api.py`
    #    的 PyInstaller 钩子**，内容却是 `collect_data_files("patchright")` —— 于是打包时给
    #    `playwright.sync_api` 收的是**改装分支的驱动**：Python 侧是正版 1.62.0，
    #    驱动侧却是 patchright 1.61.1 的 node 包。结果是"**未改装的浏览器**"这条承诺
    #    在打包后**静默失效**（而且运行期路径检查看不出来，因为模块名仍然是 playwright）。
    #    这是本工程边界（docs/07）不允许的，所以从两端封：这里排除它，
    #    再由 `tools/build.py` 的产物核对**硬断言**包里没有分支文件。
    "patchright", "patchright._impl", "patchright._impl.__pyinstaller",   # noqa: lint -- 检测词表
    "rebrowser", "rebrowser_playwright", "undetected_playwright", "playwright_stealth",  # noqa: lint -- 检测词表
]

# ⚠️ 显式收集**正版 playwright 的驱动数据**（node 包 + browsers.json）。
# 不写这一条就会依赖 PyInstaller 自己找 hook —— 而那正是上面那个坑的入口：
# 找到了分支的 hook，就会收错驱动。显式收集 + 构建期断言 = 双保险。
try:
    from PyInstaller.utils.hooks import collect_data_files
    datas += collect_data_files("playwright")
except Exception:                                    # pragma: no cover - 极端环境
    pass

block_cipher = None

a = Analysis(                               # noqa: F821
    [str(SRC / "daedalus" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    # ⚠️ **本目录的钩子优先于分发版自带的钩子** —— 这是挡住
    #    "patchright 冒充 `hook-playwright.sync_api.py`"的唯一可靠手段（见 packaging/hooks/ 的说明）。
    #    `excludes` 挡不住它：那排除的是**模块图**，挡不住别人钩子里收的**数据文件**。
    hookspath=[str(ROOT / "packaging" / "hooks")],
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

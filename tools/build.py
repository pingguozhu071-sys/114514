# -*- coding: utf-8 -*-
"""构建：版本校验 → 安装器美术 → PyInstaller → NSIS 安装器 → 产物核对

    python tools/build.py --dry-run      # **先自证**：只打印会做什么 + 缺件结论，不写任何文件
    python tools/build.py                # 完整构建
    python tools/build.py --exe-only     # 只出 onedir（不上安装器）——**唯一**允许缺 NSIS 的形态
    python tools/build.py --verify-package   # 只核对已构建的产物（不重新构建）

四条纪律（对应《06》的交付门槛）：
  1) **缺件硬报错**：PyInstaller / NSIS / 图标 / 关键资源任一缺失 → 中止（不留"半成品看起来像成功"）；
     许可页三份文本还多一条**字节级硬断言**（必须带 BOM，否则许可页在向导里是乱码生僻字）；
  2) **dry-run 先自证**：`--dry-run` 打印每一步，**不创建任何文件**；
  3) **产物核对**：两个 EXE、`_internal`、资源、版本资源都在，少一个就报错；
  4) **少起子进程**：版本校验/美术/PyInstaller 全部**当模块调**（同一进程内，有真回溯、
     不经 shell）；只有 NSIS 的 `makensis.exe` 是外部二进制，用**字面量参数列表 + `shell=False`**
     调用（本机安全策略的硬要求，与"SQL 必须是字面量"同理）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
ART = PKG / "art"

# 与其它工具一致：把 src 放进 sys.path，才能 `import daedalus`（构建前要读版本、试导入）
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

NSIS_CANDIDATES = (
    r"C:\Program Files (x86)\NSIS\makensis.exe",
    r"C:\Program Files\NSIS\makensis.exe",
    str(pathlib.Path.home() / "scoop/apps/nsis/current/makensis.exe"),
)

REQUIRED_IN_BUNDLE = ("daedalus.exe", "daedalus-cli.exe", "_internal")
# 许可页文本三份（zh/ja/en，由 packaging/make_installer_art.py 生成）；**每份都必须带 BOM**
REQUIRED_LICENSES = ("license_zh.txt", "license_ja.txt", "license_en.txt")
# 打包面**必须有**的东西：正版 playwright 的驱动数据（浏览器环境靠它起驱动进程）。
# 踩过的坑：patchright 的 PyInstaller 钩子会冒充 `playwright.sync_api` 的 hook，
# 把**改装分支**的驱动收进包 → 必须显式收集 + 在这里硬断言。
REQUIRED_PLAYWRIGHT_DRIVER = "_internal/playwright/driver/package/browsers.json"
# 打包面**绝不该有**的东西：反检测改装分支（本工程边界不允许，且会静默改变浏览器行为）
FORBIDDEN_BUNDLE_DIRS = ("_internal/patchright", "_internal/rebrowser",  # noqa: lint -- 检测词表
                         "_internal/undetected_playwright",
                         "_internal/playwright_stealth")  # noqa: lint -- 检测词表
# 解包核对：这些**绝不该**出现在产物里（凭据/密钥/敏感配置）
FORBIDDEN_PATTERNS = (
    "cookies.txt", "cookies.json", "secret", "master.key", ".bin",
    "id_rsa", ".env", "config.toml", "daedalus.db", "settings.json",
)


def _ensure_bom(path: pathlib.Path) -> bool:
    """确保脚本是 **UTF-8 with BOM**（NSIS 3 只认带 BOM 的 UTF-8，否则报 `Bad text encoding`）。

    这是个真实踩到的坑：`.nsi` 里全是中文文案，存成无 BOM 的 UTF-8 → makensis 直接拒绝编译，
    而且报错信息只说"编码不对"，不说"要 BOM"。这里在编译前**幂等地**补上，并把动作打印出来
    （不静默改文件）；已经是 BOM 的就不动。
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return False
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    return True


def assert_license_bom(art: pathlib.Path = ART) -> list[str]:
    """**硬断言**三份许可文本都在、而且都带 BOM；不满足直接 `SystemExit`（绝不静默放行）。

    为什么要在构建期钉死：许可页乱码是**用户看得见**的交付缺陷，可它的成因只是**少了 3 个字节**
    （NSIS 的 `LicenseData` 对无 BOM 文件按**本机 ANSI 代码页**解码，简体中文机器上就是 GBK），
    在构建日志、产物核对、单元测试里全都看不出来 —— 只有真的打开安装向导才暴露。
    所以判据放在这里：前 3 字节 `EF BB BF`（UTF-8 BOM）或前 2 字节 `FF FE`（UTF-16LE BOM）。
    """
    bad: list[str] = []
    ok_lines: list[str] = []
    for name in REQUIRED_LICENSES:
        p = art / name
        if not p.exists():
            bad.append(f"{name} 不存在")
            continue
        head = p.read_bytes()[:4]
        if head[:3] == b"\xef\xbb\xbf" or head[:2] == b"\xff\xfe":
            ok_lines.append(f"{name}（{p.stat().st_size} 字节，前 4 字节 {head.hex(' ')}）")
        else:
            bad.append(f"{name} 无 BOM（前 4 字节 {head.hex(' ')}）")
    if bad:
        raise SystemExit(
            "许可文本不合格：NSIS 会按**本机 ANSI 代码页**解码无 BOM 的文件 → 许可页乱码生僻字。\n"
            + "\n".join(f"  ✗ {b}" for b in bad)
            + f"\n  目录：{art}\n"
            + "  修法：`python packaging/make_installer_art.py`（三份都会写成 UTF-8 with BOM）。")
    return ok_lines


def find_nsis() -> str:
    for p in NSIS_CANDIDATES:
        if pathlib.Path(p).exists():
            return p
    return shutil.which("makensis") or ""


def _load(path: pathlib.Path, name: str):
    """把仓库里的脚本当模块加载（**不起子进程**；出错有真回溯）。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"加载不了模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def check_version() -> tuple[bool, str]:
    vc = _load(ROOT / "tools" / "version_check.py", "daedalus_version_check")
    rep = vc.check(write=False)
    return bool(rep.get("ok")), str(rep.get("version", ""))


def preflight(*, need_nsis: bool) -> dict:
    """列出缺件（**不修、不装**）：这是"缺件硬报错"的判据来源。"""
    from daedalus import VERSION                     # noqa: F401 - 顺带验证包可导入
    miss: list[str] = []
    try:
        import PyInstaller                           # noqa: F401
        from PyInstaller.__main__ import run as _pyi_run   # noqa: F401
    except Exception:
        miss.append("PyInstaller（pip install pyinstaller）")
    if not (ROOT / "assets" / "icon.ico").exists():
        miss.append("assets/icon.ico（图标）")
    if not (ROOT / "config.example.toml").exists():
        miss.append("config.example.toml")
    if not (ROOT / "docs" / "07-能力边界.md").exists():
        miss.append("docs/07-能力边界.md（安装器许可页的来源）")
    nsis = find_nsis()
    if need_nsis and not nsis:
        miss.append("NSIS（makensis）—— 只出 EXE 请显式加 --exe-only")
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        miss.append("opencv-python / numpy（安装器美术需要）")
    return {"missing": miss, "nsis": nsis, "ok": not miss}


def _say(*parts: object) -> None:
    print("  → " + " ".join(str(p) for p in parts))


def build(*, dry: bool = False, exe_only: bool = False, skip_art: bool = False) -> int:
    print("Daedalus 构建")
    print("─" * 68)
    pf = preflight(need_nsis=not exe_only)
    ok_v, version = check_version()
    print(f"① 版本四件套：{'一致' if ok_v else '不一致'}（VERSION={version}）")
    if not ok_v:
        print("   中止：先跑 `python tools/version_check.py --write`")
        return 2
    print(f"② 缺件检查：{'齐备' if pf['ok'] else '缺少 ' + '、'.join(pf['missing'])}"
          + (f"（NSIS：{pf['nsis']}）" if pf["nsis"] else ""))
    if not pf["ok"]:
        print("   中止：**缺件硬报错**（要么补齐，要么显式 --exe-only）")
        return 2

    print(f"③ 安装器美术：{'跳过' if skip_art else '生成 packaging/art/（welcome.bmp / header.bmp / 三份许可文本）'}")
    if not skip_art:
        art = _load(PKG / "make_installer_art.py", "daedalus_installer_art")
        _say("模块调用", "packaging/make_installer_art.py", "main([])")
        if not dry:
            rc = int(art.main([]))
            if rc:
                print("   美术生成失败")
                return rc

    print("④ PyInstaller（one-dir；GUI + CLI 两个 EXE 共用一份依赖）")
    _say("模块调用 PyInstaller.__main__.run", "packaging/daedalus.spec", "--noconfirm",
         "--distpath", DIST)
    if not dry:
        from PyInstaller.__main__ import run as pyi_run
        try:
            rc = int(pyi_run([str(PKG / "daedalus.spec"), "--noconfirm",
                              "--distpath", str(DIST), "--workpath", str(ROOT / "build")]) or 0)
        except SystemExit as e:                       # PyInstaller 用 SystemExit 传退出码
            rc = int(e.code or 0)
        if rc:
            print(f"   PyInstaller 失败（退出码 {rc}）")
            return rc

    if exe_only:
        print("⑤ 安装器：跳过（--exe-only）")
    else:
        print("⑤ NSIS 安装器（唯一的子进程：makensis.exe，字面量参数列表 + shell=False）")
        # 调 makensis **之前**的编码硬断言：三份许可文本必须都在且带 BOM（dry-run 也查——这是自证）。
        for line in assert_license_bom():
            print(f"   · 许可文本 {line}")
        if not dry and _ensure_bom(PKG / "installer.nsi"):
            print("   · 已给 installer.nsi 补上 UTF-8 BOM（NSIS 3 只认带 BOM 的 UTF-8）")
        _say(pf["nsis"], "/V2", PKG / "installer.nsi")
        if not dry:
            # 字面量列表在**调用点**：不经 shell、argv 不先拼字符串（安全策略硬要求）
            r = subprocess.run([str(pf["nsis"]), "/V2", str(PKG / "installer.nsi")],
                               cwd=str(PKG), shell=False)
            if int(r.returncode):
                print(f"   makensis 失败（退出码 {r.returncode}）——看上面的行号")
                return int(r.returncode)

    print("⑥ 产物核对")
    if dry:
        print("   dry-run：跳过（没有任何产物被创建）")
        print("─" * 68)
        print("dry-run 完成：以上是**将要执行**的步骤；未创建任何文件。")
        return 0
    rep = verify_package(expect_setup=not exe_only)
    for line in rep["lines"]:
        print("   " + line)
    print("─" * 68)
    print("构建完成。" if rep["ok"] else "构建有问题（见上）。")
    return 0 if rep["ok"] else 1


def verify_package(*, expect_setup: bool = True) -> dict:
    """核对产物：必需的都在、禁止的都不在（**含浏览器驱动的来源核对**）。"""
    lines: list[str] = []
    ok = True
    bundle = DIST / "daedalus"
    if not bundle.is_dir():
        return {"ok": False, "lines": [f"✗ 缺少产物目录 {bundle}"]}
    for name in REQUIRED_IN_BUNDLE:
        p = bundle / name
        good = p.exists()
        ok = ok and good
        lines.append(f"{'✓' if good else '✗'} {name}")
    # 浏览器驱动的来源（硬断言）：正版必须有；改装分支绝不许有
    drv = bundle / REQUIRED_PLAYWRIGHT_DRIVER
    good = drv.exists()
    ok = ok and good
    lines.append(f"{'✓' if good else '✗'} 正版 playwright 驱动（{REQUIRED_PLAYWRIGHT_DRIVER}）")
    forks = [d for d in FORBIDDEN_BUNDLE_DIRS if (bundle / d).exists()]
    if forks:
        ok = False
        lines.append(f"✗ 包里出现**反检测改装分支**（本工程边界不允许）：{forks}")
    else:
        lines.append("✓ 包内无反检测改装分支（patchright/rebrowser/undetected…）")
    setup = sorted(DIST.glob("Daedalus-Setup-*.exe"))
    if expect_setup:
        good = bool(setup)
        ok = ok and good
        lines.append(f"{'✓' if good else '✗'} 安装器（{setup[0].name if setup else '未生成'}）")
    bad: list[str] = []
    for p in bundle.rglob("*"):
        if not p.is_file():
            continue
        low = p.name.lower()
        for pat in FORBIDDEN_PATTERNS:
            if pat in low:
                bad.append(str(p.relative_to(bundle)))
                break
    if bad:
        ok = False
        lines.append("✗ 产物里出现不该有的文件：" + "、".join(bad[:6]))
    else:
        lines.append("✓ 解包核对：无 cookie / 密钥 / 明文库 / 敏感配置")
    size_mb = sum(f.stat().st_size for f in bundle.rglob("*") if f.is_file()) / (1 << 20)
    lines.append(f"· 体积约 {size_mb:.0f} MB")
    return {"ok": ok, "lines": lines}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daedalus 构建（EXE + 自制安装器）")
    ap.add_argument("--dry-run", action="store_true", help="只打印步骤与缺件结论，不写文件")
    ap.add_argument("--exe-only", action="store_true", help="只出 onedir（不要求 NSIS）")
    ap.add_argument("--skip-art", action="store_true", help="跳过安装器美术生成")
    ap.add_argument("--verify-package", action="store_true", help="只核对已有产物")
    args = ap.parse_args(argv)
    if args.verify_package:
        rep = verify_package(expect_setup=True)
        for line in rep["lines"]:
            print(line)
        return 0 if rep["ok"] else 1
    return build(dry=args.dry_run, exe_only=args.exe_only, skip_art=args.skip_art)


if __name__ == "__main__":
    sys.exit(main())

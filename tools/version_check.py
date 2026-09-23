# -*- coding: utf-8 -*-
"""版本号**单一来源**与四件套校验（发版门禁）

唯一来源：`src/daedalus/__init__.py` 的 `VERSION`。**任何别处都不许硬编码版本号**。
四件套（都由这里从单一来源生成/校验）：
  1) Python 包内：`daedalus.VERSION` / `about()`（界面、CLI、报告都读它）
  2) 安装脚本：`packaging/version.nsh`（NSIS 的 `APP_VERSION` + 数字版 `VIProductVersion`；
     **带 UTF-8 BOM** —— NSIS 3 对无 BOM 文件按本机 ANSI 代码页解码）
  3) 可执行文件属性：`packaging/version_info.txt`（PyInstaller `--version-file`）
  4) 发行元数据：`pyproject.toml` 的 `version`（打包成 wheel/sdist 时用）

用法：
    python tools/version_check.py            # 校验（不一致 → 退出码 1）
    python tools/version_check.py --write    # 从单一来源重新生成 2/3/4
    python tools/version_check.py --json     # 机器可读
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

VERSION_PY = ROOT / "src" / "daedalus" / "__init__.py"
NSH = ROOT / "packaging" / "version.nsh"
VERSION_INFO = ROOT / "packaging" / "version_info.txt"
PYPROJECT = ROOT / "pyproject.toml"

# 每个消费方写盘用的编码（**不是随手选的**）：
#   * `version.nsh` 用 **utf-8-sig（带 BOM）**：它被 `installer.nsi` `!include`，而 NSIS 3 对
#     **无 BOM** 的文件按本机 ANSI 代码页解码 —— 里面的中文注释在这台机器上是 `# ...`（碰巧没事），
#     但同一份文件里只要出现一个中文字符串就是乱码事故。BOM 是唯一的编码声明方式（NSIS 无 pragma）。
#   * `pyproject.toml` **绝不能带 BOM**：`tomllib` 见到 BOM 直接抛 TOMLDecodeError，装包就废。
#   * `version_info.txt` 由 PyInstaller 读（它自己按 utf-8 处理），维持不带 BOM。
TARGET_ENCODING = {NSH: "utf-8-sig", VERSION_INFO: "utf-8", PYPROJECT: "utf-8"}


def has_bom(path: pathlib.Path) -> bool:
    """前 3 字节是不是 UTF-8 BOM（`utf-8-sig` 读盘时会把 BOM 吃掉，所以必须另查字节）。"""
    try:
        return path.read_bytes()[:3] == b"\xef\xbb\xbf"
    except OSError:
        return False


def source_version() -> str:
    """从唯一来源读版本（**不 import**：避免带起整包依赖，也避免循环）。"""
    text = VERSION_PY.read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"在 {VERSION_PY} 里找不到 VERSION 字面量")
    return m.group(1)


def numeric_version(version: str) -> tuple[int, int, int, int]:
    """`0.1.0.dev0` → `(0,1,0,0)`：Windows 版本资源必须是四个整数。

    预发布后缀（dev/rc）映射到第 4 段（越低越早），这样"开发版 < 正式版"在文件属性里也成立。
    """
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[.\-+]?(.*))?$", version.strip())
    if not m:
        raise SystemExit(f"版本号不符合 x.y.z[.suffix] 形式：{version!r}")
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    tail = (m.group(4) or "").lower()
    build = 0
    if tail:
        n = re.search(r"(\d+)", tail)
        build = int(n.group(1)) if n else 0
        if tail.startswith(("dev", "a", "b", "rc")):
            build = max(0, build)          # 预发布：第 4 段给 0（正式版也是 0，但文件属性另有说明）
    return major, minor, patch, build


def render_nsh(version: str) -> str:
    a, b, c, d = numeric_version(version)
    return (
        "# 本文件由 tools/version_check.py --write 生成，**不要手改**\n"
        "# 单一来源：src/daedalus/__init__.py 的 VERSION\n"
        f"!define APP_VERSION \"{version}\"\n"
        f"!define APP_VERSION_NUMERIC \"{a}.{b}.{c}.{d}\"\n"
        f"!define APP_VERSION_MAJOR {a}\n"
        f"!define APP_VERSION_MINOR {b}\n"
        f"!define APP_VERSION_PATCH {c}\n"
        f"!define APP_VERSION_BUILD {d}\n")


def render_version_info(version: str, *, name: str = "Daedalus") -> str:
    a, b, c, d = numeric_version(version)
    return (
        "# 由 tools/version_check.py --write 生成（PyInstaller --version-file 用）\n"
        "VSVersionInfo(\n"
        f"  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, {d}), prodvers=({a}, {b}, {c}, {d}),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[StringFileInfo([StringTable('040904B0', [\n"
        f"      StringStruct('CompanyName', '{name}'),\n"
        f"      StringStruct('FileDescription', '{name} · 统一采集与感知引擎'),\n"
        f"      StringStruct('FileVersion', '{version}'),\n"
        f"      StringStruct('InternalName', '{name}'),\n"
        f"      StringStruct('OriginalFilename', '{name}.exe'),\n"
        f"      StringStruct('ProductName', '{name}'),\n"
        f"      StringStruct('ProductVersion', '{version}'),\n"
        "      StringStruct('LegalCopyright', '')])]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")


def render_pyproject(version: str) -> str:
    return (
        "[build-system]\n"
        'requires = ["setuptools>=68", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "daedalus-acq"\n'
        f'version = "{version}"\n'
        'description = "统一采集与感知引擎（不是爬虫）"\n'
        'requires-python = ">=3.11"\n'
        'dependencies = []\n\n'
        "[tool.setuptools]\n"
        'package-dir = {"" = "src"}\n\n'
        "[tool.setuptools.packages.find]\n"
        'where = ["src"]\n')


_STRAY = re.compile(r"""["'](\d+\.\d+\.\d+(?:[.\-+][0-9A-Za-z.\-]+)?)["']""")
_SKIP_DIRS = {"__pycache__", ".git", "out", "dist", "build", "node_modules", ".zcode",
              "CLAUDE-SECURITY-2", "samples"}


def stray_literals(version: str) -> list[str]:
    """全仓库找**其他**硬编码的版本字面量（单一来源之外的不许有）。

    只查 `.py`（生成物 `version.nsh`/`version_info.txt`/`pyproject.toml` 本来就是它的副本，
    由本脚本负责同步，不算漂移）。单一来源自己的那一行定义当然要跳过。
    """
    hits: list[str] = []
    for p in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tools").rglob("*.py")) \
            + sorted((ROOT / "tests").rglob("*.py")) + sorted((ROOT / "packaging").rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        is_source = p.resolve() == VERSION_PY.resolve()
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if is_source and re.match(r"^\s*VERSION\s*=", line):
                continue                       # ← 这一行就是单一来源本身
            for m in _STRAY.finditer(line):
                found = m.group(1)
                if found == version:
                    hits.append(f"{p.relative_to(ROOT)}:{i} 出现与单一来源相同的字面量（应改为读 VERSION）")
    return hits


def check(*, write: bool = False) -> dict:
    version = source_version()
    want_nsh = render_nsh(version)
    want_vi = render_version_info(version)
    want_pp = render_pyproject(version)
    report: dict = {"version": version, "numeric": list(numeric_version(version)),
                    "consumers": {}, "stray": [], "ok": False}
    targets = [(NSH, want_nsh), (VERSION_INFO, want_vi), (PYPROJECT, want_pp)]
    for path, want in targets:
        enc = TARGET_ENCODING[path]
        want_bom = enc == "utf-8-sig"
        # `utf-8-sig` 读盘会把 BOM 吃掉 → 「内容相同」**不足以**说明文件是对的：
        # 少了 BOM 的 version.nsh 内容一模一样，却会让 NSIS 按本机代码页解它 → 必须单独查字节。
        cur = path.read_text(encoding=enc) if path.exists() else ""
        bom_ok = has_bom(path) if want_bom else True
        same = (cur == want) and bom_ok
        key = path.relative_to(ROOT).as_posix()
        report["consumers"][key] = {"exists": path.exists(), "in_sync": same,
                                    "bom": has_bom(path) if want_bom else None}
        if write and not same:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want, encoding=enc)          # utf-8-sig = 写盘时带上 BOM
            report["consumers"][key]["written"] = True
            report["consumers"][key]["in_sync"] = True
            report["consumers"][key]["bom"] = has_bom(path) if want_bom else None
        elif not same:
            report["consumers"][key]["note"] = ("与单一来源不一致" if cur != want else
                                              "缺 UTF-8 BOM（NSIS 按本机 ANSI 代码页解它）")
    report["stray"] = stray_literals(version)
    report["ok"] = (all(c.get("in_sync") for c in report["consumers"].values())
                    and not report["stray"])
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="版本号单一来源与四件套校验")
    ap.add_argument("--write", action="store_true", help="从单一来源重新生成各消费方")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = check(write=args.write)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"单一来源 VERSION = {rep['version']}（数字版 {'/'.join(map(str, rep['numeric']))}）")
        for name, body in rep["consumers"].items():
            mark = "OK  " if body.get("in_sync") else "FAIL"
            extra = "（已重新生成）" if body.get("written") else \
                ("（" + body.get("note", "") + "）" if body.get("note") else "")
            print(f"  [{mark}] {name}{extra}")
        if rep["stray"]:
            print("  硬编码版本字面量：")
            for s in rep["stray"]:
                print(f"    {s}")
        print(f"结论：{'四件套一致' if rep['ok'] else '不一致（跑 --write 同步，或改掉硬编码）'}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

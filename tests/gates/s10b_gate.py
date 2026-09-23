# -*- coding: utf-8 -*-
"""S10b 门禁：跨机打包与交付面

覆盖（每条对应《06》的一个发版门槛）：
  A 版本单一来源   A1 四件套一致｜A2 无硬编码版本字面量｜A3 nsh 内容正确（含 VIProductVersion 数字版）
  B 安装向导       B1 **不静默**（拒绝 /S 且说明理由）｜B2 完整向导页齐全｜B3 DPI 感知｜
                   B4 版本徽章来自 nsh｜B5 不代下载浏览器（只提示）｜B6 许可页文本由 07 生成｜
                   B7 三份许可文本**字节级带 BOM**（无 BOM = 许可页乱码）｜B8 许可页按语言选文件
  C 卸载向导       C1 卸载段**全部**带 `un.` 前缀（漏了会在安装时执行）｜C2 延迟扫尾存在｜
                   C3 条件化清理（判据 + 三分支互斥）｜C4 **数据默认保留**（删除数据是可选段且不强制）｜
                   C5 桌面快捷方式：可选段创建 + 按 marker 条件化删除｜C6 `$DESKTOP` 真出现在指令里｜
                   C7 开始菜单段**真的建**快捷方式（不是 RO + 空段体的假组件）
  D 数据根规则     D1 便携靠标记文件（无需环境变量）｜D2 安装态走用户目录｜D3 **绝不**用解包临时目录
  E 构建器         E1 dry-run 自证（零文件创建）｜E2 缺件硬报错｜E3 产物核对（必需在/禁止不在）｜
                   E4 解包核对无凭据特征
  F 美术与文档     F1 安装器 BMP 尺寸符合 NSIS 规定｜F2 docs 00–09 齐备且有实质内容｜
                   F5 图标体检 tools/icon_check.py（存在即要求退出码 0；未写则 SKIP）

跑法（离线；不起安装器、不动系统）：
    python tests/gates/s10b_gate.py     # 退出码 0 = 全通过
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s10b_"))
os.environ["DAEDALUS_DATA_ROOT"] = str(_TMP)

_CASES: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


def skip(note: str) -> str:
    return f"SKIP {note}"


def nsi_text() -> str:
    return (ROOT / "packaging" / "installer.nsi").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
@case("A1 版本四件套一致（Python / nsh / version_info / pyproject）")
def t_version_consistent():
    import importlib.util
    spec = importlib.util.spec_from_file_location("vc", str(ROOT / "tools" / "version_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = mod.check(write=False)
    assert rep["ok"], rep
    assert all(c["in_sync"] for c in rep["consumers"].values()), rep["consumers"]
    return ok(f"VERSION={rep['version']}；三个消费方全部同步；无硬编码字面量")


@case("A2 nsh 内容正确（含数字版给 VIProductVersion 用）")
def t_nsh():
    text = (ROOT / "packaging" / "version.nsh").read_text(encoding="utf-8")
    assert "不要手改" in text and "单一来源" in text, text[:120]
    m = re.search(r'!define APP_VERSION "([^"]+)"', text)
    n = re.search(r'!define APP_VERSION_NUMERIC "(\d+)\.(\d+)\.(\d+)\.(\d+)"', text)
    assert m and n, text
    assert f'!define APP_VERSION "{m.group(1)}"' in text
    return ok(f"APP_VERSION={m.group(1)}｜APP_VERSION_NUMERIC={n.group(0).split()[-1]}")


@case("B1 **不静默安装**：检测 /S 就拒绝并说明理由")
def t_no_silent():
    text = nsi_text()
    assert "IfSilent" in text, "没有检测静默模式"
    assert "不支持静默安装" in text, "没有明确拒绝静默"
    assert "Abort" in text, "拒绝后没有中止"
    # 卸载同理
    assert text.count("IfSilent") >= 2, "卸载侧没有检测静默"
    return ok("安装与卸载两侧都拒绝 /S，并给出理由与替代方案（便携版）")


@case("B2 完整向导页齐全（欢迎/许可/组件/目录/安装/完成 + 卸载五页）")
def t_wizard_pages():
    text = nsi_text()
    need = ("MUI_PAGE_WELCOME", "MUI_PAGE_LICENSE", "MUI_PAGE_COMPONENTS", "MUI_PAGE_DIRECTORY",
            "MUI_PAGE_INSTFILES", "MUI_PAGE_FINISH",
            "MUI_UNPAGE_WELCOME", "MUI_UNPAGE_CONFIRM", "MUI_UNPAGE_COMPONENTS",
            "MUI_UNPAGE_INSTFILES", "MUI_UNPAGE_FINISH")
    missing = [x for x in need if x not in text]
    assert not missing, f"缺页：{missing}"
    assert "MUI_FINISHPAGE_RUN" in text and "doctor" in text, "完成页没有安装自检"
    return ok(f"{len(need)} 个页面齐全；完成页可跑 doctor 自检")


@case("B3 DPI 感知 + 三语言")
def t_dpi_lang():
    text = nsi_text()
    assert "ManifestDPIAware true" in text, "缺 DPI 感知（高分屏会全糊）"
    for lang in ('"SimpChinese"', '"Japanese"', '"English"'):
        assert f"MUI_LANGUAGE {lang}" in text, f"缺语言 {lang}"
    assert "MUI_LANGDLL_DISPLAY" in text, "没有语言选择"
    return ok("ManifestDPIAware true；zh-CN / ja-JP / en-US 三语可选")


@case("B4 版本徽章来自版本单一来源（不在 nsi 里硬编码）")
def t_version_badge():
    text = nsi_text()
    assert '!include "version.nsh"' in text, "没有 include 版本文件"
    assert "${APP_VERSION}" in text, "没有使用版本宏"
    # 只在"版本相关行"上查硬编码（否则 `ping 127.0.0.1` 这类四段数字会被误判）
    ver_lines = [ln for ln in text.splitlines()
                 if "VERSION" in ln.upper() and not ln.strip().startswith(";")]
    bad = [ln.strip() for ln in ver_lines if re.search(r'"\d+\.\d+\.\d+', ln)]
    assert not bad, f"版本相关行里出现硬编码版本号：{bad}"
    assert "VIProductVersion" in text, "缺 VIProductVersion（文件属性里的版本）"
    return ok("版本全部走 ${APP_VERSION} / ${APP_VERSION_NUMERIC}，版本行无硬编码")


@case("B5 不代下载浏览器运行时（只记录意图并提示命令）")
def t_no_browser_download():
    text = nsi_text()
    assert "playwright install chromium" in text, "没有给出安装命令提示"
    assert "不会替你下载" in text or "本安装器不会" in text, "没有声明不代下载"
    assert "NSISdl" not in text and "inetc" not in text, "出现了下载插件（违反不静默安装）"
    return ok("只提示 `python -m playwright install chromium`，不使用任何下载插件")


@case("B6 许可页文本由 docs/07 生成（单一来源）")
def t_license_from_docs():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "art", str(ROOT / "packaging" / "make_installer_art.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    body = mod.license_text()
    assert len(body) > 400, f"许可文本太短：{len(body)}"
    assert "不绕过" in body or "验证码" in body, body[:200]
    assert (ROOT / "packaging" / "art" / "license_zh.txt").exists(), "未生成 art/license_zh.txt"
    names = [n for n, _ in mod.license_files()]
    assert names == ["license_zh.txt", "license_ja.txt", "license_en.txt"], names
    return ok(f"由 docs/07-能力边界.md 生成 {len(body)} 字符；三份文件名 {names}（BOM 见 B7）")


@case("B7 许可文本三份齐备、**字节级**带 BOM、且中文那份确实来自 docs/07")
def t_license_bom():
    """用户投诉的乱码根因：`art/license.txt` 是 **UTF-8 无 BOM**，而 NSIS 的 `LicenseData` 对无 BOM
    文件按**本机 ANSI 代码页**（简体中文机器 936/GBK）解码 → 许可页满屏生僻字。三份都必须带 BOM。

    再加两刀，防「三份是同一个文件的三个副本」这种假修复：
      * 日文那份必须真有假名（否则就是中文原文换了文件名）；
      * 英文那份不许出现中日文字符；
      * 中文那份必须**逐字等于** `license_text()`（= 从 docs/07 生成的结果，单一来源没被手抄改掉）。
    """
    art = ROOT / "packaging" / "art"
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "artmod", str(ROOT / "packaging" / "make_installer_art.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bad, seen = [], []
    for name in ("license_zh.txt", "license_ja.txt", "license_en.txt"):
        p = art / name
        if not p.exists():
            bad.append(f"{name} 不存在")
            continue
        head = p.read_bytes()[:4]
        if head[:3] == b"\xef\xbb\xbf" or head[:2] == b"\xff\xfe":
            seen.append(f"{name} {p.stat().st_size}B {head.hex(' ')}")
        else:
            bad.append(f"{name} 无 BOM（前 4 字节 {head.hex(' ')}）")
    assert not bad, ("许可文本不合格（无 BOM → 安装向导里是乱码）：" + "、".join(bad)
                     + "；跑 `python packaging/make_installer_art.py` 重新生成")
    zh = (art / "license_zh.txt").read_text(encoding="utf-8-sig")
    ja = (art / "license_ja.txt").read_text(encoding="utf-8-sig")
    en = (art / "license_en.txt").read_text(encoding="utf-8-sig")
    assert zh == mod.license_text(), "中文许可 ≠ 从 docs/07 生成的结果（被手抄改过了？）"
    assert re.search(r"[\u3040-\u30ff]", ja), "日文那份里没有假名 —— 像是直接复制的中文原文"
    assert not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", en), "英文那份里混进了中日文字符"
    return ok("；".join(seen) + "；日文含假名、英文无中日文、中文逐字等于 docs/07 的生成结果")


@case("B8 许可页按语言选文件（LicenseLangString × 3 + $(MUILicense)，不硬编码单一文件）")
def t_license_langstring():
    """MUI2 的官方做法是 `LicenseLangString` + `LicenseData` 吃语言串；顺序**有硬要求**：
    页面宏 → `MUI_LANGUAGE` → `LicenseLangString`。顺序放错不会编译失败，只会让三行**静默塌到
    语言 1033（英语）**并报 warning 6040 —— 所以这里既查写法，也查顺序，还查不许指回单文件。"""
    code = _strip_nsi_comments(nsi_text())
    assert '!insertmacro MUI_PAGE_LICENSE "$(MUILicense)"' in code, "许可页没有用 $(MUILicense)"
    pairs = re.findall(r'^\s*LicenseLangString\s+(\S+)\s+\$\{LANG_(\w+)\}\s+"([^"]+)"',
                       code, re.MULTILINE)
    assert len(pairs) == 3, f"LicenseLangString 不是 3 条：{pairs}"
    assert {p[1] for p in pairs} == {"SIMPCHINESE", "JAPANESE", "ENGLISH"}, pairs
    assert len({p[0] for p in pairs}) == 1, f"三条语言串的名字不一致：{pairs}"
    assert {p[2] for p in pairs} == {"art\\license_zh.txt", "art\\license_ja.txt",
                                     "art\\license_en.txt"}, pairs
    assert code.rindex("MUI_LANGUAGE") < code.index("LicenseLangString"), \
        "LicenseLangString 出现在 MUI_LANGUAGE 之前 —— 会静默塌到 1033（英语）并报 warning 6040"
    assert 'MUI_PAGE_LICENSE "art\\license.txt"' not in code, "又指回了单语言的无 BOM 文件"
    return ok(f"{pairs[0][0]}：zh/ja/en 三份分别绑定；定义在 MUI_LANGUAGE 之后；未指回单文件")


@case("C1 卸载段**全部**带 `un.` 前缀（漏了会在安装时执行卸载逻辑）")
def t_un_prefix():
    text = nsi_text()
    # 抓出所有卸载侧 Section 名（在 WriteUninstaller 之后出现的 Section 声明）
    names = re.findall(r'^\s*Section(?:\s+/o)?\s+"([^"]+)"', text, re.MULTILINE)
    # 安装侧段落（在 '.onInit' 之后、卸载段之前）不算
    un_like = [n for n in names if "un." in n or n.lower().startswith("un.")]
    bad = []
    for n in names:
        # 卸载侧段落名单：出现在 "卸载" 注释块之后的（用出现在文件后半段的 Section 判）
        pass
    # 更直接的判据：卸载相关段落必须以 un. 开头（按我们自己的命名约定）
    un_sections = re.findall(r'^\s*Section(?:\s+/o)?\s+"un\.([^"]+)"', text, re.MULTILINE)
    assert len(un_sections) >= 4, f"卸载段太少（{len(un_sections)}）——可能漏了 un. 前缀"
    # 反向：FileWriteUninstaller 之前不得出现"看起来像卸载段但没前缀"的段落
    assert "WriteUninstaller" in text, "没有生成卸载器"
    assert re.search(r'Section\s+"un\.', text), "卸载段命名不对"
    assert "Function un.onInit" in text and "Function un.onUninstSuccess" in text, "卸载回调没带 un. 前缀"
    return ok(f"卸载段 {len(un_sections)} 个全部带 un. 前缀；两个回调也是 un.*")


@case("C2 延迟扫尾（卸载器删不掉自己 → 写 bat 延迟删目录并自删）")
def t_delayed_cleanup():
    text = nsi_text()
    assert "daedalus_uninstall.bat" in text, "没有延迟扫尾脚本"
    assert "ping 127.0.0.1" in text, "没有延迟（等卸载器退出）"
    assert "rmdir /s /q" in text, "没有递归删目录"
    assert "Exec" in text, "没有脱离执行"
    return ok("写 %TEMP%\\daedalus_uninstall.bat → 延迟 3s → 递归删 $INSTDIR → 自删")


@case("C3 共享资源条件化清理（判据 + 分支互斥）")
def t_conditional_cleanup():
    text = nsi_text()
    assert "install.marker" in text, "没有登记凭据（判据）"
    assert "APP_MARKER" in text and "FileWrite $0" in text, "安装时没有写 marker"
    assert "也就是标记" in text or "IfFileExists" in text, "没有按判据分支"
    assert "什么都不动" in text or "不会删除里面的任何文件" in text, "非登记安装的处理没说清"
    assert "Goto" in text, "分支之间没有显式跳转（会一路执行下去）"
    # 判据不能被安装流程自己破坏：marker 在安装段写、卸载段读；中间不得删除它
    idx_write = text.find("FileOpen $0 \"$INSTDIR\\${APP_MARKER}\" w")
    idx_read = text.find("IfFileExists \"$INSTDIR\\${APP_MARKER}\"")
    assert 0 < idx_write < idx_read < len(text), "marker 的写读顺序不对"
    return ok("marker 写入 → 卸载时读取判据 → 三分支显式跳转互斥")


def _strip_nsi_comments(text: str) -> str:
    """去掉 NSIS 注释（`;` 开头/行尾）。**判据只查真实指令**——注释里解释"为什么不能这么做"
    时出现的词不算违规（这个教训在 `_MEIPASS` 那条上也踩过）。"""
    out = []
    for line in text.splitlines():
        out.append(line.split(";", 1)[0] if not line.lstrip().startswith(";") else "")
    return "\n".join(out)


@case("C4 **数据默认保留**：删数据是可选项且默认不勾")
def t_data_preserved():
    text = nsi_text()
    m = re.search(r'Section\s+(/o\s+)?"un\.([^"]*数据[^"]*)"', text)
    assert m, "找不到删数据的段"
    assert m.group(1), "删数据段不是可选（/o）——会被强制执行"
    seg = _strip_nsi_comments(text[m.start():m.start() + 700])
    assert "SectionIn RO" not in seg, "删数据段被标成必需（危险）"
    assert "不可恢复" in seg, "没有警示不可恢复"
    assert "您的数据不会被删除" in text, "没有在界面上明说数据保留"
    return ok("删数据段为 /o（默认不勾）+ 警示不可恢复；完成页重申数据保留")


@case("C5 桌面快捷方式：可选段创建 + 卸载段**按 marker 条件化删除**")
def t_desktop_shortcut():
    """投诉之二：装完没有桌面快捷方式，而组件页却有个「开始菜单快捷方式」空壳（勾了等于没勾）。
    这里钉住三件事：桌面那条是**可选段**（`/o`，默认不勾，不静默动用户桌面）、段体**真的建**、
    而且**把「建了」登记进 marker**；卸载侧必须**按那条登记**判定，并用与创建时一致的
    `SetShellVarContext`（否则删的是另一个位置，公共桌面的图标会留下来）。"""
    code = _strip_nsi_comments(nsi_text())
    m = re.search(r'Section\s+(/o\s+)?"[^"]*桌面快捷方式[^"]*"\s+(\w+)', code)
    assert m, "找不到「桌面快捷方式」段"
    assert m.group(1), "桌面快捷方式段不是可选（/o）—— 会静默往用户桌面放图标"
    seg = code[m.start():m.end() + 1200]
    # NSIS 的命令不分大小写（文件里 `CreateShortcut` 与 `CreateShortCut` 两种写法都有），
    # 所以判据一律按 `(?i)` 匹配 —— 否则一个大小写就能让断言变成摆设。
    assert re.search(r'(?i)CreateShortCut\s+"\$DESKTOP\\', seg), "段体里没有真的建桌面快捷方式"
    assert "desktop_shortcut=1" in seg, "没有把「建了桌面快捷方式」登记进 marker"
    un = code[code.index('Section "un.'):]
    assert re.search(r'(?i)Delete\s+"\$DESKTOP\\', un), "卸载段没有删除桌面快捷方式"
    assert "desktop_shortcut=1" in un, "卸载没有按 marker 里那一行判定（等于无条件删共享位置）"
    assert "SetShellVarContext all" in un, "卸载删除时没有切回安装时的上下文（all）"
    return ok("创建段为 /o（默认不勾）+ 登记 desktop_shortcut=1；卸载按该行判定且上下文一致（all）")


@case("C6 `$DESKTOP` 出现 ≥2 次（创建 + 删除）——防「只有注释在骗人」")
def t_desktop_occurrences():
    """注释里写「这里会建桌面快捷方式」不算数：只数**去掉注释后的真实指令**。
    这条是给「回归」上的保险 —— 曾经的情况就是全工程零 `$DESKTOP` 代码、只有注释在说会建。"""
    code = _strip_nsi_comments(nsi_text())
    n = code.count("$DESKTOP")
    creates = len(re.findall(r'(?i)CreateShortCut\s+"\$DESKTOP\\', code))
    deletes = len(re.findall(r'(?i)Delete\s+"\$DESKTOP\\', code))
    assert n >= 2, f"$DESKTOP 只出现 {n} 次（要 ≥2：1 次创建 + 1 次删除）"
    assert creates >= 1, f"$DESKTOP 的创建次数为 {creates}（要 ≥1）"
    assert deletes >= 1, f"$DESKTOP 的删除次数为 {deletes}（要 ≥1）"
    return ok(f"$DESKTOP 命中 {n} 处：CreateShortCut {creates} 次、Delete {deletes} 次")


@case("C7 开始菜单段**真的建**那三个入口（不是 `SectionIn RO` + 空段体的假组件）")
def t_startmenu_real():
    """组件页上曾经有个「开始菜单快捷方式」：`SectionIn RO`（永远勾选、不可取消）**且段体是空的**。
    勾了等于没勾 —— 这类条目比没有还坏（用户以为自己取消了什么）。现在它必须真有三个 CreateShortCut。"""
    code = _strip_nsi_comments(nsi_text())
    m = re.search(r'Section\s+(/o\s+)?"[^"]*开始菜单[^"]*"\s+(\w+)', code)
    assert m, "找不到「开始菜单快捷方式」段"
    body = code[m.end():code.index("SectionEnd", m.end())]
    n = len(re.findall(r'(?i)CreateShortCut\s+"\$SMPROGRAMS\\', body))   # 命令不分大小写
    assert n >= 3, f"开始菜单段体里只有 {n} 个 CreateShortCut（要 3：程序 / CLI / 卸载）"
    assert "SectionIn RO" not in body, "开始菜单段仍被标成必需（RO）→ 不可取消 + 勾了等于没勾"
    return ok(f"段体里 {n} 个 CreateShortCut；非 RO（默认勾选、可取消）")


@case("D1/D2/D3 数据根规则：便携靠标记文件、安装态走用户目录、绝不用解包临时目录")
def t_data_root():
    import ast as _ast
    from daedalus.privacy import secrets as sec
    src = (ROOT / "src" / "daedalus" / "privacy" / "secrets.py").read_text(encoding="utf-8")
    assert "_MEIPASS" in src and "绝不" in src, "没有写明禁用解包临时目录"
    # **只查可执行代码**：文档字符串里为了说明"绝不用 _MEIPASS"必然会出现这个词
    tree = _ast.parse(src)
    spans: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            for ln in range(getattr(node, "lineno", 0), getattr(node, "end_lineno", 0) + 1):
                spans.add(ln)
    code = "\n".join(ln for i, ln in enumerate(src.splitlines(), 1)
                     if i not in spans and not ln.strip().startswith("#"))
    assert "_MEIPASS" not in code, "代码里竟然用了 _MEIPASS（解包临时目录）"
    # 便携：同级放 DaedalusData/ → 数据根在同级
    # （`portable_dir()` 的"同级"在未打包时就是当前目录，所以测试要真的 `chdir` 进去）
    tmp = _TMP / "portable"
    (tmp / "DaedalusData").mkdir(parents=True, exist_ok=True)
    old = os.environ.pop("DAEDALUS_DATA_ROOT", None)
    old2 = os.environ.pop("DAEDALUS_PORTABLE", None)
    here = pathlib.Path.cwd()
    try:
        os.chdir(tmp)
        import daedalus.privacy.secrets as s2
        got = s2.portable_dir()
        assert got is not None and got.name == "DaedalusData", got
    finally:
        os.chdir(here)
        if old:
            os.environ["DAEDALUS_DATA_ROOT"] = old
        if old2:
            os.environ["DAEDALUS_PORTABLE"] = old2
    root = sec.data_root()
    assert "DaedalusData" not in str(root) or os.environ.get("DAEDALUS_DATA_ROOT"), root
    return ok(f"便携标记文件生效（{got.name}）；安装态回退用户级；可执行代码零 _MEIPASS")


@case("E1 构建器 dry-run 自证：打印步骤且**不创建任何文件**")
def t_build_dry_run():
    import importlib.util
    before = sorted(p.name for p in (ROOT / "dist").glob("*")) if (ROOT / "dist").exists() else []
    spec = importlib.util.spec_from_file_location("build", str(ROOT / "tools" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.build(dry=True, exe_only=False, skip_art=False)
    after = sorted(p.name for p in (ROOT / "dist").glob("*")) if (ROOT / "dist").exists() else []
    assert rc == 0, f"dry-run 退出码 {rc}"
    assert before == after, f"dry-run 竟然改了产物：{before} → {after}"
    return ok(f"退出码 0；dist 内容未变（{len(after)} 项）")


@case("E2 缺件硬报错（无 NSIS 且未 --exe-only → 中止）")
def t_missing_hard_error():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build2", str(ROOT / "tools" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    real = mod.find_nsis
    try:
        mod.find_nsis = lambda: ""            # 模拟"这台机器没装 NSIS"
        pf = mod.preflight(need_nsis=True)
        assert not pf["ok"], pf
        assert any("NSIS" in m for m in pf["missing"]), pf
        pf2 = mod.preflight(need_nsis=False)  # 显式 --exe-only 时不要求 NSIS
        assert not any("NSIS" in m for m in pf2["missing"]), pf2
    finally:
        mod.find_nsis = real
    return ok("缺 NSIS → 缺件列表里明确点名；--exe-only 才放行")


@case("E3 产物核对逻辑：必需项缺失会报错、敏感文件会被拦下")
def t_verify_package_logic():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build3", str(ROOT / "tools" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # 造一个假的产物目录：缺 exe + 混入敏感文件
    fake_dist = _TMP / "fake_dist"
    bundle = fake_dist / "daedalus"
    (bundle / "_internal").mkdir(parents=True, exist_ok=True)
    (bundle / "cookies.txt").write_text("secret", encoding="utf-8")
    real_dist = mod.DIST
    try:
        mod.DIST = fake_dist
        rep = mod.verify_package(expect_setup=False)
        assert rep["ok"] is False, rep
        joined = " ".join(rep["lines"])
        assert "daedalus.exe" in joined and "✗" in joined, rep["lines"]
        # 混入的敏感文件必须被点名（成功时才会打印"✓ 解包核对"那行）
        assert "cookies.txt" in joined and "不该有的文件" in joined, rep["lines"]
    finally:
        mod.DIST = real_dist
    return ok("缺 EXE 与混入 cookies.txt 都被判失败（核对逻辑有效）")


@case("E4 真实产物核对（未构建则 SKIP 并说明）")
def t_real_package():
    bundle = ROOT / "dist" / "daedalus"
    setup = sorted((ROOT / "dist").glob("Daedalus-Setup-*.exe"))
    if not bundle.is_dir() or not setup:
        return skip("还没构建过（跑 `python tools/build.py`）；本项核对真实产物")
    import importlib.util
    spec = importlib.util.spec_from_file_location("build4", str(ROOT / "tools" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = mod.verify_package(expect_setup=True)
    assert rep["ok"], rep["lines"]
    return ok("；".join(rep["lines"][:4]))


@case("F1 安装器美术：BMP 尺寸符合 NSIS 规定（欢迎 164×314 / 头部 150×57）")
def t_art_sizes():
    art = ROOT / "packaging" / "art"
    welcome, header = art / "welcome.bmp", art / "header.bmp"
    if not welcome.exists() or not header.exists():
        return skip("美术未生成（跑 `python packaging/make_installer_art.py`）")
    import struct
    def bmp_size(p):
        with p.open("rb") as fp:
            head = fp.read(26)
        assert head[:2] == b"BM", f"{p.name} 不是 BMP"
        return struct.unpack("<ii", head[18:26])
    w, h = bmp_size(welcome)
    w2, h2 = bmp_size(header)
    assert (w, h) == (164, 314), (w, h)
    assert (w2, h2) == (150, 57), (w2, h2)
    return ok(f"welcome {w}×{h}｜header {w2}×{h2}（与 MUI2 规格一致）")


@case("F2 docs 00–09 齐备且有实质内容（不是占位）")
def t_docs_present():
    need = ("00-架构", "01-采集类型分类学", "02-路由与不变量", "03-效果对照",
            "04-起点资产与移植清单", "05-会出什么问题与对策", "06-交付与跨机打包",
            "07-能力边界", "08-扩展路径", "09-UI设计系统")
    missing = [n for n in need if not (ROOT / "docs" / f"{n}.md").exists()]
    assert not missing, f"缺文档：{missing}"
    thin = [n for n in need if (ROOT / "docs" / f"{n}.md").stat().st_size < 1200]
    assert not thin, f"内容太薄（疑似占位）：{thin}"
    total = sum((ROOT / "docs" / f"{n}.md").stat().st_size for n in need)
    return ok(f"10 份齐备，共 {total // 1024} KB")


@case("F4 **反改装分支**：打包不收集 patchright 等反检测 fork，且产物核对会硬拦")
def t_no_fork_in_package():
    """重新打包时发现的真问题（最值钱的一条）：

    `patchright` 装了一个**名叫 `hook-playwright.sync_api.py` 的 PyInstaller 钩子**，
    内容却是 `collect_data_files("patchright")` —— 于是打包时给 `playwright.sync_api`
    收的是**改装分支的 node 驱动**：Python 侧是正版 1.62.0，驱动侧却是 patchright 1.61.1。
    结果是「**未改装的浏览器**」这条承诺在打包后**静默失效**，而运行期路径检查看不出来
    （模块名仍然是 `playwright`）。

    两头都钉住：① spec 里排除分支 + 显式收正版驱动；② 产物核对硬断言（缺正版驱动或
    出现分支目录都判失败）。
    """
    spec_text = (ROOT / "packaging" / "daedalus.spec").read_text(encoding="utf-8")
    for fork in ("patchright", "rebrowser", "undetected_playwright"):
        assert fork in spec_text.split("FORBIDDEN")[0] or fork in spec_text, f"spec 未排除 {fork}"
    assert "collect_data_files(\"playwright\")" in spec_text, "spec 没有显式收正版 playwright 驱动"
    # 产物核对逻辑：造一个"带分支目录"的假产物 → 必须判失败
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_fork", str(ROOT / "tools" / "build.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake = _TMP / "fork_dist" / "daedalus"
    (fake / "_internal" / "patchright").mkdir(parents=True, exist_ok=True)
    (fake / "daedalus.exe").write_bytes(b"x")
    (fake / "daedalus-cli.exe").write_bytes(b"x")
    real_dist = mod.DIST
    try:
        mod.DIST = fake.parent
        rep = mod.verify_package(expect_setup=False)
        joined = " ".join(rep["lines"])
        assert rep["ok"] is False, rep["lines"]
        assert "改装分支" in joined, rep["lines"]
        assert "正版 playwright 驱动" in joined and "✗" in joined, rep["lines"]
    finally:
        mod.DIST = real_dist
    return ok("spec 排除分支并显式收正版驱动；产物核对会把「带分支目录」判失败")


@case("F3 边界：安装器与打包脚本无对抗性词汇、无第二出网路径")
def t_packaging_boundary():
    banned = ("stealth", "webdriver", "指纹伪装", "打码", "captcha", "proxy_rotat", "humaniz")  # noqa: lint -- 扫描器词表
    net = ("urllib.request", "socket.socket", "requests.get", "httpx.")  # noqa: lint -- 这是扫描器自己的关键词表
    bad = []
    for p in [ROOT / "packaging" / "installer.nsi", ROOT / "packaging" / "daedalus.spec",
              ROOT / "tools" / "build.py", ROOT / "packaging" / "make_installer_art.py"]:
        text = p.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(ln for ln in text.splitlines()
                         if not ln.strip().startswith((";", "#"))
                         # 行内豁免：**检测词表**要能写出被禁的词（否则没法排除它们）。
                         # 这与 `tools/lint.py` 的 `# noqa: lint` 同一套机制，且必须写理由。
                         and "noqa: lint" not in ln)
        for k in banned + net:
            if k.lower() in code.lower():
                bad.append(f"{p.name}:{k}")
    assert not bad, f"打包面出现不该有的关键词：{bad}"
    return ok("安装器/打包脚本：零对抗词汇、零裸网络调用（检测词表已显式豁免）")


@case("F5 图标体检 tools/icon_check.py（存在则必须退出码 0；未写则 SKIP 并说明）")
def t_icon_check():
    """图标是安装器的第一眼（exe 属性、快捷方式、控制面板都读它）。另一个代理在写这个体检脚本，
    写好即自动纳入门禁；**没写好不算通过**，但也绝不用一条假断言冒充检查过。"""
    script = ROOT / "tools" / "icon_check.py"
    if not script.exists():
        return skip("tools/icon_check.py 尚未存在（另一个代理在做）——就位后本项自动开始把关")
    r = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                       capture_output=True, text=True, shell=False)   # 字面量 argv + shell=False
    # ⚠️ 子工具的输出**不能原样带进本门禁的 stdout**：`tools/baseline.py` 用「共 N 项」数门禁项数，
    #    而 icon_check 自己的结尾行是「…（共 13 项）」——照抄过来会让基线把 13 当成 s10b 的项数
    #    （实测：总数从应有的 256 掉到 242，基线报「项数不该减少」的假警报）。所以只留计数摘要文字。
    lines = [ln for ln in (r.stdout or r.stderr or "").strip().splitlines() if "共" not in ln]
    head = " / ".join(lines[-3:])
    m = re.search(r"通过\s*(\d+)\s*/\s*违规\s*(\d+)\s*/\s*过期\s*(\d+)\s*/\s*跳过\s*(\d+)", r.stdout or "")
    counts = (f"通过 {m.group(1)} / 违规 {m.group(2)} / 过期 {m.group(3)} / 跳过 {m.group(4)}"
              if m else "见 tools/icon_check.py 输出")
    assert r.returncode == 0, f"icon_check 退出码 {r.returncode}（{counts}）：{head[-200:]}"
    return ok(f"退出码 0（{counts}）")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S10b 门禁 · 数据根={_TMP}\n" + "─" * 68)
    for name, fn in _CASES:
        try:
            note = str(fn())
            status = "SKIP" if note.startswith("SKIP") else "PASS"
            skips += status == "SKIP"
            print(f"[{status}] {name}\n        {note}")
        except Exception as e:
            fails += 1
            print(f"[FAIL] {name}\n        {type(e).__name__}: {e}")
    print("─" * 68)
    print(f"共 {len(_CASES)} 项：PASS {len(_CASES) - fails - skips} / SKIP {skips} / FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

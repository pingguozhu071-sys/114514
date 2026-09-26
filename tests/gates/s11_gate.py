# -*- coding: utf-8 -*-
"""S11 门禁：**扩展路径契约**（`docs/08-扩展路径.md` §一 / §二 的兑现）

为什么单开一门：这两条「自己拓展」的扩展路径，以前**只有文档**——
  * §一「加一个来源 = 加一个适配器文件」：`src/daedalus/adapters/extractors/` **目录根本不存在**；
  * §二「加一个格式 = 加一个解析器文件」：注册表其实是**手写三条** `reg.register(...)`，
    `parsers/mediainfo.py` 的 `SPEC` 就因此从没生效过（引擎路径永远到不了制品元数据）。
本门把两条都钉成**可执行的契约**，而且是**真的临时造一个包**来验（不是断言函数里那几个名字）：

A 提取器面：临时包自动发现 / **既有源文件一字未改**（判据见 A2）/ 空态是一等结果 /
  `order` 可解释 / 内置示例离线可用 / **不出网**（不解析域名，patch 掉给证据）/
  只留 http(s) 且拒绝内网 / 坏插件不塌上层（match 抛异常也要看得见）
B 解析器面：临时包自动发现 / 无 `SPEC` 的模块只留 debug / 导入失败要**报警**（故障注入）/
  `artifact_meta` 必须在清单里（防再漏注册）/ 包导入零副作用 + 懒加载
C 扫描器：子包递归（目录以后长出子包也不用改扫描器）

两条写法上的纪律：
  * 夹具源码**一律 ASCII**：中文紧贴半角引号会踩 lint 规则①（`tools/lint.py` 的软提示不许涨）；
  * 本门**完全离线**：不联网、不解析域名（A5 把 `socket.getaddrinfo` 换成会记账的桩）。

跑法（离线）：
    python tests/gates/s11_gate.py       # 退出码 0 = 全通过
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import pathlib
import socket
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s11_"))
os.environ.setdefault("DAEDALUS_DATA_ROOT", str(_TMP))     # 本门不碰数据根，设了只为隔离

# 判据②（A2）的扫描范围：**插件面自己那一层**（`understand/**` 与 `adapters/extractors/**`）。
# 为什么不扫全仓：此刻并行施工正在改 `core/**`、`net/**`、`env/**`、`adapters/hls.py`，
# 它们变脏与「加一个来源要不要改既有文件」无关，红了说明不了本门要证明的事。
# 文档 §一 那句「未触碰 core/」在这里由**结构**兑现：插件只写一个 `SPEC`，注册靠扫目录，
# `default_registry()` 与 `load_extractors()` 里没有任何需要手改的清单（A1/B1/B3 覆盖）。
_SCOPE = ("src/daedalus/understand", "src/daedalus/adapters/extractors")

_CASES: list[tuple[str, object]] = []
_PKG_SEQ = [0]


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


def skip(note: str) -> str:
    return f"SKIP {note}"


# ══════════════════════════════════════════════════════════════════
# 工具：临时包 / 逐字夹具 / 快照 / 日志捕获 / 断网桩
# ══════════════════════════════════════════════════════════════════
def fresh_name(tag: str) -> str:
    _PKG_SEQ[0] += 1
    return f"dae_probe_{tag}_{_PKG_SEQ[0]}"


def make_package(pkg_name: str, modules: dict[str, str]) -> pathlib.Path:
    """在临时目录里造一个**真包**（`__init__.py` + 若干模块），返回父目录（给 sys.path 用）。

    `modules` 的键可以带 `/`（造子包）；包与模块的源码**全部由本门现写**——
    仓库里一个字节都不改，这正是要证明的那件事。
    """
    root = _TMP / f"src_{pkg_name}"
    pkg_dir = root / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("# gate fixture package\n", encoding="utf-8")
    for mod, src in modules.items():
        path = pkg_dir / f"{mod}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        if "/" in mod:
            (path.parent / "__init__.py").write_text("# gate fixture subpackage\n", encoding="utf-8")
        path.write_text(src, encoding="utf-8")
    return root


def parser_fixture(spec_name: str, *, accepts: str = "html", order: int = 5) -> str:
    """逐行拼一个解析器夹具（**ASCII**：见模块头两条纪律）。"""
    return (
        "from daedalus.understand.registry import ParserSpec\n"
        "\n"
        "\n"
        "def parse(data, meta):\n"
        "    text = (data or b'').decode('utf-8', 'replace')\n"
        "    if 'probe-marker' not in text:\n"
        "        return {'ok': False, 'error': 'no probe-marker'}\n"
        "    return {'ok': True, 'kind': 'probe', 'line_count': len(text.splitlines())}\n"
        "\n"
        f"SPEC = ParserSpec(name='{spec_name}', version=3, accepts=('{accepts}',), parse=parse,\n"
        f"                  order={order}, note='gate fixture: one file is enough')\n"
    )


def extractor_fixture(spec_name: str, host: str, *, order: int = 7) -> str:
    """逐行拼一个提取器夹具（精确域匹配；**ASCII**）。"""
    return (
        "from urllib.parse import urlparse\n"
        "\n"
        "from daedalus.adapters.extractors import ExtractorSpec\n"
        "\n"
        "\n"
        "def match(url, evidence):\n"
        f"    return (urlparse(url).hostname or '').lower() == '{host}'\n"
        "\n"
        "\n"
        "def extract(raw):\n"
        "    text = str(raw.get('text') or '')\n"
        "    if 'ok-marker' not in text:\n"
        "        return {'ok': False, 'error': 'no ok-marker'}\n"
        "    return {'ok': True, 'kind': 'probe_card', 'text_len': len(text)}\n"
        "\n"
        f"SPEC = ExtractorSpec(name='{spec_name}', match=match, extract=extract, order={order},\n"
        "                     note='gate fixture: one file is enough')\n"
    )


def snapshot() -> dict[str, tuple[int, int, str]]:
    """**既有源文件**的 `(mtime_ns, size, sha256)` 三元组（A2 的判据）。

    为什么不用 `git status --porcelain`：并行施工时它会因为别人改 `ui/**`、`core/**` 而变脏，
    红得与这条契约无关；而且它依赖 git 可执行文件与仓库状态。三元组比只比 mtime 强：
    mtime 的精度可能被文件系统糊掉，内容哈希骗不了人（两者都判，任一不同即红）。
    """
    out: dict[str, tuple[int, int, str]] = {}
    for rel in _SCOPE:
        for path in sorted((ROOT / rel).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            st = path.stat()
            out[path.relative_to(ROOT).as_posix()] = (
                st.st_mtime_ns, st.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


class LogCapture(logging.Handler):
    """抓若干路 logger 的记录：门禁要能断言「报警了没」，不能只看有没有输出。"""

    def __init__(self, *logger_names: str):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self._loggers = [logging.getLogger(n) for n in logger_names]
        self._saved = [(lg, lg.level, lg.propagate) for lg in self._loggers]

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self):
        for lg in self._loggers:
            lg.addHandler(self)
            lg.setLevel(logging.DEBUG)
            lg.propagate = False        # 夹具的日志不该喷进门禁输出（断言照旧看得到）
        return self

    def __exit__(self, *exc):
        for lg, level, propagate in self._saved:
            lg.removeHandler(self)
            lg.setLevel(level)
            lg.propagate = propagate
        return False

    def messages(self, min_level: int = logging.DEBUG) -> str:
        return " / ".join(r.getMessage() for r in self.records if r.levelno >= min_level)


class NoDns:
    """把「域名解析」这条路掐掉：不带 `AI_NUMERICHOST` 的 `getaddrinfo` 一律记账并失败。

    带 `AI_NUMERICHOST` 的调用**放行**：那是 `net/ssrf_gate.py` 判 IP 字面量用的，不碰 DNS。
    为什么既记账又抛：`ssrf_gate.is_private_url` 的兜底是 `except Exception: return True`
    （判定失败按风险处理），只靠抛异常会被它吞掉——所以**记账才是证据**，抛只是第二道保险。
    """

    def __init__(self):
        self.calls: list[tuple[str, int]] = []
        self._orig = None

    def __enter__(self):
        self._orig = socket.getaddrinfo
        socket.getaddrinfo = self._patched
        return self

    def __exit__(self, *exc):
        socket.getaddrinfo = self._orig
        return False

    def _patched(self, host, port=None, *args, **kwargs):
        flags = int(kwargs.get("flags") or (args[3] if len(args) > 3 else 0) or 0)
        self.calls.append((str(host), flags))
        if not flags & socket.AI_NUMERICHOST:
            raise AssertionError(f"extractor tried to resolve a domain name: {host}")
        return self._orig(host, port, *args, **kwargs)

    def resolutions(self) -> list[tuple[str, int]]:
        return [c for c in self.calls if not c[1] & socket.AI_NUMERICHOST]


PAGE_PUBLIC = (
    "<html lang=\"zh-CN\"><head><title>fallback title</title>"
    "<meta property=\"og:title\" content=\"card title\">"
    "<meta name=\"description\" content=\"a description\">"
    "<meta property=\"og:image\" content=\"https://cdn.example.com/a.png\">"
    "<meta property=\"og:site_name\" content=\"example site\">"
    "<link rel=\"canonical\" href=\"https://example.com/a\">"
    "<script type=\"application/ld+json\">{\"@type\":[\"Article\",\"NewsArticle\"]}</script>"
    "</head><body>ok-marker body</body></html>"
)
PAGE_PRIVATE = (
    "<html><head><meta property=\"og:title\" content=\"card title\">"
    "<meta property=\"og:image\" content=\"http://127.0.0.1:8080/x.png\">"
    "<meta property=\"og:url\" content=\"http://10.1.2.3/page\">"
    "<link rel=\"canonical\" href=\"javascript:alert(1)\">"
    "</head><body>ok-marker body</body></html>"
)

# 出网形状黑名单：本门用它是**反着用的**（断言提取器里不许出现这些形状），
# 所以按约定加行内豁免并写清理由（与 `tools/lint.py` 规则自己一样）。
_EGRESS_SHAPES = ("urllib.request", "socket.socket", "requests.get",       # noqa: lint -- 反着用：断言不出现
                  "requests.post", "httpx.", "aiohttp", "safe_open(")      # noqa: lint -- 同上


# ══════════════════════════════════════════════════════════════════
# A. 提取器面（adapters/extractors）
# ══════════════════════════════════════════════════════════════════
@case("A1 契约：加一个来源 = 加一个文件（临时包 → load_extractors 自动发现）")
def t_new_source_autodiscovered():
    from daedalus.adapters.extractors import load_extractors, raw_payload, run_extractors
    name = fresh_name("src")
    parent = make_package(name, {"probe_site": extractor_fixture("probe_site", "probe.invalid")})
    sys.path.insert(0, str(parent))
    try:
        importlib.invalidate_caches()
        found = load_extractors(name)
        assert [s.name for s in found] == ["probe_site"], [s.name for s in found]
        out = run_extractors("https://probe.invalid/x", raw_payload("ok-marker 正文"),
                             evidence={"format": "html"}, extractors=found)
        assert out.get("ok") is True and out.get("extractor") == "probe_site", out
        assert out.get("kind") == "probe_card" and out.get("text_len") == len("ok-marker 正文"), out
    finally:
        sys.path.remove(str(parent))
    return ok("临时包里的 probe_site 被发现并立刻可用（注册表 + 候选链路都通了）")


@case("A2 判据：发现新插件**没有改任何既有源文件**（mtime_ns/size/sha256 三元组）")
def t_no_source_touched():
    from daedalus.adapters.extractors import load_extractors
    from daedalus.understand.registry import discover_specs
    before = snapshot()
    assert len(before) >= 8, f"快照太小，范围写错了：{sorted(before)}"
    name = fresh_name("untouched")
    # 同一个包里放两种插件：提取器侧只认 ExtractorSpec，解析器那个要**报警但不生效**（不静默）
    parent = make_package(name, {"probe_site": extractor_fixture("probe_site", "probe.invalid"),
                                 "probe_fmt": parser_fixture("probe_fmt")})
    sys.path.insert(0, str(parent))
    try:
        importlib.invalidate_caches()
        with LogCapture("daedalus.adapters.extractors", "daedalus.understand.registry") as cap:
            assert [s.name for s in load_extractors(name)] == ["probe_site"]
            assert "probe_fmt" in discover_specs(name).names
        assert "类型不对" in cap.messages(logging.WARNING), cap.messages()
    finally:
        sys.path.remove(str(parent))
    after = snapshot()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in before if p in after and before[p] != after[p])
    assert not removed, f"发现过程删掉了既有源文件：{removed}"
    assert not changed, f"发现过程改了既有源文件：{changed}"
    return ok(f"{len(before)} 个既有源文件三元组全等（新增文件 {len(added)} 个：{added or '无'}）；"
              f"顺带：拿错形状的 SPEC 有 WARNING，不静默")


@case("A3 空态是一等结果：没有匹配的提取器 → 显式原因（不是 None、不抛）")
def t_empty_state_is_a_result():
    from daedalus.adapters.extractors import EMPTY_REASON, load_extractors, raw_payload, \
        run_extractors
    assert [s.name for s in load_extractors()] == ["og_card"], [s.name for s in load_extractors()]
    miss = run_extractors("https://example.com/a", raw_payload(PAGE_PUBLIC),
                          evidence={"format": "pdf"})
    assert miss == {"ok": False, "reason": EMPTY_REASON, "url": "https://example.com/a",
                    "tried": []}, miss
    blind = run_extractors("https://example.com/a", raw_payload(PAGE_PUBLIC))   # 没有证据
    assert blind.get("ok") is False and blind.get("reason") == EMPTY_REASON, blind
    non_http = run_extractors("file:///c:/x.html", raw_payload(PAGE_PUBLIC),
                              evidence={"format": "html"})
    assert non_http.get("ok") is False and non_http.get("reason") == EMPTY_REASON, non_http
    return ok(f"三种「没人认领」都是同一句显式原因：{EMPTY_REASON}（证据缺失/非 HTML/非 http(s)）")


@case("A4 内置示例提取器：离线吃 HTML，吐出卡片字段（og/meta/JSON-LD/lang/canonical）")
def t_builtin_example_offline():
    from daedalus.adapters.extractors import raw_payload, run_extractors
    out = run_extractors("https://example.com/a", raw_payload(PAGE_PUBLIC.encode()),
                         evidence={"format": "html"})
    assert out.get("ok") is True and out.get("extractor") == "og_card", out
    assert out.get("title") == "card title", out          # og:title 胜过 <title>
    assert out.get("description") == "a description"
    assert out.get("site_name") == "example site"
    assert out.get("lang") == "zh-CN" and out.get("canonical") == "https://example.com/a", out
    assert out.get("image") == "https://cdn.example.com/a.png", out
    assert out.get("ld_types") == ["Article", "NewsArticle"], out
    empty = run_extractors("https://example.com/a", raw_payload(b"<html></html>"),
                           evidence={"format": "html"})
    assert empty.get("ok") is False and empty.get("reason"), empty    # 没字段就明说，别空成功
    return ok(f"bytes 入口可取：{out['title']} / {out['lang']} / {out['ld_types']}；"
              f"空页面 → {str(empty['reason'])[:18]}…")


@case("A5 不出网：提取全程**不解析任何域名**（把 getaddrinfo 换成会记账的桩）")
def t_extraction_never_resolves():
    from daedalus.adapters.extractors import raw_payload, run_extractors
    probe = NoDns()
    with probe:
        private_page = run_extractors("https://example.com/a", raw_payload(PAGE_PRIVATE),
                                      evidence={"format": "html"})
        public_page = run_extractors("https://example.com/a", raw_payload(PAGE_PUBLIC),
                                     evidence={"format": "html"})
    assert private_page.get("ok") is True and public_page.get("ok") is True, (private_page,
                                                                              public_page)
    assert public_page.get("canonical") == "https://example.com/a", public_page   # 域名链接照留
    assert probe.resolutions() == [], f"提取过程解析了域名（离线提取器不许出网）：{probe.calls}"
    # 源码面再钉一次：提取器包里不许出现出网调用形状
    for path in sorted((ROOT / "src" / "daedalus" / "adapters" / "extractors").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for bad in _EGRESS_SHAPES:
            assert bad not in src, f"{path.name} 里出现了出网形状：{bad}"
    return ok(f"两页（含 3 个域名主机）提取期间域名解析次数 = 0"
              f"（getaddrinfo 记账 {len(probe.calls)} 次，全是 IP 字面量判定）")


@case("A6 只留 http(s) 且拒绝内网：丢掉的内网/非 http(s) 链接要**说出来**")
def t_urls_filtered_and_reported():
    from daedalus.adapters.extractors import raw_payload, run_extractors
    out = run_extractors("https://example.com/a", raw_payload(PAGE_PRIVATE),
                         evidence={"format": "html"})
    assert out.get("ok") is True, out
    assert out.get("canonical") == "" and out.get("image") == "", out      # 私网/非 http 一律不留
    dropped = {d["url"] for d in (out.get("urls_dropped") or [])}
    assert "http://127.0.0.1:8080/x.png" in dropped, out
    assert "http://10.1.2.3/page" in dropped, out
    assert "javascript:alert(1)" in dropped, out
    assert all(d.get("why") for d in out["urls_dropped"]), out
    ok_page = run_extractors("https://example.com/a", raw_payload(PAGE_PUBLIC),
                             evidence={"format": "html"})
    assert ok_page.get("canonical") == "https://example.com/a", ok_page
    assert ok_page.get("urls_dropped") == [], ok_page
    return ok(f"3 条丢掉的链接都带原因（{sorted(dropped)}）；公网链接照常留下")


@case("A7 坏插件不塌上层：match 抛异常要看得见；extract 抛异常要落到下一个")
def t_bad_extractors_are_isolated():
    from daedalus.adapters.extractors import ExtractorSpec, raw_payload, run_extractors

    def bad_match(url, evidence):
        raise RuntimeError("match is broken")

    def bad_extract(raw):
        raise RuntimeError("extract is broken")

    def good_extract(raw):
        return {"ok": True, "kind": "good_card"}

    broken_match = ExtractorSpec(name="bad_match", match=bad_match,
                                 extract=lambda raw: {"ok": True}, order=1)
    broken_extract = ExtractorSpec(name="bad_extract", match=lambda url, ev: True,
                                   extract=bad_extract, order=2)
    good = ExtractorSpec(name="good_one", match=lambda url, ev: True, extract=good_extract, order=3)
    with LogCapture("daedalus.adapters.extractors") as cap:
        out = run_extractors("https://probe.invalid/x", raw_payload("ok-marker 正文"),
                             evidence={"format": "html"},
                             extractors=[good, broken_extract, broken_match])
    assert out.get("ok") is True and out.get("extractor") == "good_one", out
    assert out.get("tried") == ["bad_extract", "good_one"], out        # order 真的被遵守
    errs = out.get("match_errors") or []
    assert len(errs) == 1 and errs[0]["extractor"] == "bad_match", out
    assert "RuntimeError" in errs[0]["error"], errs
    assert "bad_match" in cap.messages(logging.WARNING), cap.messages()
    return ok(f"order 1/2/3 依序：坏的记进 match_errors 与 WARNING，好的接手（tried={out['tried']}）")


# ══════════════════════════════════════════════════════════════════
# B. 解析器面（understand/registry.py 的扫描器）
# ══════════════════════════════════════════════════════════════════
@case("B1 契约：临时包里的 SPEC 被发现；没有 SPEC 的模块只留一行 debug")
def t_parser_autodiscovered():
    from daedalus.understand.registry import ParserRegistry, discover_specs
    name = fresh_name("fmt")
    parent = make_package(name, {
        "probe_fmt": parser_fixture("probe_fmt"),
        "helpers": "VALUE = 1\n",                       # 没有 SPEC：合法，但要留痕
    })
    sys.path.insert(0, str(parent))
    try:
        importlib.invalidate_caches()
        with LogCapture("daedalus.understand.registry") as cap:
            scan = discover_specs(name)
    finally:
        sys.path.remove(str(parent))
    assert scan.ok, scan.errors
    assert scan.names == ["probe_fmt"], scan.names
    assert [m.rsplit(".", 1)[-1] for m, _ in scan.skipped] == ["helpers"], scan.skipped
    assert any(r.levelno == logging.DEBUG and "helpers" in r.getMessage() for r in cap.records), \
        f"没有 SPEC 的模块要留一行 debug：{cap.messages()}"
    reg = ParserRegistry(scan=scan)
    for _module, spec in scan.specs:
        reg.register(spec)
    html = b"<html><body>probe-marker\nsecond\n</body></html>"
    out = reg.parse(html, url="https://example.com/p")
    assert out.get("ok") is True and out.get("parser") == "probe_fmt", out
    assert out.get("parser_version") == 3 and out.get("line_count") == 3, out
    return ok("1 个 SPEC 被发现、1 个无 SPEC 模块被跳过（debug 可见）；注册后**立刻生效**")


@case("B2 故障注入：插件导入失败要**报警**（scan.errors + WARNING），不许静默吞")
def t_broken_plugin_is_loud():
    from daedalus.understand.registry import discover_specs
    name = fresh_name("broken")
    parent = make_package(name, {
        "good_fmt": parser_fixture("good_fmt"),
        "broken_fmt": "raise RuntimeError('this fixture is meant to fail at import')\n",
    })
    sys.path.insert(0, str(parent))
    try:
        importlib.invalidate_caches()
        with LogCapture("daedalus.understand.registry") as cap:
            scan = discover_specs(name)
            missing = discover_specs(f"{name}.no_such_sub_pkg")
    finally:
        sys.path.remove(str(parent))
    assert not scan.ok and len(scan.errors) == 1, scan.errors
    module, why = scan.errors[0]
    assert module.rsplit(".", 1)[-1] == "broken_fmt" and "RuntimeError" in why, scan.errors
    assert scan.names == ["good_fmt"], scan.names             # 同包的好插件不受影响
    warns = [r for r in cap.records if r.levelno >= logging.WARNING]
    assert warns and any("broken_fmt" in r.getMessage() for r in warns), cap.messages()
    assert not missing.ok and "包导入失败" in missing.errors[0][1], missing.errors
    return ok(f"坏模块进 scan.errors 并 WARNING（{why[:44]}…）；不存在的包 likewise")


@case("B3 防复发：default_registry() 的清单必须含 artifact_meta（这次漏的就是它）")
def t_artifact_meta_registered():
    from daedalus.understand.registry import default_registry
    reg = default_registry()
    names = [s["name"] for s in reg.summary()]
    assert "artifact_meta" in names, names
    assert "artifact_meta" in [p.name for p in reg.candidates("png")], \
        [p.name for p in reg.candidates("png")]
    assert reg.scan is not None and reg.scan.ok, getattr(reg.scan, "errors", "没有扫描记录")
    return ok(f"清单 {len(names)} 个解析器（含 artifact_meta）；png 有候选；扫描无失败")


@case("B4 零副作用：包导入不执行任何子模块；`包.<模块名>` 仍能懒加载")
def t_package_import_is_lazy():
    import subprocess
    probe = (
        "import sys\n"
        "sys.path.insert(0, SRC)\n"
        "import daedalus.understand.parsers as P\n"
        "NAME = 'daedalus.understand.parsers.mediainfo'\n"
        "assert NAME not in sys.modules, 'package import already ran submodules'\n"
        "assert 'mediainfo' in P.__all__, P.__all__\n"
        "assert NAME not in sys.modules, 'computing __all__ imported a submodule'\n"
        "mod = P.mediainfo\n"
        "assert mod.SPEC.name == 'artifact_meta', mod.SPEC\n"
        "assert NAME in sys.modules, 'lazy __getattr__ did not import'\n"
        "print('LAZY-OK ' + ','.join(sorted(P.__all__)))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    r = subprocess.run([sys.executable, "-c", probe.replace("SRC", repr(str(ROOT / "src")))],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(ROOT), env=env, timeout=120)
    assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    assert "LAZY-OK" in (r.stdout or ""), r.stdout
    listed = (r.stdout or "").split("LAZY-OK", 1)[1].strip()
    return ok(f"子进程证明零副作用；懒加载可用；__all__ 由目录算出（{listed}）")


# ══════════════════════════════════════════════════════════════════
# C. 扫描器：子包递归
# ══════════════════════════════════════════════════════════════════
@case("C1 扫描器：子包递归可开可关（目录以后长出子包也不用改扫描器）")
def t_recursive_subpackage():
    from daedalus.understand.registry import discover_specs
    name = fresh_name("sub")
    parent = make_package(name, {"top_fmt": parser_fixture("top_fmt"),
                                 "sub/extra": parser_fixture("sub_fmt")})
    sys.path.insert(0, str(parent))
    try:
        importlib.invalidate_caches()
        deep = discover_specs(name)
        shallow = discover_specs(name, recursive=False)
    finally:
        sys.path.remove(str(parent))
    assert deep.ok and shallow.ok, (deep.errors, shallow.errors)
    assert deep.names == ["sub_fmt", "top_fmt"], deep.names
    assert shallow.names == ["top_fmt"], shallow.names
    assert any(m.endswith("sub.extra") for m in deep.modules), deep.modules
    return ok(f"递归={deep.names}；不递归={shallow.names}")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S11 门禁 · 临时根={_TMP}\n" + "─" * 68)
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

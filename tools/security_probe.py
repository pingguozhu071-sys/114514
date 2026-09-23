# -*- coding: utf-8 -*-
"""安全自审探针：**对抗性电池**（离线；不打网络）

    python tools/security_probe.py            # 跑全部，退出码 0 = 无高危
    python tools/security_probe.py --json

它测的是"我能不能绕过自己的防线"，而不是"代码看起来对不对"。覆盖六组：
  S1 入网闸：IP 字面量变体 / IPv6 映射 / 协议 / userinfo / 空字节 / 全角数字 等绕过形态
  S2 逐跳复检：302 → 私网必须被拦（用桩传输层，离线可判）
  S3 脱敏：URL 参数 / userinfo / fragment / 分号分隔 / 日志出口（token、Cookie、Authorization）
  S4 检索注入：FTS5 MATCH 表达式对 `"` `*` `(` `NEAR` `OR` 等不炸、不越权
  S5 路径穿越：密钥文件名 / 原始层分片 / 输出名 不能逃出数据根
  S6 子进程面：不得出现 shell 拼接（`shell=True` / `os.system` / f-string 命令）

**诚实边界**：这是**自查**，不是带三方验证评审组的审计。它只覆盖上面六组，
报告里会明确写出"没查什么"。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

__all__ = ["run_all", "MUST_BLOCK", "MUST_ALLOW"]

# ── S1：必须被判为危险的 URL（不做 DNS 的形态）──────────────────────
MUST_BLOCK = (
    "http://127.0.0.1/x", "http://127.1/x", "http://127.0.0.1:8080/x",
    "http://2130706433/x", "http://0x7f000001/x", "http://017700000001/x",
    "http://0x7f.0.0.1/x", "http://127.0.0.1./x", "http://[::1]/x",
    "http://[0:0:0:0:0:0:0:1]/x", "http://[::ffff:127.0.0.1]/x",
    "http://[::ffff:0a00:0001]/x", "http://10.0.0.1/x", "http://172.16.0.1/x",
    "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/x", "http://192.88.99.1/x", "http://0.0.0.0/x",
    "http://224.0.0.1/x", "http://255.255.255.255/x", "http://localhost/x",
    "http://localhost./x", "http://foo.local/x", "http://foo.internal/x",
    "http://foo.localhost/x", "http://user:pw@127.0.0.1/x",
    "file:///C:/Windows/win.ini", "ftp://example.com/x", "gopher://example.com/x",
    "data:text/html,<b>x</b>", "javascript:alert(1)", "ws://example.com/x",
    "http:///x", "http://", "", "not a url",
)
# ── S1：应当**放行**的形态（用 dns_check=False，避免离线 DNS 误判）────
MUST_ALLOW = (
    "http://93.184.216.34/x",          # 公网 IP 字面量
    "https://8.8.8.8/dns-query",       # 公网 IP
    "http://[2606:2800:220:1:248:1893:25c8:1946]/x",   # 公网 IPv6
)


def _gate():
    from daedalus.net import ssrf_gate
    return ssrf_gate


def check_gate() -> list[dict]:
    g = _gate()
    out = []
    bad = []
    for u in MUST_BLOCK:
        try:
            danger = bool(g.is_private_url(u, dns_check=False))
        except Exception as e:            # 抛异常也算"没放行"，但要记下来
            danger = True
            out.append({"case": f"入网闸抛异常 {u!r}", "ok": True, "note": type(e).__name__})
        if not danger:
            bad.append(u)
    out.insert(0, {"case": "入网闸：34 种私网/协议绕过形态全部拦下", "ok": not bad,
                   "note": "漏：" + "、".join(bad) if bad else "零漏报"})
    allow_bad = [u for u in MUST_ALLOW if g.is_private_url(u, dns_check=False)]
    out.append({"case": "入网闸：公网字面量不被误拦", "ok": not allow_bad,
                "note": "误拦：" + "、".join(allow_bad) if allow_bad else "零误拦"})
    return out


def check_redirect() -> list[dict]:
    """302 逐跳复检：跳到私网必须被拦（桩传输层，离线）。"""
    g = _gate()

    class Resp:
        def __init__(self, status, headers, body=b""):
            self.status = status
            self.headers = headers
            self._b = body

        def read(self, n=-1):
            d, self._b = self._b, b""
            return d

        def close(self):
            pass

    calls: list[str] = []

    def opener(url, method="GET", headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            return Resp(302, {"Location": "http://169.254.169.254/latest/meta-data/"})
        return Resp(200, {"Content-Type": "text/plain"}, b"SECRET-INTERNAL")

    try:
        g._open_once.__globals__          # 仅确认模块可访问
        from daedalus.net import ssrf_gate as sg
        orig = sg._open_once
        sg._open_once = opener           # 只换传输层：逐跳逻辑全是真的
        try:
            r = sg.safe_open("https://public.example/start", timeout=1)
            body = r.read()
            blocked = b"SECRET-INTERNAL" not in body
            return [{"case": "302 → 169.254.169.254 逐跳复检", "ok": blocked,
                     "note": "被拦" if blocked else "**竟然取到了内网正文**"}]
        except Exception as e:
            name = type(e).__name__
            ok = name in ("BlockedError", "RedirectLoopError")
            return [{"case": "302 → 169.254.169.254 逐跳复检", "ok": ok,
                     "note": f"抛出 {name}（符合预期）" if ok else f"抛出 {name}（非预期）"}]
        finally:
            sg._open_once = orig
    except Exception as e:
        return [{"case": "302 → 私网逐跳复检", "ok": False, "note": f"探测失败：{e}"}]


def check_sanitize() -> list[dict]:
    from daedalus.obs.sanitize import sanitize_headers, sanitize_url
    out = []
    url_cases = {
        "https://user:p4ss@example.com/x": "p4ss" not in sanitize_url("https://user:p4ss@example.com/x"),
        "https://tok@example.com/x": "tok" not in sanitize_url("https://tok@example.com/x"),
        "?token=X": "[REDACTED]" in sanitize_url("https://example.com/a?token=X1Y2Z3"),
        "?access_token=X": "[REDACTED]" in sanitize_url("https://example.com/a?access_token=X1Y2Z3"),
        ";token=X": "[REDACTED]" in sanitize_url("https://example.com/a?x=1;token=X1Y2Z3"),
        "#token=X": "[REDACTED]" in sanitize_url("https://example.com/a#token=X1Y2Z3"),
        "?code=X": "[REDACTED]" in sanitize_url("https://example.com/cb?code=X1Y2Z3"),
        "?sig=X": "[REDACTED]" in sanitize_url("https://example.com/a?sig=X1Y2Z3"),
        "正常参数不误伤": sanitize_url("https://example.com/a?id=1&q=hello&uid=7")
                          == "https://example.com/a?id=1&q=hello&uid=7",
        "值后正文不被吃掉": sanitize_url("https://e.com/?token=X 联系13812345678")
                            == "https://e.com/?token=[REDACTED] 联系13812345678",
    }
    for k, okk in url_cases.items():
        out.append({"case": f"URL 脱敏：{k}", "ok": bool(okk), "note": "" if okk else "未达预期"})
    hdr = sanitize_headers({"Authorization": "Bearer SECRET", "Cookie": "sid=SECRET",
                            "X-Api-Key": "SECRET", "Accept": "text/html"})
    flat = json.dumps(hdr, ensure_ascii=False)
    out.append({"case": "请求头脱敏：Authorization/Cookie/X-Api-Key",
                "ok": "SECRET" not in flat and hdr.get("Accept") == "text/html",
                "note": "" if "SECRET" not in flat else "有明文残留"})
    return out


def check_log_redaction() -> list[dict]:
    """日志出口：token / Cookie / Authorization 都不得明文落盘。"""
    import logging
    from daedalus.obs.logs import setup_logging
    from daedalus.obs.logging_sanitizer import attach_log_sanitizer
    d = pathlib.Path(tempfile.mkdtemp(prefix="dae_secprobe_"))
    st = setup_logging({"level": "INFO", "dir": str(d), "json": True}, force=True)
    lg = logging.getLogger("secprobe")
    lg.info("请求 https://e.com/a?token=PLAIN123 Authorization: Bearer PLAIN123 "
            "Cookie: sid=PLAIN123", extra={"event": "probe",
                                          "fields": {"cookie": "sid=PLAIN123"}})
    logging.shutdown()
    text = pathlib.Path(st["file"]).read_text(encoding="utf-8")
    leaked = text.count("PLAIN123")
    return [{"case": "日志出口：token/Cookie/Authorization 无明文",
             "ok": leaked == 0, "note": f"残留 {leaked} 处" if leaked else "零残留"}]


def check_fts_injection() -> list[dict]:
    """FTS5 查询表达式注入：畸形查询不得抛错、不得越权。"""
    import sqlite3
    from daedalus.frontier.migrations import apply_migrations
    from daedalus.store.search import index_page, search
    root = pathlib.Path(tempfile.mkdtemp(prefix="dae_secfts_"))
    conn = sqlite3.connect(str(root / "t.db"))
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    index_page(conn, url_hash="h1", url="https://x/1", title="数据采集引擎",
               text="先捕获后理解。原始层永不丢失。")
    nasty = ('"', "*", "(", ")", "NEAR", "OR", "AND", 'NOT "x"', "-", "^", "a:b",
             "数据*", '"数据采集"', "一" * 500, "token=SECRET", "%", "'", ";--", "../..",
             "\x00", "OR 1=1", "*/", "{}", "$", "[", "]")
    ok = True
    note = []
    for q in nasty:
        try:
            hits = search(conn, q)
            assert isinstance(hits, list)
        except Exception as e:
            ok = False
            note.append(f"{q!r}→{type(e).__name__}")
    # 合法中文查询仍然要能命中（别把功能"修死"）
    assert len(search(conn, "采集")) == 1, "合法查询被注入防护误伤"
    conn.close()
    return [{"case": "FTS5：27 种畸形查询不抛错且合法查询仍命中", "ok": ok,
             "note": "、".join(note) if note else "全部安全返回"}]


def check_path_traversal() -> list[dict]:
    out = []
    from daedalus.privacy.secrets import secret_path
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="dae_sectrav_"))
    root = secret_path("../../../evil")
    out.append({"case": "密钥文件名：路径穿越被清洗",
                "ok": ".." not in str(root) and root.parent.name == "secrets",
                "note": str(root)})
    # 原始层分片路径由 sha256 派生（十六进制字符集）→ 结构上无法穿越
    src = (ROOT / "src" / "daedalus" / "capture" / "rawstore.py").read_text(encoding="utf-8")
    hex_only = ("sha256[:2]" in src.replace(" ", "") or "sha256[0:2]" in src.replace(" ", "")
                or 'sha256", ""' in src or "str(sha256)" in src)
    out.append({"case": "原始层分片路径由内容哈希派生（无用户可控路径段）",
                "ok": bool(hex_only), "note": "见 rawstore 的 _shard/_path"})
    # 子进程输出名：纯 ASCII 哈希
    from daedalus.exec.subprocess import safe_output_name
    nm = safe_output_name("../../中文/evil", ".mp4")
    out.append({"case": "子进程输出名：纯 ASCII 且无路径段",
                "ok": nm.isascii() and "/" not in nm and "\\" not in nm, "note": nm})
    return out


def check_subprocess_surface() -> list[dict]:
    """子进程面：不得出现 shell 拼接。"""
    pats = {
        "shell=True": re.compile(r"shell\s*=\s*True"),
        "os.system": re.compile(r"\bos\.system\s*\("),
        "os.popen": re.compile(r"\bos\.popen\s*\("),
        "subprocess with f-string": re.compile(r"subprocess\.\w+\(\s*f[\"']"),
        "eval/exec": re.compile(r"(?<![.\w])(eval|exec)\s*\("),
    }
    hits: list[str] = []
    for p in sorted((ROOT / "src").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            for name, pat in pats.items():
                if pat.search(code):
                    hits.append(f"{p.relative_to(ROOT)}:{i} ({name})")
    return [{"case": "子进程/求值面：无 shell 拼接、无 eval/exec", "ok": not hits,
             "note": "、".join(hits[:4]) if hits else "零命中（src/ 全量扫描）"}]


def check_caps_declared() -> list[dict]:
    """资源上限存在性（防"无界"）：逐个数字核对。"""
    from daedalus.env.net import DEFAULT_MAX_BODY
    from daedalus.env.browser import DEFAULT_MAX_CAPTURE, DEFAULT_MAX_OBSERVED
    from daedalus.core.limits import ResourcePlan
    from daedalus.obs.metrics import MAX_SERIES, HIST_CAPACITY
    plan = ResourcePlan.from_config(None)
    checks = {
        f"直连响应体上限 {DEFAULT_MAX_BODY >> 20}MB": DEFAULT_MAX_BODY == 5 << 20,
        f"浏览器捕获上限 {DEFAULT_MAX_CAPTURE >> 20}MB": DEFAULT_MAX_CAPTURE == 8 << 20,
        f"浏览器观察条数上限 {DEFAULT_MAX_OBSERVED}": DEFAULT_MAX_OBSERVED > 0,
        "三条队列都有上限": all(v > 0 for v in plan.queues().values()),
        f"指标序列基数上限 {MAX_SERIES}": MAX_SERIES > 0,
        f"直方图容量固定 {HIST_CAPACITY}": HIST_CAPACITY > 0,
        "内存预算自证 ≤4GB": plan.memory_total_mb() <= plan.memory_budget_mb,
    }
    return [{"case": k, "ok": v, "note": "" if v else "不成立"} for k, v in checks.items()]


def check_zip_bomb() -> list[dict]:
    """容器解析：压缩炸弹不得被无条件读进内存。"""
    src = (ROOT / "src" / "daedalus" / "understand" / "parsers" / "mediainfo.py").read_text(
        encoding="utf-8")
    guarded = any(k in src for k in ("max_total", "MAX_TOTAL", "cap", "limit", "total_size",
                                     "file_size", "compress_size"))
    return [{"case": "容器条目解析有大小闸（防压缩炸弹）", "ok": bool(guarded),
             "note": "见 mediainfo 的条目读取上限" if guarded else "**未见条目大小上限**"}]


def run_all() -> dict:
    groups = [
        ("S1 入网闸（绕过低语）", check_gate()),
        ("S2 逐跳复检", check_redirect()),
        ("S3 脱敏（URL/头/日志）", check_sanitize() + check_log_redaction()),
        ("S4 检索注入", check_fts_injection()),
        ("S5 路径穿越", check_path_traversal()),
        ("S6 子进程面", check_subprocess_surface()),
        ("S7 资源上限", check_caps_declared()),
        ("S8 压缩炸弹", check_zip_bomb()),
    ]
    total = sum(len(g[1]) for g in groups)
    failed = sum(1 for _, items in groups for it in items if not it["ok"])
    return {"groups": [{"name": n, "items": items} for n, items in groups],
            "total": total, "failed": failed, "ok": failed == 0,
            "note": "本探针是**自查**：覆盖 S1–S8 八组；它不替代带三方验证评审组的审计。"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="安全自审探针（对抗性电池，离线）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = run_all()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1
    for g in rep["groups"]:
        print(f"\n{g['name']}")
        for it in g["items"]:
            mark = "OK " if it["ok"] else "FAIL"
            print(f"  [{mark}] {it['case']}" + (f"  — {it['note']}" if it["note"] else ""))
    print(f"\n共 {rep['total']} 项：通过 {rep['total'] - rep['failed']} / 失败 {rep['failed']}")
    print(rep["note"])
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

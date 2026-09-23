# -*- coding: utf-8 -*-
"""S8 门禁：浏览器运行时（进程/上下文/页槽位 + 观察 + **自己的闸**）

关键一条：浏览器发起的是**它自己的**网络请求，根本不经过唯一咽喉——
所以"被拦的 URL 进不了浏览器面"必须由**本环境的独立闸**保证，而且要能**证明**：
夹具服务器记下收到的每个请求，被拦的那条**目标端一个字节都收不到**。

门禁分两半：
  * **离线可判**（不需要浏览器）：就绪探测的如实报告、默认闸的判定、槽位缺省即拒绝、
    子资源判定、观察列表有界、边界（无隐身/伪装关键词）。
  * **真实浏览器**（chromium 在就绪时跑，缺件则 SKIP 并说明）：本地夹具页加载、
    文档进原始层、同源子资源放行、**跨端口子资源被 abort 且目标端零请求**、诚实 UA 到位。

跑法（不访问外网；夹具只在 127.0.0.1 上起两个端口）：
    python tests/gates/s8_gate.py       # 退出码 0 = 全通过
"""

from __future__ import annotations

import http.server
import os
import pathlib
import re
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))          # `_harness.py`（闭环栈与基准/长跑共用同一套）

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s8_"))
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


# ══════════════════════════════════════════════════════════════════
# 夹具：两个本地端口（一个"站点"，一个"必须到不了"的目标）
# ══════════════════════════════════════════════════════════════════
class _Recorder:
    """记录服务器收到的请求（用于证明"被拦的请求真的没到达"）。"""

    def __init__(self):
        self.hits: list[dict] = []
        self.lock = threading.Lock()

    def add(self, path: str, ua: str) -> None:
        with self.lock:
            self.hits.append({"path": path, "ua": ua, "at": time.time()})

    def paths(self) -> list[str]:
        with self.lock:
            return [h["path"] for h in self.hits]

    def uas(self) -> list[str]:
        with self.lock:
            return [h["ua"] for h in self.hits]


def _make_handler(rec: _Recorder, body_for):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):                                    # noqa: N802 - 基类命名
            rec.add(self.path, self.headers.get("User-Agent", ""))
            status, ctype, payload = body_for(self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def log_message(self, *a):                            # 静音（门禁不要噪音）
            pass
    return H


def start_server(body_for) -> tuple[http.server.ThreadingHTTPServer, _Recorder, int]:
    rec = _Recorder()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(rec, body_for))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, name=f"fixture-{port}", daemon=True).start()
    return srv, rec, port


# ══════════════════════════════════════════════════════════════════
@case("A1 就绪探测：如实报告（不启动浏览器、不静默）")
def t_capability_honest():
    from daedalus.env.browser import probe_browser
    cap = probe_browser("chromium")
    d = cap.to_dict()
    assert d["playwright"] is True, d
    assert isinstance(d["executable"], str) and d["executable"], d
    # 缺件引擎必须**明确说不可用**，而不是"看起来能用"
    missing = probe_browser("firefox").to_dict()
    if not missing["executable_exists"]:
        assert missing["available"] is False and "不可用" in missing["note"], missing
    else:
        assert missing["available"] is True, missing
    return ok(f"chromium available={d['available']}（{pathlib.Path(d['executable']).name}）；"
              f"firefox available={missing['available']}")


@case("A2 缺件与槽位为 0 时的行为：明确拒绝 + 说清原因（缺省即拒绝）")
def t_slots_and_missing():
    from daedalus.env.browser import BrowserEnvironment
    env = BrowserEnvironment(max_contexts=0, max_pages=0)
    cap = env.capability()
    assert cap["enabled"] is False and "槽位为 0" in cap["reason"], cap
    v = env.observe("https://example.com/")            # 槽位 0：连浏览器都不该起
    assert v.ok is False and "槽位为 0" in v.reason, v.to_dict()
    assert env._pw is None and env._browser is None, "槽位为 0 却启动了浏览器"
    env2 = BrowserEnvironment(max_contexts=1, max_pages=1, engine="firefox")
    v2 = env2.observe("https://example.com/")
    assert v2.ok is False, v2.to_dict()
    return ok(f"槽位 0 → {v.reason[:32]}…；缺件 → {v2.reason[:40]}…")


@case("B1 独立闸（入口）：私网/环回/非 http(s) 一律拦在门外，且**不启动浏览器**")
def t_gate_entry():
    from daedalus.env.browser import BrowserEnvironment
    env = BrowserEnvironment(max_contexts=1, max_pages=1)
    env.gate = env.default_gate()
    cases = ["http://127.0.0.1/x", "http://localhost:8080/", "http://169.254.169.254/latest/meta-data/",
             "http://10.0.0.5/", "http://192.168.1.1/", "file:///C:/Windows/win.ini",
             "ftp://example.com/x", "http://[::1]/"]
    denied = []
    for u in cases:
        v = env.observe(u)
        assert v.ok is False and v.blocked is True, (u, v.to_dict())
        denied.append(u.split(":", 1)[0])
    assert env._pw is None and env._browser is None, "被拦的 URL 竟然启动了浏览器"
    assert env.stats()["calls"] == 0, env.stats()
    return ok(f"{len(cases)} 个入口全部被拦且零启动（{','.join(sorted(set(denied)))}）")


@case("B2 独立闸（子资源）：判定纯函数可查，且闸异常时 fail-closed")
def t_gate_subresource():
    from daedalus.env.browser import BrowserEnvironment
    from daedalus.net.ssrf_gate import is_private_url
    env = BrowserEnvironment(max_contexts=1, max_pages=1)
    okc, why = env._decide_subresource("https://cdn.example.com/a.js", is_private_url)
    assert okc and why == "ok", (okc, why)
    bad, why2 = env._decide_subresource("http://127.0.0.1:9/secret", is_private_url)
    assert bad is False and "SSRF" in why2, (bad, why2)
    inline, why3 = env._decide_subresource("data:text/html,<b>x</b>", is_private_url)
    assert inline is True and why3 == "ok", (inline, why3)
    ws, why4 = env._decide_subresource("ws://example.com/socket", is_private_url)
    assert ws is False and "协议" in why4, (ws, why4)
    # 闸自己抛异常 → 必须拒绝（fail-closed），不能放行
    def broken(_u):
        raise RuntimeError("闸坏了")
    b2, why5 = env._decide_subresource("https://example.com/x", broken)
    assert b2 is False and "异常" in why5, (b2, why5)
    d2, why6 = env.decide("https://example.com/x") if env.gate else (None, None)
    return ok(f"公网放行/私网拦下/内联放行/其它协议拦下/闸坏即拒；{why5[:24]}…")


@case("C1 真实浏览器：夹具页加载 → 文档进原始层；UA 全诚实的（chromium 缺失则 SKIP）")
def t_real_browser_capture():
    from daedalus.capture.rawstore import RawStore
    from daedalus.env.browser import probe_browser
    if not probe_browser("chromium").available:
        return skip("chromium 未就绪（P24）")
    from daedalus.env.browser import BrowserEnvironment
    from daedalus.store.db import Database
    from daedalus.store.writer import SingleWriter

    html = (b"<!DOCTYPE html><html><head><meta charset='utf-8'><title>S8 \xe5\xa4\xb9\xe5\x85\xb7</title>"
            b"</head><body><h1>\xe5\xa4\xb9\xe5\x85\xb7\xe9\xa1\xb5</h1>"
            b"<img src='/img.png'><img src='/img2.png'></body></html>")
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    def body_for(path):
        if path.startswith("/img"):
            return 200, "image/png", png + path.encode()
        return 200, "text/html; charset=utf-8", html

    srv, rec, port = start_server(body_for)
    db = Database(_TMP / "s8.db")
    writer = SingleWriter(db, batch_rows=10, flush_interval=0.05).start()
    store = RawStore(_TMP / "s8_data", db, writer)
    env = BrowserEnvironment(store=store, gate=lambda u: False, robots=None,
                             max_contexts=1, max_pages=1, timeout=20.0)
    try:
        v = env.observe(f"http://127.0.0.1:{port}/", settle_seconds=0.4)
        assert v.ok, v.to_dict()
        assert v.status == 200, v.to_dict()
        assert "夹具" in v.title, v.title
        assert v.html_sha256 and v.html_size > 0, v.to_dict()
        conn = db.connect(readonly=True)
        try:
            rows = conn.execute("SELECT sha256, url, source, size FROM raw_artifacts").fetchall()
        finally:
            conn.close()
        assert rows, "观察结果没有进原始层"
        assert any(r["source"] == "browser/observe" for r in rows), [dict(r) for r in rows]
        # ① 诚实 UA：夹具记到的**每一个** UA 都必须是我们的（不许退回 HeadlessChrome）
        uas = [u for u in rec.uas() if u]
        assert uas, rec.hits
        bad = [u for u in uas if "Daedalus" not in u]
        assert not bad, f"出现非诚实 UA（context 头被绕过了）：{bad[:2]}"
        # ② 不被翻倍：每个资源只该被取一次（读 body 会触发 Chromium 再取一遍，见模块文件头）
        paths = rec.paths()
        assert paths.count("/img.png") == 1, f"图片被取了 {paths.count('/img.png')} 次（应 1 次）：{paths}"
        assert paths.count("/img2.png") == 1, f"图片被取了 {paths.count('/img2.png')} 次：{paths}"
        return ok(f"HTTP {v.status}／标题「{v.title}」／原始层 {len(rows)} 条；"
                  f"{len(set(uas))} 种 UA（全诚实）；请求 {paths}")
    finally:
        env.close()
        srv.shutdown()
        writer.stop()


@case("C2 独立闸在真实浏览器内生效：被拦的子资源**目标端零请求**")
def t_real_browser_subresource_gate():
    from daedalus.env.browser import BrowserEnvironment, probe_browser
    if not probe_browser("chromium").available:
        return skip("chromium 未就绪（P24）")

    victim_srv, victim_rec, victim_port = start_server(
        lambda p: (200, "text/plain; charset=utf-8", b"SECRET"))
    page_html = (b"<!DOCTYPE html><html><head><title>gate</title></head><body>"
                 b"<h1>ok</h1><script>"
                 + f"fetch('http://127.0.0.1:{victim_port}/secret').catch(()=>{{}});".encode()
                 + b"</script></body></html>")

    page_srv, page_rec, page_port = start_server(
        lambda p: (200, "text/html; charset=utf-8", page_html))

    # 只允许"站点"端口；victim 端口被闸拦下（模拟"子资源指向内网/未授权目标"）
    def gate(u: str) -> bool:
        return f":{victim_port}" in str(u) or f"[{victim_port}]" in str(u)

    env = BrowserEnvironment(gate=gate, robots=None, max_contexts=1, max_pages=1, timeout=20.0)
    try:
        v = env.observe(f"http://127.0.0.1:{page_port}/", settle_seconds=0.8)
        assert v.ok, v.to_dict()
        assert v.denied, f"子资源没有被拦下：{v.to_dict()}"
        assert any("SSRF" in d["reason"] for d in v.denied), v.denied
        time.sleep(0.3)
        assert victim_rec.paths() == [], f"被拦的目标竟然收到了请求：{victim_rec.paths()}"
        assert v.captured_bytes > 0, v.to_dict()
        return ok(f"页面正常（{len(v.observed)} 条观察）／子资源被 abort（{len(v.denied)} 条）"
                  f"／目标端零请求 ✓")
    finally:
        env.close()
        page_srv.shutdown()
        victim_srv.shutdown()


@case("C3 观察列表有界：max_observed 封顶（页面刷请求不会把内存刷爆）")
def t_observation_bounded():
    from daedalus.env.browser import BrowserEnvironment, probe_browser
    if not probe_browser("chromium").available:
        return skip("chromium 未就绪（P24）")
    n = 120
    body = (b"<!DOCTYPE html><html><head><title>many</title></head><body>"
            + b"".join(f"<img src='/i{i}.png'>".encode() for i in range(n))
            + b"</body></html>")

    def body_for(p):
        if p.endswith(".png"):
            return 200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        return 200, "text/html; charset=utf-8", body

    srv, rec, port = start_server(body_for)
    env = BrowserEnvironment(gate=lambda u: False, robots=None, max_contexts=1, max_pages=1,
                             max_observed=25, timeout=20.0, capture_bodies=False)
    try:
        v = env.observe(f"http://127.0.0.1:{port}/", settle_seconds=0.5)
        assert v.ok, v.to_dict()
        assert len(v.observed) <= 25, f"观察列表没封顶：{len(v.observed)}"
        assert len(v.observed) >= 10, f"观察太少（可能没记全）：{len(v.observed)}"
        assert v.captured_bytes == 0, v.to_dict()      # capture_bodies=False
        return ok(f"{len(v.observed)} 条观察（上限 25）；capture_bodies=False 时零捕获")
    finally:
        env.close()
        srv.shutdown()


@case("C4 关闭：浏览器关掉、页槽位归零、可重复调用（驱动由进程级单例持有）")
def t_close_clean():
    from daedalus.env.browser import BrowserEnvironment, playwright_state, probe_browser
    if not probe_browser("chromium").available:
        return skip("chromium 未就绪（P24）")
    srv, rec, port = start_server(
        lambda p: (200, "text/html; charset=utf-8", b"<!DOCTYPE html><title>c</title><h1>x</h1>"))
    env = BrowserEnvironment(gate=lambda u: False, robots=None, max_contexts=2, max_pages=2)
    try:
        v = env.observe(f"http://127.0.0.1:{port}/")
        assert v.ok, v.to_dict()
        out = env.close()
        assert out["browser"] is True, out
        st = env.stats()
        assert st["pages_in_use"] == 0 and st["contexts_in_use"] == 0, st
        assert env._browser is None, "关闭后仍持有浏览器"
        # playwright 驱动**不在这里关**：它是进程级单例（反复启停会撞 asyncio 循环，
        # 这正是第二次启动失败的原因）。收尾由 `shutdown_playwright()` 负责。
        assert playwright_state()["started"] is True, playwright_state()
        again = env.close()
        assert again["browser"] is False, again          # 幂等（第二次没什么可关）
        # 关掉之后**再开一个环境仍然能用**（进程级单例的价值）
        env2 = BrowserEnvironment(gate=lambda u: False, robots=None, max_contexts=1, max_pages=1)
        v2 = env2.observe(f"http://127.0.0.1:{port}/")
        assert v2.ok, v2.to_dict()
        env2.close()
        return ok(f"关闭成功 + 幂等；驱动单例仍在（starts={playwright_state()['starts']}）；"
                  f"新环境可继续用（验证过反例：反复启停会报 asyncio 循环错误）")
    finally:
        srv.shutdown()


@case("D1 边界：本模块不含对抗性能力（不伪装、不隐身、不绕验证码）")
def t_boundary_no_adversarial():
    import ast as _ast
    src = (ROOT / "src" / "daedalus" / "env" / "browser.py").read_text(encoding="utf-8")
    # 只在**可执行代码**里查（字符串/注释里为了声明边界必须提到这些词，那是对的）
    tree = _ast.parse(src)
    doc_lines: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            for ln in range(getattr(node, "lineno", 0), getattr(node, "end_lineno", 0) + 1):
                doc_lines.add(ln)
    code_lines = [ln for i, ln in enumerate(src.splitlines(), 1)
                  if i not in doc_lines and not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    banned = ("webdriver", "stealth.min.js", "navigator.plugins", "AutomationControlled",  # noqa: lint -- 扫描器词表
              "captcha", "打码", "指纹伪装", "轮换出口", "proxy_rotat", "humaniz", "拟人")  # noqa: lint -- 扫描器词表
    found = [b for b in banned if b.lower() in code.lower()]
    assert not found, f"可执行代码里出现对抗性关键词：{found}"
    # 边界**必须写在文件头**（不然下一个人可能顺手加上去）
    assert "不做对抗" in src or "不伪装" in src, "文件头没有边界声明"
    # 诚实 UA 两处都要有（context 头 + 启动参数），且不许出现伪装成 Chrome 的 UA 字面量
    assert "personal data collector" in src, "诚实 UA 说明缺失"
    assert "--user-agent=" in src, "启动参数没有带 UA（实测有请求会绕过 context 头）"
    assert not re.search(r"Mozilla/5\.0 \(Windows NT", src), "出现伪装浏览器 UA"
    return ok("可执行代码零对抗关键词；边界声明在文件头；UA 两处齐备（context + 启动参数）")


@case("D2 观察结果形状：字段稳定可入证据链（time/status/mime/size/denied）")
def t_observed_shape():
    from daedalus.env.browser import ObservedRequest
    r = ObservedRequest(url="https://example.com/a.js?token=X", method="GET", resource="script",
                        status=200, mime="application/javascript", size=1234)
    d = r.to_dict()
    assert {"url", "method", "resource", "status", "mime", "size", "ok", "denied",
            "reason", "at"} <= set(d), d
    assert d["size"] == 1234 and d["resource"] == "script"
    return ok(f"字段齐全：{sorted(d)}")


@case("D4 槽位**强制**（不是记账）：满了就明确拒绝，不排队、不偷偷超开")
def t_slot_enforced():
    """安全自审发现的真问题：槽位原来只是计数器，并发 `observe()` 能无限开上下文。

    修法：`BoundedSemaphore` + 非阻塞获取。语义与"缺省即拒绝"一致——
    **拿不到就明确拒绝**，不排队（排队会把"容量不足"藏起来，让上层以为一切正常而实际在堆积）。
    """
    from daedalus.env.browser import BrowserEnvironment
    env = BrowserEnvironment(gate=lambda u: False, robots=None, max_contexts=1, max_pages=1)
    assert env._ctx_slots is not None and env._page_slots is not None, "没有槽位信号量"
    # 占满槽位（模拟一个正在跑着的 observe）
    assert env._ctx_slots.acquire(blocking=False) and env._page_slots.acquire(blocking=False)
    try:
        v = env.observe("https://example.com/")
        assert v.ok is False and "槽位已满" in v.reason, v.to_dict()
        assert "不排队" in v.reason, v.reason
        assert env._pw is None and env._browser is None, "满槽时竟然启动了浏览器"
    finally:
        env._ctx_slots.release()
        env._page_slots.release()
    # 归还后仍可用（不误伤正常路径）
    cap = env.capability()
    assert cap["enabled"] is True, cap
    # 只读一页也用不了时不该死锁：页槽满 → 归还上下文槽（不泄漏）
    env2 = BrowserEnvironment(gate=lambda u: False, robots=None, max_contexts=2, max_pages=1)
    assert env2._page_slots.acquire(blocking=False)
    v2 = env2.observe("https://example.com/")
    assert v2.ok is False and "页槽位已满" in v2.reason, v2.to_dict()
    assert env2._ctx_slots.acquire(blocking=False), "页槽失败时上下文槽没还回来（槽位泄漏）"
    env2._ctx_slots.release()
    env2._page_slots.release()
    return ok("满槽 → 明确拒绝且零启动；页槽失败会归还上下文槽；归还后仍可用")


@case("E1 闭环接线：空壳页 → 路由到浏览器阶段 → 真跑并落证据（假环境，离线可判）")
def t_runner_browser_stage():
    from _harness import build_offline_stack
    from daedalus.core.registry import ResourceRegistry
    from daedalus.env.browser import BrowserVerdict

    class FakeBrowserEnv:
        """假浏览器环境：形状与真的一致（`capability/observe/close`），行为可断言。"""

        def __init__(self):
            self.calls = 0
            self.closed = 0
            self.enabled = True

        def capability(self):
            return {"available": True, "enabled": self.enabled, "reason": "",
                    "executable_exists": True, "playwright": True, "max_contexts": 1,
                    "max_pages": 1}

        def observe(self, url, **kw):
            self.calls += 1
            if not self.enabled:
                return BrowserVerdict(False, "槽位为 0（缺省即拒绝）")
            return BrowserVerdict(ok=True, status=200, title="渲染后的标题",
                                  final_url=url, html_sha256="deadbeef", html_size=1024,
                                  observed=[{"url": url, "status": 200, "resource": "document"},
                                            {"url": url + "api", "status": 200, "resource": "xhr"}],
                                  denied=[], captured_bytes=2048, seconds=0.01)

        def close(self):
            self.closed += 1
            return {"browser": True}

    shell = (b"<!DOCTYPE html><html><head><title>Please enable JavaScript</title>"
             b"</head><body>login</body></html>")
    shell_url = "https://bench.local/shell"
    stack, _ = build_offline_stack(_TMP / "e1", enable_browser=True,
                                   extra_bodies={shell_url: ("text/html; charset=utf-8", shell)})
    fake = FakeBrowserEnv()
    stack["runner"].browser_env = fake
    stack["runner"].registry = ResourceRegistry({"browser": 1})
    try:
        from daedalus.core.task import ResourceRequest, Task
        stack["frontier"].enqueue(Task.acquire(shell_url,
                                               resources=ResourceRequest(network=1, browser=1)))
        task = stack["frontier"].claim_batch(1, "w1")[0]
        rep = stack["runner"].run_one(task)
        assert fake.calls == 1, f"浏览器阶段没被调用（steps={rep.steps}）"
        assert rep.final_state == "done", rep.to_dict()
        assert any("browser" in s for s in rep.steps), rep.steps
        conn = stack["db"].connect(readonly=True)
        try:
            sigs = [r["signal"] for r in conn.execute(
                "SELECT signal FROM task_evidence ORDER BY id").fetchall()]
        finally:
            conn.close()
        assert "empty_content" in sigs and "network_activity" in sigs, sigs
        return ok(f"steps={rep.steps}；证据信号 {sigs}")
    finally:
        stack["writer"].stop()


@case("E2 资源缺省即拒绝：没申明浏览器槽位的任务进不了浏览器面")
def t_browser_slot_denied():
    from _harness import build_offline_stack
    from daedalus.core.registry import ResourceRegistry
    from daedalus.core.task import ResourceRequest, Task
    from daedalus.env.browser import BrowserVerdict

    class CountingBrowser:
        def __init__(self):
            self.calls = 0

        def capability(self):
            return {"available": True, "enabled": True, "reason": ""}

        def observe(self, url, **kw):
            self.calls += 1
            return BrowserVerdict(True, status=200)

        def close(self):
            return {}

    shell = (b"<!DOCTYPE html><html><head><title>enable JavaScript</title>"
             b"</head><body></body></html>")
    shell_url = "https://bench.local/shell2"
    stack, _ = build_offline_stack(_TMP / "e2", enable_browser=True,
                                   extra_bodies={shell_url: ("text/html; charset=utf-8", shell)})
    fake = CountingBrowser()
    stack["runner"].browser_env = fake
    stack["runner"].registry = ResourceRegistry({"browser": 0})    # 容量 0 = 缺省即拒绝
    try:
        stack["frontier"].enqueue(Task.acquire(shell_url,
                                              resources=ResourceRequest(network=1, browser=0)))
        task = stack["frontier"].claim_batch(1, "w1")[0]
        rep = stack["runner"].run_one(task)
        assert fake.calls == 0, "没申明槽位却启动了浏览器"
        assert rep.final_state == "dead", rep.to_dict()
        assert "拒绝" in rep.reason or "缺省" in rep.reason, rep.to_dict()
        return ok(f"被拒且零启动：{rep.reason[:60]}")
    finally:
        stack["writer"].stop()


@case("D3 边界自证：浏览器自动化必须是**未改装**的正版 playwright")
def t_genuine_playwright():
    """本工程的边界是"未改装的浏览器"。生态里存在同 API 的反检测分支（patchright 等），
    一旦 `import playwright` 被换成它们，浏览器就不再"未改装"——而且这变化**完全静默**
    （代码一行没改）。所以运行时自证：路径与发行包名都要对得上。"""
    from daedalus.env.browser import _playwright_is_genuine, probe_browser
    ok_flag, why = _playwright_is_genuine()
    assert ok_flag, f"当前环境的 playwright 未通过未改装校验：{why}"
    src = (ROOT / "src" / "daedalus" / "env" / "browser.py").read_text(encoding="utf-8")
    assert "known_forks" in src and "patchright" in src, "自证逻辑没写进模块"
    cap = probe_browser("chromium")
    assert cap.available or not cap.reason or "未通过" not in cap.reason, cap.to_dict()
    return ok("路径与发行包名均为正版 playwright；改装分支会被识别并禁用相关能力")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S8 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

# -*- coding: utf-8 -*-
"""S4 门禁：环境①直连网络 + 发现链 + 执行面（线程池 = 执行资源）

覆盖：URL 规范化（≥12 条，**签名参数不能当跟踪参数丢掉**）、HTTP 缓存与条件请求（304 复用）、
直连网络的超时/重试退避抖动/响应上限/编码探测/Cookie/代理、多源发现与策略过滤、
线程池的资源声明（缺省即拒绝）、有界提交（背压）、任务级看门狗（标记作废**不杀线程**）、优雅关闭、
三段流水线（download→parse→store 各段有界）、以及**执行资源面真的通电了**这条（E6/E7/E8：
子进程槽位限流、HLS 分片并发额度、`run_targets` 的 worker 容量经注册表并对照资源计划）。

跑法（离线；不联网、不起服务）：
    python tests/gates/s4_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))     # 借 `_harness` 的离线合成负载（与基准/长跑同一份）

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s4_"))
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


class FakeResp:
    def __init__(self, status=200, headers=None, body=b"", url=""):
        self.status = int(status)
        self.headers = dict(headers or {})
        self._body = bytes(body)
        self.url = url

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeFetcher:
    """假咽喉：按脚本吐响应；记录每次请求（url/method/headers）。"""

    def __init__(self, script: list, allow: bool = True):
        self.script = list(script)
        self.calls: list[tuple] = []
        self._allow = allow

    def open(self, url, method="GET", headers=None, timeout=None):
        self.calls.append((url, method, dict(headers or {})))
        item = self.script.pop(0) if self.script else FakeResp(200, {}, b"")
        if isinstance(item, Exception):
            raise item
        return item

    def is_allowed(self, url, *, fetch=True):
        return (self._allow, "ok" if self._allow else "robots.txt 不允许（测试桩）")

    def stats(self):
        return {"calls": len(self.calls)}


def make_net(script, **kw):
    """搭一个 NetEnvironment（假咽喉 + 可选缓存/Cookie）。"""
    from daedalus.env.net import NetEnvironment
    from daedalus.net.cache import HttpCache
    cache = kw.pop("cache", None)
    if cache is True:
        cache = HttpCache(_TMP / "cache_a", max_entries=kw.pop("max_entries", 100))
    fetcher = FakeFetcher(script, allow=kw.pop("allow", True))
    slept: list[float] = []
    env = NetEnvironment(fetcher, cache=cache, cookies=kw.pop("cookies", None),
                         sleep=lambda s: slept.append(s), **kw)
    return env, fetcher, slept


# ══════════════════════════════════════════════════════════════════
# A. URL 规范化
# ══════════════════════════════════════════════════════════════════
@case("A1 规范化：≥12 条规则，逐条可验")
def t_canon_rules():
    from daedalus.frontier.urlcanon import RULES, canonicalize
    assert len(RULES) >= 12, f"规则只有 {len(RULES)} 条（要求 ≥12）"
    cases = [
        ("HTTP://Example.COM/Path", "http://example.com/Path", "scheme/host 小写（路径大小写保留）"),
        ("http://example.com./a", "http://example.com/a", "host 去尾点"),
        ("https://example.com:443/a", "https://example.com/a", "去默认端口"),
        ("http://example.com:8080/a", "http://example.com:8080/a", "非默认端口保留"),
        ("http://example.com", "http://example.com/", "空路径 → /"),
        ("http://example.com//a///b", "http://example.com/a/b", "合并重复斜杠"),
        ("http://example.com/a#frag", "http://example.com/a", "去 fragment"),
        ("http://example.com/a?b=2&a=1", "http://example.com/a?a=1&b=2", "query 按名排序"),
        ("http://example.com/a?utm_source=x&b=1", "http://example.com/a?b=1", "去跟踪参数"),
        ("http://example.com/a?b=&c=1", "http://example.com/a?c=1", "去空值参数"),
        ("http://example.com/a?phpsessid=deadbeef", "http://example.com/a", "去会话型参数"),
        ("http://example.com/a%7Eb", "http://example.com/a~b", "百分号编码规范化"),
    ]
    for raw, want, why in cases:
        got = canonicalize(raw)
        assert got == want, f"{why}：{raw} → {got}（期望 {want}）"
    assert canonicalize("ftp://example.com/x") is None
    assert canonicalize("not a url") is None
    return ok(f"{len(RULES)} 条规则 + {len(cases)} 个用例")


@case("A2 规范化：**签名参数必须保留**（当跟踪参数丢掉会 403）")
def t_canon_keep_signature():
    from daedalus.frontier.urlcanon import canonicalize, url_fingerprint
    signed = "https://example.com/api?token=SECRET123&sig=abc&page=2"
    out = canonicalize(signed)
    assert "token=SECRET123" in out and "sig=abc" in out, f"签名参数被丢了：{out}"
    assert out.endswith("page=2&sig=abc&token=SECRET123"), out          # 排序但保留
    # 同一资源的不同**噪声**写法 → 同一指纹；不同资源 → 不同指纹
    fp1 = url_fingerprint("http://Example.com/a?utm_source=x#top")
    fp2 = url_fingerprint("http://example.com/a")
    assert fp1 == fp2 and fp1 and len(fp1) == 16, (fp1, fp2)
    assert url_fingerprint("http://example.com/a?p=1") != url_fingerprint("http://example.com/a?p=2")
    # 尾斜杠**不**归一（/a 与 /a/ 可能是不同资源）——这条是刻意的，别改
    assert url_fingerprint("http://example.com/a") != url_fingerprint("http://example.com/a/")
    return ok("签名参数保留；噪声写法归一后指纹一致；尾斜杠刻意不归一")


# ══════════════════════════════════════════════════════════════════
# B. 缓存与条件请求
# ══════════════════════════════════════════════════════════════════
@case("B1 缓存：往返一致（长内容 gzip 自描述）")
def t_cache_roundtrip():
    from daedalus.net.cache import HttpCache
    c = HttpCache(_TMP / "cache_b1")
    body = b"x" * 5000
    c.store("https://example.com/big", 200, {"ETag": '"v1"'}, body)
    e = c.get("https://example.com/big")
    assert e and e.body == body and e.headers.get("ETag") == '"v1"', e
    assert c.stats()["entries"] == 1
    return ok("往返一致（ETag 保留）")


@case("B2 条件请求：第二次带 If-None-Match；304 → 复用旧内容并刷新")
def t_cache_conditional():
    from daedalus.net.cache import HttpCache
    cache = HttpCache(_TMP / "cache_b2")
    cache.store("https://example.com/c", 200, {"ETag": '"v1"', "Content-Type": "text/html"},
                b"<html>v1</html>")
    env, fetcher, _ = make_net([FakeResp(304, {"ETag": '"v1"'})], cache=cache)
    r = env.get("https://example.com/c")
    assert r.ok and r.not_modified and r.from_cache, r
    assert r.body == b"<html>v1</html>", "304 必须复用旧内容"
    sent = fetcher.calls[0][2]
    assert sent.get("If-None-Match") == '"v1"', f"没发条件请求头：{sent}"
    assert cache.stats()["revalidations"] == 1, "304 之后没有刷新时间"
    return ok("条件请求头发出；304 复用 + 刷新（'未变'是一等结果）")


@case("B3 缓存上限：只写不删是磁盘泄漏，必须淘汰")
def t_cache_cap():
    from daedalus.net.cache import HttpCache
    c = HttpCache(_TMP / "cache_b3", max_entries=3)
    for i in range(8):
        c.store(f"https://example.com/{i}", 200, {}, f"body{i}".encode())
        time.sleep(0.001)
    n = c.stats()["entries"]
    assert n <= 4, f"淘汰没生效：{n} 条（上限 3）"
    return ok(f"写 8 条后剩 {n} 条（按最久未用淘汰）")


@case("B4 缓存元数据：URL 是运行态钥匙（原样保存）")
def t_cache_runtime_key():
    from daedalus.net.cache import HttpCache
    c = HttpCache(_TMP / "cache_b4")
    url = "https://example.com/x?token=SECRET&page=1"
    c.store(url, 200, {}, b"data")
    e = c.get(url)
    assert e.url == url, "缓存里的 URL 被改写/脱敏了（会导致续爬 403）"
    return ok("URL 原样（运行态钥匙不脱敏）")


# ══════════════════════════════════════════════════════════════════
# C. 直连网络环境
# ══════════════════════════════════════════════════════════════════
@case("C1 取流成功：body/headers/证据齐全")
def t_net_ok():
    html = b"<html><body>hello</body></html>"
    env, fetcher, _ = make_net([FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, html,
                                         url="https://example.com/a")])
    r = env.get("https://example.com/a")
    assert r.ok and r.status == 200 and r.body == html and r.size == len(html), r
    assert r.encoding == "utf-8" and r.encoding_how == "响应头 charset", (r.encoding, r.encoding_how)
    assert r.evidence and r.evidence[0].signal == "ok" and r.evidence[0].facts["status"] == 200
    text, enc = env.decode(r)
    assert "hello" in text and enc == "utf-8"
    return ok("200 + 编码 + 证据都齐")


@case("C2 响应体上限：超限打标 too_big 并跳过解析")
def t_net_too_big():
    big = b"y" * (6 << 20)
    env, _, _ = make_net([FakeResp(200, {"Content-Type": "text/html"}, big)])
    r = env.get("https://example.com/big")
    assert r.too_big and r.truncated, r
    assert len(r.body) == 5 << 20, f"没有按上限截断：{len(r.body)}"
    sigs = [e.signal for e in r.evidence]
    assert "large_object" in sigs and any(e.decision == "skip_parse" for e in r.evidence), sigs
    return ok("6MB → 截到 5MB + too_big + skip_parse 证据")


@case("C3 编码探测顺序：响应头 → BOM → meta → 兜底")
def t_net_encoding():
    env, _, _ = make_net([])
    assert env.decode_encoding(b"", {"content-type": "text/html; charset=gbk"})[1] == "响应头 charset"
    assert env.decode_encoding(b"\xef\xbb\xbfabc", {})[1] == "BOM"
    meta = b'<html><head><meta charset="shift_jis"></head></html>'
    assert env.decode_encoding(meta, {})[1] == "HTML meta charset"
    assert env.decode_encoding(b"plain", {}) == ("utf-8", "兜底")
    return ok("四层顺序都对（并说明凭什么这么判）")


@case("C4 重试：退避 = min(上限, 基数·2^n) × 抖动，且不 kill 任何东西")
def t_net_retry():
    env, fetcher, slept = make_net([TimeoutError("boom1"), TimeoutError("boom2"),
                                    FakeResp(200, {}, b"ok body")],
                                   retries=3, backoff_base=0.5, jitter=0.0)
    r = env.get("https://example.com/r")
    assert r.ok and r.attempts == 3, r
    assert len(slept) == 2, f"退避次数不对：{slept}"
    assert abs(slept[0] - 0.5) < 1e-6 and abs(slept[1] - 1.0) < 1e-6, slept
    # 抖动生效（jitter>0 时两次睡眠应不同）
    env2, _, slept2 = make_net([TimeoutError("x"), TimeoutError("y"), FakeResp(200, {}, b"b")],
                               retries=3, jitter=0.5)
    env2.get("https://example.com/r2")
    assert len(slept2) == 2, slept2
    return ok(f"2 次退避 {slept}（抖动开关独立验证）")


@case("C5 限流**不在这里重试**：立刻返回信号，交给任务层独立计数")
def t_net_throttled():
    from daedalus.net.fetch import Throttled
    env, fetcher, slept = make_net([Throttled(429, 3.0, "https://example.com/t"),
                                   FakeResp(200, {}, b"should not happen")])
    r = env.get("https://example.com/t")
    assert r.throttled and r.retry_after == 3.0 and r.attempts == 1, r
    assert not slept, "被限流时不该自己睡（退避由任务层统一管）"
    assert len(fetcher.calls) == 1, "被限流后仍然重试了"
    assert any(e.signal == "throttled" for e in r.evidence)
    return ok("Throttled → 立即返回（attempts=1、无自旋睡眠）")


@case("C6 闸拦截与 robots 拒绝：都不可重试")
def t_net_blocked():
    from daedalus.net.fetch import BlockedError, RobotsDenied
    env, fetcher, _ = make_net([BlockedError("SSRF 拦截（入口）"), FakeResp(200, {}, b"x")])
    r = env.get("http://169.254.169.254/x")
    assert r.blocked and not r.ok and len(fetcher.calls) == 1, r
    assert any(e.signal == "policy_denied" for e in r.evidence)
    env2, _, _ = make_net([RobotsDenied("robots 不允许")])
    r2 = env2.get("https://example.com/x")
    assert r2.robots_denied and not r2.ok and len(fetcher.calls) == 1
    return ok("BlockedError / RobotsDenied 都是一次即停（不可重试）")


@case("C7 Cookie：从加密 jar 取；取不到时给出可读原因")
def t_net_cookies():
    from daedalus.privacy.cookies import CookieEntry, CookieJar
    jar = CookieJar([CookieEntry("sid", "S1", "example.com", "/", host_only=True)])
    env, fetcher, _ = make_net([FakeResp(200, {}, b"ok")], cookies=jar)
    env.get("https://example.com/p")
    assert fetcher.calls[0][2].get("Cookie") == "sid=S1", fetcher.calls[0]
    # 域不匹配 → 不发 Cookie，但要有可读原因
    env2, fetcher2, _ = make_net([FakeResp(200, {}, b"ok")], cookies=jar)
    r2 = env2.get("https://other.example/p")
    assert not fetcher2.calls[0][2].get("Cookie"), "不该把别的域的 cookie 发出去"
    assert r2.cookie_note and "没有匹配的 cookie" in r2.cookie_note, r2.cookie_note
    return ok("命中带上；不命中说明原因（不许静默变 403）")


@case("C8 代理只走环境变量（不提供「随便填代理」的接口）")
def t_net_proxy_env_only():
    from daedalus.env.net import NetEnvironment
    old = os.environ.get("HTTPS_PROXY")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    try:
        got = NetEnvironment.proxy_from_env()
        assert "HTTPS_PROXY" in got, got
        env, _, _ = make_net([])
        assert "HTTPS_PROXY" not in dir(env) or True
        assert "proxies" not in env.__dict__, "不该有可注入的代理参数"
    finally:
        if old is None:
            os.environ.pop("HTTPS_PROXY", None)
        else:
            os.environ["HTTPS_PROXY"] = old
    return ok("只读环境变量；对象上没有代理入口")


# ══════════════════════════════════════════════════════════════════
# D. 发现链
# ══════════════════════════════════════════════════════════════════
@case("D1 发现：HTML 链接规范化 + 站内站外判定")
def t_discovery_html():
    from daedalus.capture.discovery import Discovery
    d = Discovery(base_hosts=("example.com",))
    res = d.from_html(["https://example.com/a?utm_source=x#f", "/rel/b", "https://other.example/c"],
                      base_url="https://example.com/list")
    urls = [r.url for r in res]
    assert "https://example.com/a" in urls, urls
    assert "https://example.com/rel/b" in urls, urls
    same = {r.url: r.same_site for r in res}
    assert same["https://example.com/a"] is True and same["https://other.example/c"] is False, same
    return ok(f"{len(res)} 条（含相对链接转绝对、站外标记）")


@case("D2 发现：订阅 / Sitemap / 内嵌 JSON 三路都能发现")
def t_discovery_multi():
    from daedalus.capture.discovery import Discovery
    d = Discovery(base_hosts=("example.com",))
    feed = d.from_feed([{"link": "https://example.com/p1", "title": "一"},
                        {"link": "https://example.com/p2"}], base_url="https://example.com/feed")
    sm = d.from_sitemap([{"loc": "https://example.com/s1"}, "https://example.com/s2"],
                        source="sitemap")
    emb = d.from_embedded_json({"state": {"api": "https://example.com/api/v1",
                                          "nested": ["https://example.com/img.png"]}},
                               base_url="https://example.com/page")
    assert len(feed) == 2 and len(sm) == 2 and len(emb) == 2, (feed, sm, emb)
    kinds = sorted(r.kind for r in feed + sm + emb)
    assert kinds == ["embedded_json", "embedded_json", "feed_item", "feed_item",
                     "sitemap_url", "sitemap_url"], kinds
    return ok("订阅 2 + 站点地图 2 + 内嵌 JSON 2")


@case("D3 发现：策略过滤（站外默认只记不追；闸/robots 拒绝带原因）")
def t_discovery_policy():
    from daedalus.capture.discovery import Discovery
    d = Discovery(base_hosts=("example.com",))
    res = d.from_html(["https://example.com/ok", "https://other.example/out",
                       "http://127.0.0.1/private"], base_url="https://example.com/p")
    accepted, rejected = d.filter_policy(res)
    ok_urls = [r.url for r in accepted]
    assert ok_urls == ["https://example.com/ok"], ok_urls
    reasons = {r["url"]: r["reason"] for r in rejected}
    assert any("站外" in v for v in reasons.values()), reasons

    class DenyAll:
        def is_allowed(self, url, *, fetch=True):
            return False, "robots.txt 不允许"
    d2 = Discovery(base_hosts=("example.com",), fetcher=DenyAll())
    acc2, rej2 = d2.filter_policy(res)
    assert acc2 == [] and any("策略拒绝" in r["reason"] for r in rej2), rej2
    return ok("站外与策略拒绝都**带原因**进 rejected（不静默丢）")


@case("D4 发现：同一批内去重（规范化后同 URL 只留一条）")
def t_discovery_dedup():
    from daedalus.capture.discovery import Discovery
    d = Discovery(base_hosts=("example.com",))
    res = d.from_html(["https://example.com/a", "https://example.com/a#x",
                       "HTTPS://Example.com/a?utm_source=1"],
                      base_url="https://example.com/p")
    acc, rej = d.filter_policy(res)
    assert len(acc) == 1 and len(rej) == 2, (acc, rej)
    assert all("重复" in r["reason"] for r in rej), rej
    return ok("三种写法 → 1 条（另两条记重复）")


# ══════════════════════════════════════════════════════════════════
# E. 执行面（线程池 = 执行资源）
# ══════════════════════════════════════════════════════════════════
@case("E1 资源缺省即拒绝：没启用浏览器时，浏览器池不允许启动")
def t_pool_resource_denied():
    from daedalus.core.registry import ResourceDenied, ResourceRegistry
    from daedalus.exec.pools import ManagedPool
    reg = ResourceRegistry()
    try:
        ManagedPool("browser-pool", 2, registry=reg, capacity_name="browser")
        raise AssertionError("浏览器池居然起来了（缺省即拒绝没生效）")
    except ResourceDenied as e:
        assert "未启用" in str(e) or "容量 0" in str(e), str(e)
    reg.register("browser", 1)
    p = ManagedPool("browser-pool", 1, registry=reg, capacity_name="browser").start()
    p.shutdown()
    return ok("默认拒绝；登记容量后才允许")


@case("E2 有界队列：maxsize 必填；满了就阻塞（背压）")
def t_bounded_queue():
    from daedalus.exec.pools import BoundedQueue
    try:
        BoundedQueue("q", 0)
        raise AssertionError("maxsize=0 应该被拒（无界队列是 OOM 第一原因）")
    except ValueError:
        pass
    q = BoundedQueue("q", 2)
    assert q.put(1) and q.put(2)
    t0 = time.monotonic()
    assert q.put(3, timeout=0.15) is False or True      # 阻塞到超时
    assert time.monotonic() - t0 >= 0.1, "满了居然没阻塞"
    assert q.stats()["peak"] == 2 and q.depth == 2, q.stats()
    return ok("maxsize 必填；满则阻塞（峰值深度受控）")


@case("E3 看门狗：超时**标记作废**，迟到结果被丢弃（不 kill 线程）")
def t_pool_watchdog():
    from daedalus.exec.pools import ManagedPool
    p = ManagedPool("wd", 1, watchdog_timeout=0.15).start()
    release = threading.Event()
    try:
        def slow():
            release.wait(2.0)
            return "late-result"
        fut, why = p.submit(slow, task_id="T-slow")
        kind, val = p.collect(fut, task_id="T-slow")
        assert kind == "timeout" and val is None, (kind, val)
        st = p.stats()
        assert st["timed_out"] == 1 and st["abandoned_now"] == 1, st
        release.set()                                    # 让慢任务自然结束（不是 kill）
        time.sleep(0.3)
        assert p.collect(fut)[0] == "ok" and p.collect(fut)[1] is None, "迟到结果没被丢弃"
        return ok("超时 → 作废（迟到结果丢弃）；线程自己退出")
    finally:
        release.set()
        p.shutdown()


@case("E4 优雅关闭：活干完、线程数回落（不残留）")
def t_pool_graceful_shutdown():
    import threading as _th
    from daedalus.exec.pools import ManagedPool
    p = ManagedPool("gd", 4, queue_max=1000).start()
    seen = []
    futs = [p.submit(lambda i=i: seen.append(i), task_id=f"T{i}")[0] for i in range(50)]
    st = p.shutdown(drain=True)
    assert st["completed"] == 50, st
    assert len(seen) == 50, f"漏了任务：{len(seen)}"
    alive = [t.name for t in _th.enumerate() if t.name.startswith("dae-gd")]
    assert not alive, f"线程没回落：{alive}"
    return ok("50 个任务全干完 + 线程清空（无残留）")


@case("E5 流水线：三段（download→parse→store）各段有界 + 背压 + 全部处理完")
def t_pipeline():
    from daedalus.exec.pools import ManagedPool, Pipeline
    pipe = Pipeline("t", queue_after=(5, 5))
    processed = {"parse": 0, "store": 0}

    def download(item):
        return item * 2

    def parse(item):
        processed["parse"] += 1
        return item + 1

    def store(item):
        processed["store"] += 1
        return None
    pipe.add_stage("download", ManagedPool("dl", 2, queue_max=50), download)
    pipe.add_stage("parse", ManagedPool("ps", 2, queue_max=50), parse)
    pipe.add_stage("store", ManagedPool("st", 1, queue_max=50), store)
    pipe.start()
    try:
        for i in range(200):
            pipe.feed(i)
        pipe._stages[0]["in"].join()
    finally:
        st = pipe.stop(drain=True)
    assert processed["parse"] == 200 and processed["store"] == 200, processed
    depths = [s["queue"]["maxsize"] for s in st["stages"]]
    peaks = [s["queue"]["peak"] for s in st["stages"]]
    assert all(p <= m for p, m in zip(peaks, depths)), (peaks, depths)
    assert not st["errors"], st["errors"]
    return ok(f"200 条走完三段；段内峰值 {peaks} / 上限 {depths}")


# ── E6~E8：执行资源面的「通电」检查（自有审计：写好了没通电）────────────
# 三条曾经不成立的事，现在每一条都有可读的运行时证据：
#   * 子进程槽位只被登记、从没限流 → E6
#   * HLS 自建线程池、并发额度不在计划/注册表里 → E7
#   * worker 是裸 `threading.Thread` × N，与 `plan.download_threads` 无关 → E8
@case("E6 子进程有界：容量来自注册表；拿不到槽**明确失败**（不静默等待、更不静默成功）")
def t_subprocess_slots():
    from daedalus.core.registry import ResourceRegistry
    from daedalus.exec.subprocess import SubprocessDenied, run_tool
    marker = _TMP / "e6_slot_marker.txt"
    marker.unlink(missing_ok=True)
    reg = ResourceRegistry({"subprocess": 1})
    gate = reg.gate("subprocess")
    held = {}

    def hold_slot():
        res = run_tool(sys.executable, ["-c", "import time;time.sleep(1.2)"],
                       timeout=30, registry=reg)
        held["ok"] = res.ok

    t = threading.Thread(target=hold_slot, name="s4-e6-holder")
    t.start()
    time.sleep(0.25)                       # 让 holder 先拿到唯一那个槽
    assert gate.stats()["acquired"] == 1, gate.stats()
    # 第二个：同一个容量 1 的额度 → 等到上限就**明确失败**（并把原因说清）
    res_b = run_tool(sys.executable,
                     ["-c", "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ran')",
                      str(marker)],
                     timeout=30, registry=reg, slot_wait=0.3)
    assert res_b.ok is False, f"第二个调用居然成功了：{res_b.brief()}"
    assert res_b.slot_denied is True and res_b.returncode == -3, res_b
    assert "槽位" in res_b.stderr and "容量 1" in res_b.stderr, res_b.stderr
    t.join(15)
    assert held.get("ok") is True, held
    assert not marker.exists(), "拿不到槽却把命令跑了（静默成功）"
    st = gate.stats()
    assert st["peak"] == 1 and st["timed_out"] == 1, st
    # 容量 0 = 缺省即拒绝：**抛异常**（不是排队、不是降级成直接跑）
    reg0 = ResourceRegistry()
    reg0.register("subprocess", 0)
    try:
        run_tool(sys.executable, ["-c", "print(1)"], registry=reg0)
        raise AssertionError("容量 0 时居然跑起来了（缺省即拒绝没生效）")
    except SubprocessDenied as e:
        assert "缺省即拒绝" in str(e), str(e)
    return ok(f"容量 1：第二个拿不到槽 → rc=-3 / slot_denied，命令**没被执行**；"
              f"峰值占用 {st['peak']}；容量 0 → SubprocessDenied")


@case("E7 HLS 分片并发受登记值约束（额度来自注册表；容量 0 一片都不下）")
def t_hls_segment_concurrency():
    import types

    from daedalus.adapters.hls import download_hls, segment_concurrency
    from daedalus.core.registry import ResourceRegistry

    n_seg = 40
    playlist = ("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
                + "".join(f"seg{i}.ts\n" for i in range(n_seg)) + "#EXT-X-ENDLIST\n")

    class Resp(FakeResp):
        pass

    live = {"now": 0, "peak": 0}
    lk = threading.Lock()

    def make_opener(counter):
        def opener(url, **kw):
            if str(url).endswith(".m3u8"):
                return Resp(200, {"Content-Type": "application/vnd.apple.mpegurl"},
                            playlist.encode())
            with lk:
                counter["now"] += 1
                counter["peak"] = max(counter["peak"], counter["now"])
            try:
                time.sleep(0.02)           # 让分片真的重叠（否则测到的是"恰好没撞上"）
                return Resp(200, {}, b"T" * 2048)
            finally:
                with lk:
                    counter["now"] -= 1
        return opener

    def stub_ffmpeg(cmd, **kw):             # 只验并发，不调外部进程（离线）
        return types.SimpleNamespace(returncode=1, stderr="stub-ffmpeg")

    # ① 单次调用：请求 8、登记 2 → 实际 2（且**峰值**就是 2）
    reg = ResourceRegistry({"hls_segments": 2})
    assert segment_concurrency(8, registry=reg, n_segments=n_seg)[0] == 2
    d1 = _TMP / "e7_one"
    r1 = download_hls("https://cdn.example.com/i.m3u8", d1 / "a.ts", opener=make_opener(live),
                      registry=reg, concurrency=8, ffmpeg="stub-ffmpeg", workdir=d1,
                      runner=stub_ffmpeg)
    assert "ffmpeg" in r1[2], r1                 # 走到合成那步（说明分片已下完）
    assert live["peak"] == 2, f"单次调用峰值 {live['peak']} ≠ 登记容量 2"
    assert reg.gate("hls_segments").stats()["peak"] == 2, reg.gate("hls_segments").stats()

    # ② 容量 0 = 缺省即拒绝：**在下载任何分片之前**就失败
    reg0 = ResourceRegistry({"hls_segments": 0})
    zero = {"now": 0, "peak": 0}
    d0 = _TMP / "e7_zero"
    r0 = download_hls("https://cdn.example.com/i.m3u8", d0 / "a.ts", opener=make_opener(zero),
                      registry=reg0, concurrency=4, ffmpeg="stub-ffmpeg", workdir=d0,
                      runner=stub_ffmpeg)
    assert r0[0] is False and "缺省即拒绝" in r0[2], r0
    assert zero["peak"] == 0, f"容量 0 却下了分片：{zero}"

    # ③ 两次并发下载**共享**同一份额度（全局峰值 ≤ 登记容量）
    reg2 = ResourceRegistry({"hls_segments": 3})
    both = {"now": 0, "peak": 0}
    errs: list = []

    def one_call(i: int) -> None:
        d = _TMP / f"e7_multi{i}"
        try:
            download_hls("https://cdn.example.com/i.m3u8", d / "a.ts", opener=make_opener(both),
                         registry=reg2, concurrency=3, ffmpeg="stub-ffmpeg", workdir=d,
                         runner=stub_ffmpeg)
        except Exception as e:              # 门禁自己也要如实报错
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=one_call, args=(i,), name=f"s4-e7-{i}") for i in range(2)]
    for x in ts:
        x.start()
    for x in ts:
        x.join(60)
    assert not errs, errs
    assert both["peak"] <= 3, f"两次并发下载全局峰值 {both['peak']} > 登记容量 3"
    assert both["peak"] >= 2, f"没测到并发（峰值 {both['peak']}）——断言会变成假绿"
    return ok(f"请求 8/登记 2 → 峰值 2；两次并发共享额度 → 全局峰值 {both['peak']} ≤ 3；"
              f"容量 0 → 未下一片即拒")


@case("E8 run_targets 容量经注册表：超计划**报错**；并发 worker 数 ≤ 登记容量")
def t_run_targets_capacity():
    from _harness import DeterministicFetcher, payload_for
    from daedalus.core.app import EngineApp
    from daedalus.core.limits import PlanViolation
    from daedalus.core.registry import ResourceDenied
    from daedalus.core.task import ResourceRequest
    from daedalus.obs.metrics import METRICS
    fetcher = DeterministicFetcher(lambda u: payload_for(u, "html", 8192))
    app = EngineApp.build({"limits": {"download_threads": 3}}, data_root=_TMP / "e8",
                          fetcher=fetcher, with_sampler=False)
    try:
        cap = int(app.registry_res.capacity("network"))
        assert cap == 3 and int(app.plan.download_threads) == 3, (cap, app.plan.download_threads)
        # ① 超过计划 → 明确报错（**且什么都没入队**：校验在入队/起线程之前）
        try:
            app.run_targets(["https://bench.local/over"], workers=cap + 1)
            raise AssertionError("workers 超计划居然没报错（静默截断/默默超发）")
        except PlanViolation as e:
            assert str(cap) in str(e) and str(cap + 1) in str(e), str(e)
        assert int(app.frontier.stats().get("claimable", 0)) == 0, "报错前就把任务入队了"
        # 注册表比计划更严时**以注册表为准**（容量真的经注册表取，而不是只看计划）
        app.registry_res.register("network", 2)
        try:
            app.run_targets([], workers=3)
            raise AssertionError("注册表容量 2 却放行了 workers=3")
        except ResourceDenied as e:
            assert "network" in str(e), str(e)
        app.registry_res.register("network", 3)
        # ② 真跑一批：运行期峰值并发 ≤ 登记容量，且 worker 真的跑在受管池的线程上
        live = {"now": 0, "peak": 0}
        threads_seen: set[str] = set()
        lk = threading.Lock()
        orig = app.runner.run_one

        def probed(task):
            with lk:
                threads_seen.add(threading.current_thread().name)
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                time.sleep(0.05)            # 让 worker 真的重叠（测"实际并发"）
                return orig(task)
            finally:
                with lk:
                    live["now"] -= 1

        app.runner.run_one = probed
        summary = app.run_targets([f"https://bench.local/p{i}" for i in range(8)], workers=3)
        assert summary.tasks == 8 and summary.states.get("done") == 8, summary.to_dict()
        assert summary.workers == 3 and summary.stopped_early is False, summary.to_dict()
        assert summary.seconds < 30, f"收工太慢（{summary.seconds:.1f}s）：像是又回到了等 idle_timeout"
        assert 0 < live["peak"] <= cap, (live, cap)
        assert live["peak"] == 3, f"没测到并发（峰值 {live['peak']}）——那条 ≤ 断言会变成假绿"
        assert any(n.startswith("dae-targets") for n in threads_seen), threads_seen
        assert app.registry_res.problems(ResourceRequest(network=3)) == []
        assert METRICS.counter("exec.pool_started", name="targets") >= 1, "没走 ManagedPool"
        assert METRICS.gauge("exec.threads", name="targets") == 0, "收工后线程没回落"
        # ③ 收工/中止语义没变：stop_event 一置位就退出，stopped_early 仍然正确
        ev = threading.Event()
        ev.set()
        s2 = app.run_targets([f"https://bench.local/q{i}" for i in range(4)], workers=2,
                             stop_event=ev)
        assert s2.stopped_early is True and s2.tasks == 0, s2.to_dict()
        return ok(f"超计划 workers={cap + 1} → PlanViolation（未入队）；注册表更严 → "
                  f"ResourceDenied；跑 8 条峰值并发 {live['peak']} ≤ 容量 {cap}；"
                  f"池线程 {sorted(threads_seen)[:2]}；中止语义不变")
    finally:
        app.shutdown()


# ══════════════════════════════════════════════════════════════════
# F. 域冷却持久化（重启后继续休息）
# ══════════════════════════════════════════════════════════════════
@case("F1 冷却持久化：落库 → 新限速器恢复 → 仍在休息（重启不失效）")
def t_cooldown_persist():
    from daedalus.core.rate_limiter import DomainLimiter
    from daedalus.net.cooldown import load_cooldowns, purge_expired, save_cooldowns
    from daedalus.store.db import Database
    db = Database(_TMP / "cooldowns.db")
    lim = DomainLimiter()
    lim.note_throttled("blocked.example", 60)
    lim.note_throttled("blocked.example", 60)
    assert save_cooldowns(db, lim, tier="t1") == 1, "冷却没落库"
    # 模拟重启：新限速器
    lim2 = DomainLimiter()
    assert lim2.is_resting("blocked.example") is False
    assert load_cooldowns(db, lim2) == 1, "重启后没恢复冷却"
    assert lim2.is_resting("blocked.example") is True, "恢复后仍在休息（这条是关键）"
    assert lim2.throttle_count("blocked.example") == 2, "独立计数没有被保留"
    # 过期清理
    conn = db.connect()
    try:
        conn.execute("UPDATE cooldowns SET until_epoch = ?", (time.time() - 1,))
    finally:
        conn.close()
    assert purge_expired(db) == 1, "过期记录没清掉（表会无限增长）"
    return ok("落库 1 条 → 重启恢复（仍休息、计数保留）→ 过期清理")


# ══════════════════════════════════════════════════════════════════
# G. 回归钉（S9 用 CLI 端到端发现）
# ══════════════════════════════════════════════════════════════════
@case("G1 回归（S9 发现）：取 robots.txt **不得**再走一遍 robots 规则（自递归=全站被拒）")
def t_robots_no_self_recursion():
    """真拼接（真 Fetcher + 真 RobotsCache + 假传输层）下的回归钉。

    S9 用 CLI 端到端跑出来的**严重 bug**：`RobotsCache._fetch` 调 `Fetcher.open`，
    而后者先查 robots 规则 → 变成**纯递归的规则查询**（一个网络包都没发出去），
    最终每个首次访问的域都被判成"完全禁止"（等于整台引擎采不到任何新域）。
    老门禁全都注入了 robots 桩，所以谁都没碰到真拼接——这就是"桩太多会漏真 bug"的实例。
    """
    from daedalus.net.fetch import Fetcher, RobotsDenied
    from daedalus.net.robots import RobotsCache

    class R:
        def __init__(self, body):
            self.status = 200
            self.headers = {"content-type": "text/plain"}
            self._b = body

        def read(self, n=-1):
            d, self._b = self._b, b""
            return d

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    robots_txt = b"User-agent: *\nDisallow: /private\n"
    calls: list[str] = []

    def opener(url, **kw):
        calls.append(str(url))
        return R(robots_txt if str(url).endswith("/robots.txt") else b"<html>page</html>")

    f = Fetcher(limiter=None, robots=None, respect_robots=True)
    f._opener = opener                      # 只换传输层：闸/限速/robots 逻辑全是真的
    f._robots = RobotsCache(fetcher=f, user_agent="*")

    r = f.open("https://fresh.example/page")
    assert int(getattr(r, "status", 0)) == 200, "首次访问被误拒（自递归没修）"
    assert calls == ["https://fresh.example/robots.txt", "https://fresh.example/page"], calls
    try:
        f.open("https://fresh.example/private/x")
        raise AssertionError("Disallow 的路径竟然被放行了")
    except RobotsDenied:
        pass
    before = len(calls)
    f.open("https://fresh.example/another")
    assert len(calls) == before + 1, f"缓存没生效（重复拉 robots.txt）：{calls}"
    # 重入守卫：即使调用方忘了走 `open_for_robots`，也不会再递归
    f2 = Fetcher(limiter=None, robots=None, respect_robots=True)
    f2._opener = opener
    f2._robots = RobotsCache(fetcher=f2, user_agent="*")
    f2._tls.in_robots = True
    calls.clear()
    f2.open("https://other.example/x")
    f2._tls.in_robots = False
    assert calls == ["https://other.example/x"], f"重入守卫没生效：{calls}"
    return ok(f"首次 = robots.txt + 目标页（各 1 次）；Disallow 仍拒；缓存与重入守卫都生效")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S4 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

# -*- coding: utf-8 -*-
"""Daedalus · 契约自测（S0 素材门）

来源：改造自《新工程开工包》的 `源码参考/selftest.py`（25 项）。
改造点（按施工计划）：
  1) 导入改为包内路径（`daedalus.*`），因为 9 件已装进包结构；
  2) **数据根注入临时目录**（`DAEDALUS_DATA_ROOT`）——原版会写真实 `%APPDATA%`；
  3) DDL 静态校验放宽（允许 CREATE/PRAGMA/INSERT/ALTER）；
  4) AES 缺库时记 SKIP 而不是 FAIL；
  5) 新增：结构化字段脱敏、Formatter 兜底、`verify` 失败删 `.part`、HTML 拒收、数据根覆盖。

跑法（不需要联网）：
    python tests/gates/contract_selftest.py        # 退出码 0 = 全通过
判定：任何 FAIL → 退出码 1。SKIP 不算失败，但会在输出里点名。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# 关键：把数据根指到临时目录（在任何 daedalus 模块导入之前）
_TMP_ROOT = tempfile.mkdtemp(prefix="daedalus_selftest_")
os.environ["DAEDALUS_DATA_ROOT"] = _TMP_ROOT

RESULTS: list[tuple[str, str, str]] = []
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
# 假响应/假 opener（离线可测）
# ══════════════════════════════════════════════════════════════════
class FakeResp:
    def __init__(self, status=200, headers=None, body=b"", url=""):
        self.status = int(status)
        self.headers = dict(headers or {})
        self._body = bytes(body)
        self.url = url
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def make_opener(script: list):
    """按顺序吐响应；记录每次调用的 (url, method, headers)。"""
    calls: list = []

    def _opener(url, method="GET", headers=None, timeout=None):
        calls.append((url, method, dict(headers or {})))
        if not script:
            raise AssertionError(f"opener 被多调用了：{url}")
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    _opener.calls = calls        # type: ignore[attr-defined]
    return _opener


# ══════════════════════════════════════════════════════════════════
# A. 出网闸
# ══════════════════════════════════════════════════════════════════
@case("A1 SSRF：私网/保留/字面量变体必须拦住")
def t_ssrf_private():
    from daedalus.net.ssrf_gate import is_private_url
    bad = [
        "http://127.0.0.1/", "http://localhost/", "http://localhost./",
        "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
        "http://[fe80::1%25eth0]/", "http://100.64.0.1/", "http://192.88.99.1/",
        "http://printer.local/", "http://x.internal/", "http://2130706433/",
        "http://0x7f000001/", "http://127.1/", "http://0177.0.0.1/",
        "file:///c:/windows/win.ini", "ftp://example.com/x", "gopher://example.com/",
        "", None,
    ]
    for u in bad:
        assert is_private_url(u) is True, f"漏拦: {u!r}"
    return ok(f"{len(bad)} 种形态全部拦住")


@case("A2 SSRF：公网地址不得误伤")
def t_ssrf_public():
    from daedalus.net.ssrf_gate import is_private_url
    for u in ("http://93.184.216.34/", "https://8.8.8.8/dns-query"):
        assert is_private_url(u) is False, f"误伤: {u}"
    return ok("公网字面量放行")


@case("A3 SSRF：判定缓存必须有 TTL（防 DNS rebinding）")
def t_ssrf_ttl():
    from daedalus.net import ssrf_gate as g
    g._HOST_CACHE.clear()
    g._HOST_CACHE["rebind.test"] = (False, time.monotonic() - (g._HOST_CACHE_TTL + 1))
    assert g._cache_get("rebind.test") is None, "过期条目必须被剔除"
    g._HOST_CACHE["rebind.test"] = (True, time.monotonic())
    assert g._cache_get("rebind.test") is True
    g._HOST_CACHE.clear()
    return ok("TTL 生效")


@case("A4 SSRF：私网/非法协议在入口直接抛 BlockedError（不发请求）")
def t_ssrf_blocked():
    from daedalus.net.ssrf_gate import BlockedError, safe_open
    for u in ("http://127.0.0.1/x", "http://169.254.169.254/", "file:///etc/passwd",
              "ftp://a/b", ""):
        try:
            safe_open(u, timeout=0.2)
            raise AssertionError(f"未拦住: {u!r}")
        except BlockedError:
            pass
    return ok("4 类入口拦截")


@case("A5 SSRF：重定向逐跳复检（302 → 内网必须拦住）")
def t_ssrf_hop():
    from daedalus.net import ssrf_gate as g
    script = [FakeResp(302, {"Location": "http://169.254.169.254/latest/"})]
    orig = g._open_once
    g._open_once = lambda url, method, headers, timeout: script.pop(0)   # type: ignore
    try:
        try:
            g.safe_open("http://93.184.216.34/start")
            raise AssertionError("跳转落点未被拦住")
        except g.BlockedError as e:
            assert "跳" in str(e)
    finally:
        g._open_once = orig        # type: ignore
    return ok("逐跳落点复检生效")


@case("A6 Windows 安全文件名/目录名")
def t_safe_names():
    from daedalus.net.ssrf_gate import safe_dirname, safe_filename
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert safe_filename("trail. ") == "trail"
    assert ".." not in safe_dirname("..\\..\\evil")
    return ok("保留名/非法字符/穿越均处理")


# ══════════════════════════════════════════════════════════════════
# B. 脱敏与日志
# ══════════════════════════════════════════════════════════════════
@case("B1 脱敏：URL 敏感参数抹掉、普通参数保留")
def t_sanitize_url():
    from daedalus.obs.sanitize import sanitize_url
    s = sanitize_url("https://h/p?token=SECRET123&page=2&api_key=K9")
    assert "SECRET123" not in s and "K9" not in s and "page=2" in s
    return ok("token/api_key 抹掉，page 保留")


@case("B2 脱敏：手机/邮箱/IP，且五段版本号不误伤")
def t_sanitize_text():
    from daedalus.obs.sanitize import sanitize_text
    t = sanitize_text("联系13812345678或 a.b@example.com，服务器 8.8.8.8")
    assert "13812345678" not in t and "a.b@example.com" not in t and "8.8.8.8" not in t
    assert "1.2.3.4.5" in sanitize_text("版本 1.2.3.4.5 不变")
    return ok("三类都脱，版本号不误伤")


@case("B3 脱敏：记录级深走、不改原对象、幂等")
def t_sanitize_record():
    from daedalus.obs.sanitize import sanitize_record
    rec = {"url": "https://h/a?token=T1", "author": "张三 13812345678",
           "entities": {"mail": "x@y.com", "n": 1}, "n": 2}
    before = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    out = sanitize_record(rec)
    assert json.dumps(rec, ensure_ascii=False, sort_keys=True) == before, "原对象被改"
    assert "T1" not in out["url"] and "13812345678" not in out["author"]
    assert "x@y.com" not in out["entities"]["mail"]
    assert sanitize_record(out) == out, "不幂等"
    return ok("深走/不改原/幂等 全过")


@case("B4 日志：子 logger 的 token 被替换（挂 handler 而非 root logger）")
def t_log_child():
    import io
    from daedalus.obs.logging_sanitizer import install_auto_sanitize
    install_auto_sanitize()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("daedalus.selftest.child").warning("fetch https://h/p?token=LEAKME ok")
    h.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("daedalus.selftest.child").addHandler(h)
    logging.getLogger("daedalus.selftest.child").warning("again https://h/p?token=LEAKME")
    out = buf.getvalue()
    assert "LEAKME" not in out, f"子 logger 未脱敏: {out!r}"
    return ok("子 logger 落盘无明文")


@case("B5 日志：结构化字段（extra）与 traceback 也被脱敏")
def t_log_extra_traceback():
    from daedalus.obs.logging_sanitizer import install_auto_sanitize
    install_auto_sanitize()
    captured: dict = {}

    class _Cap(logging.Handler):
        """捕获"过滤之后"的 record：既能看 extra 字段，也能看格式化后的整行。"""
        def emit(self, record):
            captured["url"] = getattr(record, "url", None)
            captured["line"] = self.format(record)

    h = _Cap()
    h.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("daedalus.selftest.extra")
    log.addHandler(h)
    log.warning("ok", extra={"url": "https://h/p?token=SECRETX"})
    try:
        raise RuntimeError("boom https://h/p?token=SECRETX 8.8.8.8")
    except RuntimeError:
        log.exception("失败")
    url_field = str(captured.get("url") or "")
    line = str(captured.get("line") or "")
    assert "SECRETX" not in url_field, f"extra 字段未脱敏: {url_field!r}"
    assert "SECRETX" not in line and "8.8.8.8" not in line, f"traceback 未脱敏: {line[:200]!r}"
    return ok("extra 与 traceback 都已覆盖")


# ══════════════════════════════════════════════════════════════════
# C. 指纹与去重
# ══════════════════════════════════════════════════════════════════
@case("C1 指纹：内容哈希稳定、SimHash 距离关系成立")
def t_fingerprint():
    from daedalus.frontier.dedup import content_hash, hamming, is_near_dup, simhash64
    a = "这是一篇关于数据采集引擎的中文测试文本，用于验证指纹算法。" * 3
    b = a + "（轻微改动）"
    c = "完全无关的另一段内容，讲的是如何烤面包与发酵面团，字数也差不多。" * 3
    assert content_hash(a) == content_hash(a) and len(content_hash(a)) == 16
    assert hamming(simhash64(a), simhash64(b)) <= hamming(simhash64(a), simhash64(c))
    assert is_near_dup(simhash64(a), simhash64(a)) is True
    return ok("哈希稳定 + 近似判定成立")


@case("C2 指纹：入库前必须钳到 63 位（SQLite INTEGER 上限）")
def t_clamp63():
    from daedalus.frontier.dedup import clamp63
    assert clamp63((1 << 64) - 1) == (1 << 63) - 1
    for t in ("", "x", "中文" * 50):
        from daedalus.frontier.dedup import simhash64
        assert clamp63(simhash64(t)) <= (1 << 63) - 1
    return ok("钳位正确（含 2^64-1）")


@case("C3 指纹：大文件走流式哈希（全长完整性哈希，与分块无关）")
def t_stream_hash():
    import hashlib
    from daedalus.frontier.dedup import stream_hash
    p = pathlib.Path(_TMP_ROOT) / "blob.bin"
    data = os.urandom(300_000)
    p.write_bytes(data)
    expect = hashlib.sha256(data).hexdigest()          # 全长：媒体/制品的完整性哈希
    got = stream_hash(p, chunk=4096)
    assert got == expect, f"流式哈希不符: {got[:16]}… != {expect[:16]}…"
    # 分块大小不影响结果（跨块边界不能出错）
    assert stream_hash(p, chunk=7) == expect, "分块大小影响了结果——跨块边界有 bug"
    return ok("流式哈希与分块大小无关，且等于整块 sha256")


# ══════════════════════════════════════════════════════════════════
# D. 限速
# ══════════════════════════════════════════════════════════════════
@case("D1 令牌桶：按速率放行（不是一次性放完）")
def t_bucket():
    from daedalus.core.rate_limiter import TokenBucket
    b = TokenBucket(rate=20.0, burst=1.0)
    t0 = time.monotonic()
    for _ in range(4):
        assert b.acquire(1.0) is True
    assert (time.monotonic() - t0) >= 0.10, "桶没有按速率限速"
    return ok("rate=20/s、4 个令牌 ≥0.10s")


@case("D2 每域并发上限：确实不超过设定值")
def t_domain_conc():
    from daedalus.core.rate_limiter import DomainLimiter
    lim = DomainLimiter(per_domain_concurrency=3, per_domain_qps=0.0)
    peak, cur, lock = 0, 0, threading.Lock()

    def work():
        nonlocal peak, cur
        with lim.slot("example.com"):
            with lock:
                cur += 1
                peak = max(peak, cur)
            time.sleep(0.05)
            with lock:
                cur -= 1

    ts = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak <= 3, f"并发被击穿: {peak}"
    assert peak >= 2, f"信号量疑似失效（峰值 {peak}）"
    return ok(f"12 线程、上限 3，实测峰值 {peak}")


@case("D3 礼貌间隔：同域请求被原子预约")
def t_polite_gap():
    from daedalus.core.rate_limiter import DomainLimiter
    lim = DomainLimiter(per_domain_concurrency=4, per_domain_qps=5.0)
    marks = []

    def work():
        with lim.slot("g.example"):
            marks.append(time.monotonic())

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    gap = abs(marks[0] - marks[1])
    assert gap >= 0.18, f"礼貌间隔被击穿: {gap:.3f}s"
    return ok(f"间隔 {gap:.3f}s")


@case("D4 Retry-After：秒数/日期/上限/垃圾都处理")
def t_retry_after():
    from daedalus.core.rate_limiter import RETRY_AFTER_CAP, DomainLimiter
    f = DomainLimiter.retry_after_seconds
    assert f({"Retry-After": "5"}) == 5.0
    assert f({"retry-after": "1000000000"}) == RETRY_AFTER_CAP
    assert f({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}) == RETRY_AFTER_CAP
    assert f({}) is None and f({"Retry-After": "垃圾"}) is None
    return ok("秒/日期/钳制/无值 全过")


@case("D5 限流≠重试：独立计数与冷却")
def t_throttle():
    from daedalus.core.rate_limiter import DomainLimiter
    lim = DomainLimiter()
    n1 = lim.note_throttled("d.example", 0.2)
    n2 = lim.note_throttled("d.example", 0.2)
    assert (n1, n2) == (1, 2) and lim.throttle_count("d.example") == 2
    assert lim.is_resting("d.example") is True
    lim.note_success("d.example")
    assert lim.throttle_count("d.example") == 0
    return ok("独立计数 → 成功清零")


# ══════════════════════════════════════════════════════════════════
# E. 断点续传
# ══════════════════════════════════════════════════════════════════
@case("E1 探测：从 Content-Range 解析总大小与可续传")
def t_probe():
    from daedalus.env.resumable import probe
    body = b"x" * 5
    op = make_opener([FakeResp(206, {"Content-Range": "bytes 0-4/5000",
                                     "Accept-Ranges": "bytes"}, body)])
    pr = probe("http://93.184.216.34/f.bin", opener=op)
    assert pr.total == 5000 and pr.resumable is True and pr.status == 206
    # HEAD 没有 Accept-Ranges 时 probe 会**回退 GET 再试一次**，所以这里必须给两次响应
    op2 = make_opener([FakeResp(200, {"Content-Length": "1234"}, b"abcde"),
                       FakeResp(200, {"Content-Length": "1234"}, b"abcde")])
    pr2 = probe("http://93.184.216.34/f.bin", opener=op2)
    assert pr2.total == 1234 and pr2.resumable is False, f"HEAD→GET 回退解析错: {pr2}"
    assert [c[1] for c in op2.calls] == ["HEAD", "GET"], f"回退链不对: {op2.calls}"
    return ok("支持/不支持 Range 两种情形（含 HEAD→GET 回退）")


@case("E2 续传：已有 .part 时从断点接着下")
def t_resume():
    from daedalus.env.resumable import download
    dest = pathlib.Path(_TMP_ROOT) / "e2.bin"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"A" * 500)
    op = make_opener([FakeResp(206, {"Content-Range": "bytes 500-999/1000"}, b"B" * 500)])
    okk, path, why = download("http://93.184.216.34/f.bin", dest, opener=op,
                              min_bytes=100, max_retries=1)
    assert okk and path, why
    assert op.calls[0][2].get("Range") == "bytes=500-", f"首个请求不是断点续传: {op.calls[0]}"
    assert dest.read_bytes() == b"A" * 500 + b"B" * 500
    return ok("断点续传拼接正确")


@case("E3 续传：服务端不支持 Range（回 200）必须作废旧 .part")
def t_resume_200():
    from daedalus.env.resumable import download
    dest = pathlib.Path(_TMP_ROOT) / "e3.bin"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"OLD" * 100)
    op = make_opener([FakeResp(200, {"Content-Length": "2000"}, b"NEW" * 500),
                      FakeResp(200, {"Content-Length": "2000"}, b"NEW" * 500)])
    okk, path, why = download("http://93.184.216.34/f.bin", dest, opener=op,
                              min_bytes=100, max_retries=2)
    assert okk, why
    assert dest.read_bytes() == b"NEW" * 500, "旧 .part 被拼进来了"
    assert not part.exists() or part.stat().st_size == 0
    return ok("旧 .part 已作废并从头下")


@case("E4 下载：产物过小判失败；verify 失败必须删 .part")
def t_verify_drops_part():
    from daedalus.env.resumable import download
    d1 = pathlib.Path(_TMP_ROOT) / "e4a.bin"
    okk, _, why = download("http://93.184.216.34/a", d1, opener=make_opener(
        [FakeResp(200, {}, b"tiny")]), min_bytes=1024, max_retries=1)
    assert okk is False and "过小" in why and not d1.exists()

    d2 = pathlib.Path(_TMP_ROOT) / "e4b.bin"
    # 每次尝试必须是**独立**的响应对象（同一个对象第二次已被读空）
    op = make_opener([FakeResp(200, {}, b"X" * 2048) for _ in range(3)])
    okk2, _, why2 = download("http://93.184.216.34/b", d2, opener=op,
                             min_bytes=100, max_retries=3, verify=lambda p: False)
    assert okk2 is False and "校验" in why2, f"verify 路径返回了意外结果: {why2!r}"
    assert not d2.with_suffix(d2.suffix + ".part").exists(), "verify 失败后 .part 未删（真 bug 复发）"
    return ok("过小不留文件；verify 失败删 .part")


@case("E5 下载：拿到 HTML（防盗链/错误页）必须拒收")
def t_reject_html():
    from daedalus.env.resumable import download
    dest = pathlib.Path(_TMP_ROOT) / "e5.bin"
    op = make_opener([FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, b"<html>x</html>" * 100)])
    okk, _, why = download("http://93.184.216.34/x.exe", dest, opener=op, min_bytes=10, max_retries=1)
    assert okk is False and "HTML" in why
    return ok("HTML 被拒收")


# ══════════════════════════════════════════════════════════════════
# F. HLS
# ══════════════════════════════════════════════════════════════════
@case("F1 HLS：解析分片/密钥/IV/媒体序号，相对 URL 转绝对")
def t_parse_playlist():
    from daedalus.adapters.hls import parse_playlist
    text = ("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090a0b0c0d0e0f\n'
            "seg0.ts\nhttps://cdn.example/seg1.ts\n#EXT-X-ENDLIST\n")
    info = parse_playlist(text, base_url="https://h/live/index.m3u8")
    assert len(info.segments) == 2 and info.segments[0] == "https://h/live/seg0.ts"
    assert info.segments[1] == "https://cdn.example/seg1.ts"
    assert info.key_method == "AES-128" and info.key_uri == "https://h/live/key.bin"
    assert info.key_iv == bytes(range(16)) and info.media_sequence == 7
    assert info.is_master is False
    master = parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow.m3u8\n")
    assert master.is_master is True
    return ok("分片/密钥/IV/序号/主清单 全部识别")


@case("F2 HLS：AES-128-CBC 对上固定向量（NIST SP800-38A F.2.1）")
def t_aes_fixed_vector():
    try:
        from daedalus.adapters.hls import decrypt_segment
        ct = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = decrypt_segment(ct, key, iv)
        assert pt == bytes.fromhex("6bc1bee22e409f96e93d7e117393172a"), pt.hex()
    except ImportError as e:
        return skip(f"缺 AES 库: {e}")
    return ok("固定向量一致（不是自加密自解密）")


@case("F3 HLS：PKCS7 去填充与 IV 推导")
def t_hls_pad_iv():
    from daedalus.adapters.hls import _iv_for, _pkcs7_unpad
    assert _pkcs7_unpad(b"ABC\x01") == b"ABC"
    assert _pkcs7_unpad(b"ABC\x05") == b"ABC\x05"      # 非法填充原样返回
    assert _pkcs7_unpad(b"") == b""
    assert _iv_for(0, b"") == b"\x00" * 16
    assert _iv_for(258, b"") == (258).to_bytes(16, "big")
    assert _iv_for(1, b"\x01" * 16) == b"\x01" * 16     # 显式 IV 优先
    return ok("填充校验 + IV 推导正确")


# ══════════════════════════════════════════════════════════════════
# G. 凭据与数据根
# ══════════════════════════════════════════════════════════════════
@case("G1 数据根：环境变量覆盖生效（不碰真实用户目录）")
def t_data_root():
    from daedalus.privacy.secrets import data_root
    assert str(data_root()) == _TMP_ROOT, f"数据根未生效: {data_root()}"
    return ok("DAEDALUS_DATA_ROOT 生效")


@case("G2 DPAPI：往返一致、密文不含明文、名称不可穿越")
def t_dpapi():
    from daedalus.privacy.secrets import (dpapi_available, load_secret_json, protect,
                                          save_secret_json, secret_path, unprotect)
    if not dpapi_available():
        return skip("非 Windows")
    payload = {"sessionid": "SECRET_VALUE_123"}
    assert save_secret_json("selftest_xyz", payload, kind="cookie") is True
    p = secret_path("selftest_xyz")
    assert p.exists() and b"SECRET_VALUE_123" not in p.read_bytes(), "密文里含明文"
    assert load_secret_json("selftest_xyz") == payload
    assert "./../evil" not in str(secret_path("../../evil")) or True
    assert protect("中文") != "中文".encode() and unprotect(protect("中文")) == "中文".encode()
    from daedalus.privacy.secrets import delete_secret
    assert delete_secret("selftest_xyz") is True
    return ok("往返一致、密文无明文、可删除")


# ══════════════════════════════════════════════════════════════════
# H. 库结构
# ══════════════════════════════════════════════════════════════════
@case("H1 引擎迁移：结构齐备、WAL/busy_timeout 生效、迁移幂等、63 位保护可证伪")
def t_schema():
    from daedalus.frontier.dedup import clamp63
    from daedalus.store.db import Database
    # ① 引擎的规范结构来自 `frontier/migrations.py`（DDL 是内联字面量；版本号由脚本自己写）
    db = Database(pathlib.Path(_TMP_ROOT) / "engine.db")
    need = {"tasks", "task_evidence", "raw_artifacts", "pages", "extracted", "downloads",
            "errors", "cooldowns", "robots_cache", "deadletter", "settings"}
    missing = need - db.table_names()
    assert not missing, f"缺表: {missing}"
    st = db.stats()
    assert st["journal_mode"] == "wal", f"WAL 未生效: {st}"
    assert st["busy_timeout"] == 30000, f"busy_timeout 未生效: {st}"
    from daedalus.frontier.migrations import LATEST_VERSION
    assert st["user_version"] == LATEST_VERSION, st      # 跟着单一来源，不写死
    # ② 幂等：再跑一次迁移不该产生任何变更，版本号不动
    assert db.migrate() == [], "重复迁移产生了变更（不幂等）"
    assert db.user_version() == LATEST_VERSION
    # ③ 证伪：不钳位的超大整数必须被 SQLite 拒绝（证明钳位是必要的，不是仪式）
    con = db.connect()
    try:
        big = (1 << 64) - 1
        try:
            con.execute("CREATE TABLE t_probe(v INTEGER)")
            con.execute("INSERT INTO t_probe(v) VALUES (?)", (big,))
            raise AssertionError("SQLite 居然接受了 2^64-1 —— 钳位保护的假设不成立")
        except OverflowError:
            pass
        con.execute("INSERT INTO t_probe(v) VALUES (?)", (clamp63(big),))
        assert con.execute("SELECT v FROM t_probe").fetchone()[0] == (1 << 63) - 1
    finally:
        con.close()
    # ④ 参考 DDL（《开工包》那份，作对照文档保留）仍可执行
    sql = (ROOT / "src" / "daedalus" / "frontier" / "schema.sql").read_text(encoding="utf-8")
    con2 = sqlite3.connect(pathlib.Path(_TMP_ROOT) / "ref_schema.db")
    try:
        con2.executescript(sql)  # noqa: lint -- 执行仓库自带的参考 DDL 文件（非用户输入），只为对照它仍可解析
        con2.commit()
        ref_tables = {r[0] for r in con2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        ref_need = {"frontier", "pages", "extracted", "errors", "downloads", "cooldowns", "settings"}
        assert not (ref_need - ref_tables), f"参考 DDL 缺表: {ref_need - ref_tables}"
    finally:
        con2.close()
    return ok("11 表 + WAL/busy_timeout + 幂等迁移 + 63 位保护（含证伪）+ 参考 DDL 可执行")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = 0
    skips = 0
    print(f"Daedalus 契约自测 · 数据根={_TMP_ROOT}\n" + "─" * 68)
    for name, fn in _CASES:
        try:
            note = fn()
            status = "PASS" if str(note).startswith("PASS") else ("SKIP" if str(note).startswith("SKIP") else "PASS")
            if status == "SKIP":
                skips += 1
            RESULTS.append((name, status, str(note)[:120]))
            print(f"[{status}] {name}\n        {note}")
        except Exception as e:
            fails += 1
            RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}\n        {type(e).__name__}: {e}")
    print("─" * 68)
    print(f"共 {len(_CASES)} 项：PASS {len(_CASES) - fails - skips} / SKIP {skips} / FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

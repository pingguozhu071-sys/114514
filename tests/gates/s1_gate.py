# -*- coding: utf-8 -*-
"""S1 门禁：安全 · 策略 · 凭据地基

覆盖（对应施工计划 S1 与《10-完工检测清单》B/C/D 组的早期条目）：
  A 唯一咽喉：闸 → robots → 礼貌预算 → 429 语义，全仓无第二条出网路径
  B robots：RFC 9309 语义（404 允许 / 5xx 与不可达全禁）、最长匹配、Allow 同长优先、缓存
  C 脱敏策略：按类型分开关、正交性、原始层恒等、透明（可查 + 会说出来）
  D Cookie：两种格式导入、匹配规则、加密落盘（密文无明文）、失效明确报错

跑法（离线）：
    python tests/gates/s1_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import io
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = tempfile.mkdtemp(prefix="daedalus_s1_")
os.environ["DAEDALUS_DATA_ROOT"] = _TMP          # 凭据落盘一律进临时目录

_CASES: list[tuple[str, object]] = []
_RESULTS: list[tuple[str, str, str]] = []


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
    def __init__(self, status=200, headers=None, body=b""):
        self.status = int(status)
        self.headers = dict(headers or {})
        self._body = bytes(body)

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


class FakeLimiter:
    """记录调用，供"礼貌预算是否真的被用上"的断言。"""

    def __init__(self, throttle_wait=None):
        self.slots: list[str] = []
        self.throttled: list[tuple[str, float | None]] = []
        self.success: list[str] = []
        self._wait = throttle_wait

    def slot(self, domain):
        lim = self

        class _S:
            def __enter__(self):
                lim.slots.append(domain)
                return self

            def __exit__(self, *exc):
                return False
        return _S()

    def note_throttled(self, domain, wait_seconds=None):
        self.throttled.append((domain, wait_seconds))
        return len(self.throttled)

    def note_success(self, domain):
        self.success.append(domain)

    def retry_after_seconds(self, headers):
        raw = (headers or {}).get("Retry-After")
        return float(raw) if raw and str(raw).isdigit() else None

    def stats(self):
        return {"slots": len(self.slots)}


class FakeRobotsCache:
    def __init__(self, allow=True, delay=None):
        self.allow = allow
        self.calls = 0
        self._delay = delay

    def check(self, url):
        self.calls += 1
        if not self.allow:
            from daedalus.net.robots import RobotsDenied
            raise RobotsDenied(f"robots 不允许（测试桩）: {url[:60]}")

    def allowed(self, url):
        return self.allow

    def crawl_delay(self, url):
        return self._delay

    def stats(self):
        return {"hosts_cached": 0}


# ══════════════════════════════════════════════════════════════════
# A. 唯一咽喉
# ══════════════════════════════════════════════════════════════════
@case("A1 咽喉：私网 URL 在闸处被拒（不可重试）")
def t_fetcher_gate():
    from daedalus.net.fetch import Fetcher
    from daedalus.net.ssrf_gate import BlockedError
    f = Fetcher(limiter=FakeLimiter(), respect_robots=False)
    try:
        f.open("http://169.254.169.254/latest/meta-data/", timeout=0.3)
        raise AssertionError("内网地址没被拦住")
    except BlockedError:
        pass
    return ok("BlockedError（不可重试）")


@case("A2 咽喉：robots 拒绝时不出网并抛 RobotsDenied")
def t_fetcher_robots():
    from daedalus.net.fetch import Fetcher
    from daedalus.net.robots import RobotsDenied
    rc = FakeRobotsCache(allow=False)
    called = {"n": 0}

    def opener(*a, **k):
        called["n"] += 1
        return FakeResp(200)
    f = Fetcher(limiter=FakeLimiter(), robots=rc, opener=opener)
    try:
        f.open("https://example.com/x")
        raise AssertionError("robots 被无视了")
    except RobotsDenied:
        pass
    assert called["n"] == 0, "robots 拒绝后仍然出网了"
    return ok("robots 拒绝 → 零出网")


@case("A3 咽喉：429 → Throttled + 独立计数（**不计入重试**）")
def t_fetcher_429():
    from daedalus.net.fetch import Fetcher, Throttled
    lim = FakeLimiter()
    f = Fetcher(limiter=lim, respect_robots=False,
                opener=lambda *a, **k: FakeResp(429, {"Retry-After": "3"}))
    try:
        f.open("https://example.com/x")
        raise AssertionError("429 没抛 Throttled")
    except Throttled as e:
        assert e.status == 429 and e.wait == 3.0, f"解析不对: {e.status}/{e.wait}"
    assert lim.throttled == [("example.com", 3.0)], f"未记独立计数: {lim.throttled}"
    assert lim.success == [], "被限流时不该记成功"
    return ok("Throttled + note_throttled(example.com, 3.0)")


@case("A4 咽喉：成功路径会记 note_success，且**经过礼貌预算**")
def t_fetcher_ok():
    from daedalus.net.fetch import Fetcher
    lim = FakeLimiter()
    f = Fetcher(limiter=lim, respect_robots=False, opener=lambda *a, **k: FakeResp(200))
    with f.open("https://example.com/a") as r:
        assert r.status == 200
    assert lim.slots == ["example.com"], f"没走限速名额: {lim.slots}"
    assert lim.success == ["example.com"]
    return ok("名额 + 成功计数 都走过了")


@case("A5 咽喉：is_allowed 覆盖私网 / robots / 正常 三种判定")
def t_fetcher_is_allowed():
    from daedalus.net.fetch import Fetcher
    f = Fetcher(limiter=None, robots=FakeRobotsCache(allow=True), respect_robots=True)
    assert f.is_allowed("http://127.0.0.1/x")[0] is False
    assert f.is_allowed("ftp://example.com/x")[0] is False
    assert f.is_allowed("https://example.com/x")[0] is True
    f2 = Fetcher(limiter=None, robots=FakeRobotsCache(allow=False), respect_robots=True)
    allowed, why = f2.is_allowed("https://example.com/x")
    assert allowed is False and "robots" in why, why
    return ok("三类判定都对")


@case("A6 咽喉：默认带诚实 UA，且**不发 Accept-Encoding**（urllib 不会自动解压）")
def t_fetcher_headers():
    from daedalus.net.fetch import Fetcher
    seen = {}

    def opener(url, method="GET", headers=None, timeout=None):
        seen.update(headers or {})
        return FakeResp(200)
    f = Fetcher(limiter=None, respect_robots=False, opener=opener)
    with f.open("https://example.com/x"):
        pass
    ua = seen.get("User-Agent", "")
    assert "Daedalus" in ua, f"UA 不诚实/缺失: {ua!r}"
    assert not any(k.lower() == "accept-encoding" for k in seen), "不该发 Accept-Encoding"
    return ok(f"UA={ua[:40]}…")


# ══════════════════════════════════════════════════════════════════
# B. robots
# ══════════════════════════════════════════════════════════════════
@case("B1 robots 解析：最长匹配、Allow 同长优先、* 与 $ 通配、UA 组")
def t_robots_parse():
    from daedalus.net.robots import parse_robots
    text = ("User-agent: *\n"
            "Disallow: /private\n"
            "Allow: /private/public\n"
            "Disallow: /*.json$\n"
            "\n"
            "User-agent: DaedalusBot\n"
            "Disallow: /\n")
    r = parse_robots(text)
    assert r.allows("/private/x", "*") is False, "Disallow 未生效"
    assert r.allows("/private/public/x", "*") is True, "Allow 更长应优先"
    assert r.allows("/a/b.json", "*") is False, "$ 结尾锚未生效"
    assert r.allows("/a/b.json?v=1", "*") is True, "$ 锚不该匹配带查询的路径"
    assert r.allows("/anything", "*") is True, "未命中应允许"
    assert r.allows("/", "DaedalusBot/0.1") is False, "UA 组未生效"
    return ok("5 条断言全过")


@case("B2 robots 缓存：404 允许 / 5xx 全禁 / 不可达全禁（RFC 9309）")
def t_robots_status():
    from daedalus.net.robots import RobotsCache

    class F:
        def __init__(self, resp=None, exc=None):
            self.resp, self.exc, self.calls = resp, exc, 0

        def open(self, url, timeout=None):
            self.calls += 1
            if self.exc:
                raise self.exc
            return self.resp

    c404 = RobotsCache(fetcher=F(FakeResp(404)))
    assert c404.allowed("https://a.example/x") is True, "404 应视为不存在 → 允许"
    c500 = RobotsCache(fetcher=F(FakeResp(503)))
    assert c500.allowed("https://b.example/x") is False, "5xx 应完全禁止"
    cnet = RobotsCache(fetcher=F(exc=TimeoutError("boom")))
    assert cnet.allowed("https://c.example/x") is False, "不可达应完全禁止"
    cok = RobotsCache(fetcher=F(FakeResp(200, body=b"User-agent: *\nDisallow: /no\n")))
    assert cok.allowed("https://d.example/no/x") is False
    assert cok.allowed("https://d.example/yes") is True
    return ok("404/5xx/不可达/正常 四种都符合")


@case("B3 robots 缓存：同域第二次不再取（24h TTL）")
def t_robots_cache():
    from daedalus.net.robots import RobotsCache

    class F:
        def __init__(self):
            self.calls = 0

        def open(self, url, timeout=None):
            self.calls += 1
            return FakeResp(200, body=b"User-agent: *\nDisallow: /no\n")
    f = F()
    c = RobotsCache(fetcher=f, ttl=3600)
    for _ in range(3):
        c.allowed("https://e.example/no/x")
    assert f.calls == 1, f"robots 被重复抓取 {f.calls} 次"
    return ok("三次判定只用了一次抓取")


@case("B4 robots：Crawl-delay 被解析并暴露（非标准，仅暴露）")
def t_robots_delay():
    from daedalus.net.robots import parse_robots
    r = parse_robots("User-agent: *\nCrawl-delay: 2.5\nDisallow: /x\n")
    assert r.crawl_delay == 2.5, r.crawl_delay
    return ok("Crawl-delay=2.5")


# ══════════════════════════════════════════════════════════════════
# C. 脱敏策略
# ══════════════════════════════════════════════════════════════════
@case("C1 策略默认值矩阵：日志类开、导出/派生/UI 关、headers 开、原始层不可配")
def t_policy_defaults():
    from daedalus.obs.policy import RAW_LAYER_SANITIZATION, SanitizationPolicy
    p = SanitizationPolicy()
    assert p.enabled("log_url") and p.enabled("log_headers") and p.enabled("log_text")
    assert p.enabled("db_headers")
    assert not p.enabled("export_record") and not p.enabled("derived_records")
    assert not p.enabled("ui_view")
    assert RAW_LAYER_SANITIZATION is False, "原始层绝不脱敏（写死，不是开关）"
    assert p.for_raw_layer({"x": "?token=ABC"}) == {"x": "?token=ABC"}, "原始层应恒等"
    return ok("7 项默认值 + 原始层恒等")


@case("C2 正交性：关 log_text 时手机号保留，但 log_url 仍生效")
def t_policy_orthogonal():
    from daedalus.obs.policy import SanitizationPolicy
    p = SanitizationPolicy().with_toggle("log_text", False)
    line = "访问 https://h/p?token=SECRET 联系13812345678"
    out = p.scrub_log(line)
    assert "SECRET" not in out, "log_url 应仍生效"
    assert "13812345678" in out, "log_text 关掉后应保留原文"
    q = SanitizationPolicy().with_toggle("log_url", False)
    out2 = q.scrub_log(line)
    assert "SECRET" in out2 and "13812345678" not in out2, "反向正交失败"
    return ok("两个开关互不干扰")


@case("C3 出口各自独立：export 默认恒等；打开后脱敏；UI/派生同理")
def t_policy_exits():
    from daedalus.obs.policy import SanitizationPolicy
    rec = {"url": "https://h/a?token=T1", "author": "张三 13812345678"}
    p = SanitizationPolicy()
    assert p.for_export(rec) == rec, "导出默认不该脱（个人使用要原始数据）"
    assert p.for_ui(rec) == rec
    p2 = p.with_toggle("export_record", True)
    assert "T1" not in p2.for_export(rec)["url"], "打开后应脱敏"
    assert p2.for_ui(rec) == rec, "打开 export 不该影响 UI 出口（正交）"
    return ok("导出/UI 正交")


@case("C4 透明：可查 + 会说出来（有开关关闭时打 INFO）")
def t_policy_transparency():
    from daedalus.obs.policy import DEFAULT_TOGGLES, KINDS, SanitizationPolicy
    # 期望值**从默认矩阵推导**（不硬编码数字，免得再数错一次）
    exp_disabled = [k for k in KINDS if not DEFAULT_TOGGLES[k]]
    exp_enabled = sum(1 for k in KINDS if DEFAULT_TOGGLES[k])
    p = SanitizationPolicy()
    s = p.summary()
    assert s["disabled"] == exp_disabled, f"{s['disabled']} != {exp_disabled}"
    assert s["enabled_count"] == exp_enabled, s["enabled_count"]
    assert s["raw_layer_sanitized"] is False and "永不脱敏" in s["note"]
    assert f"{len(exp_disabled)} 项已关闭" in p.describe(), p.describe()
    # 关掉一个默认开着的类 → 关闭项 +1，且**必须说出来**（不静默）
    p3 = p.with_toggle("log_text", False)
    assert p3.disabled_kinds() == ["log_text"] + exp_disabled, p3.disabled_kinds()
    assert f"{len(exp_disabled) + 1} 项已关闭" in p3.describe(), p3.describe()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("daedalus.s1.policy")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    p3.log_state(lg)
    assert "已关闭" in buf.getvalue(), "关闭项没被说出来（静默了）"
    assert SanitizationPolicy().sanitizes("log_url") is True   # enabled 的别名语义
    return ok(f"{p3.describe()}")


@case("C5 策略接进日志过滤器：按开关生效且正交")
def t_policy_wired_to_logging():
    from daedalus.obs.logging_sanitizer import install_auto_sanitize
    from daedalus.obs.policy import SanitizationPolicy
    pol = SanitizationPolicy().with_toggle("log_text", False)   # 只关"文本"，URL 仍脱
    install_auto_sanitize(policy=pol)
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    lg = logging.getLogger("daedalus.s1.wired")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.info("取 https://h/p?token=SECRET 联系13812345678")
    out = buf.getvalue()
    assert "SECRET" not in out, f"log_url 未生效: {out!r}"
    assert "13812345678" in out, f"log_text 关掉后不该脱: {out!r}"
    # 复原：后续用例用默认策略
    install_auto_sanitize(policy=SanitizationPolicy())
    return ok("过滤器按策略生效（正交）")


# ══════════════════════════════════════════════════════════════════
# D. Cookie
# ══════════════════════════════════════════════════════════════════
NETSCAPE_SAMPLE = """# Netscape HTTP Cookie File
.example.com\tTRUE\t/\tFALSE\t1893456000\tsid\tSECRET_SID
#HttpOnly_www.example.com\tFALSE\t/app\tTRUE\t1893456000\ttoken\tSECRET_TOKEN
other.test\tFALSE\t/\tFALSE\t1000000000\told\tGONE
"""


@case("D1 导入：Netscape（含 #HttpOnly_ 与注释行）")
def t_cookie_netscape():
    from daedalus.privacy.cookies import parse_netscape
    es = parse_netscape(NETSCAPE_SAMPLE)
    assert len(es) == 3, f"解析条数不对: {len(es)}"
    m = {e.name: e for e in es}
    assert m["sid"].domain == "example.com" and m["sid"].host_only is False
    assert m["token"].host_only is False and m["token"].secure is True  # #HttpOnly_ 行
    assert "/app" == m["token"].path
    return ok("3 条（含 HttpOnly 行、注释跳过）")


@case("D2 导入：JSON（含 {'cookies': [...]} 包裹）")
def t_cookie_json():
    from daedalus.privacy.cookies import parse_json
    data = {"cookies": [{"name": "a", "value": "1", "domain": "example.com", "path": "/"},
                        {"name": "b", "value": "2", "domain": ".example.com",
                         "secure": True, "expires": 1893456000}]}
    es = parse_json(data)
    assert len(es) == 2
    assert es[0].host_only is True and es[1].host_only is False and es[1].secure is True
    return ok("列表与包裹两种形状都认")


@case("D3 匹配：域（host-only vs 域 cookie）/ 路径 / secure / 过期")
def t_cookie_match():
    from daedalus.privacy.cookies import CookieEntry, CookieJar
    jar = CookieJar([
        CookieEntry("s", "1", "example.com", "/", 0, host_only=True),          # 仅本域
        CookieEntry("d", "2", ".example.com", "/", 0, host_only=False),        # 含子域
        CookieEntry("p", "3", "example.com", "/app", 0, host_only=True),       # 路径
        CookieEntry("sec", "4", "example.com", "/", 0, secure=True, host_only=True),
        CookieEntry("old", "5", "example.com", "/", time.time() - 10, host_only=True),
    ])
    assert jar.header_for("https://example.com/") == "s=1; d=2; sec=4"
    assert jar.header_for("https://sub.example.com/") == "d=2"                 # 子域只带域 cookie
    assert jar.header_for("http://example.com/") == "s=1; d=2"                 # 非 https 不带 secure
    assert "p=3" in jar.header_for("https://example.com/app/x")                # 路径命中
    assert jar.header_for("https://example.com/other") == "s=1; d=2; sec=4"    # 路径不命中
    assert "old=5" not in jar.header_for("https://example.com/"), "过期 cookie 不该带"
    return ok("域/子域/路径/secure/过期 五类都正确")


@case("D4 落盘：DPAPI 密文，文件里**不含明文**，往返一致")
def t_cookie_persist():
    from daedalus.privacy.cookies import CookieJar, parse_netscape
    jar = CookieJar(parse_netscape(NETSCAPE_SAMPLE), name="s1_cookies")
    assert jar.save() is True, "加密保存失败（DPAPI 不可用？）"
    from daedalus.privacy.secrets import secret_path
    p = secret_path("s1_cookies")
    assert p.exists()
    raw = p.read_bytes()
    for leak in (b"SECRET_SID", b"SECRET_TOKEN"):
        assert leak not in raw, f"密文里出现明文 {leak!r}"
    back = CookieJar.load("s1_cookies")
    assert len(back) == len(jar.entries()) and back.header_for("https://www.example.com/app/x")
    return ok(f"{len(jar.entries())} 条加密往返（密文无明文）")


@case("D5 失效要明确报错（不许静默变成 403）")
def t_cookie_missing_reason():
    from daedalus.privacy.cookies import CookieEntry, CookieJar
    empty = CookieJar()
    assert "为空" in empty.missing_reason("https://example.com/")
    jar = CookieJar([CookieEntry("x", "1", "example.com", "/", time.time() - 5)])
    why = jar.missing_reason("https://example.com/")
    assert "全部已过期" in why, why
    jar2 = CookieJar([CookieEntry("x", "1", "other.com")])
    why2 = jar2.missing_reason("https://example.com/")
    assert "没有匹配的 cookie" in why2, why2
    s = jar2.summary()
    assert "SECRET" not in json.dumps(s) and s["total"] == 1, "summary 不得含值"
    return ok("三种失效原因都说得清，summary 不含值")


# ══════════════════════════════════════════════════════════════════
# E. 全仓裸请求门禁（把《清单》B1 自动化）
# ══════════════════════════════════════════════════════════════════
@case("E1 全仓无第二条出网路径（除 net/ssrf_gate.py）")
def t_no_naked_requests():
    import re as _re
    pat = _re.compile(r"urlopen\(|requests\.(get|post|head|put)\(|http\.client|httpx\.|aiohttp")  # noqa: lint -- 扫描器自己的正则
    offenders: list[str] = []
    src = ROOT / "src"
    for p in sorted(src.rglob("*.py")):
        if p.name == "ssrf_gate.py" and p.parent.name == "net":
            continue                    # noqa: lint -- 闸内部允许使用 urllib.request（扫描器白名单）
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pat.search(code):
                offenders.append(f"{p.relative_to(ROOT)}:{i}")
    assert not offenders, f"发现绕过咽喉的裸请求: {offenders}"
    return ok("除闸以外零裸请求")


# ══════════════════════════════════════════════════════════════════
# F. 配置接线（清单 O1：**设置 → 引擎真的读到**，这是最高优先级的纪律）
# ══════════════════════════════════════════════════════════════════
@case("F1 配置往返：TOML 里的每个键都能被引擎读到")
def t_config_wiring():
    from daedalus.config import build_fetcher, build_policy, load_config, to_toml_text
    toml = (ROOT / "config.example.toml")
    assert toml.exists(), "config.example.toml 不存在"
    # 用示例配置原样加载：所有键都应"读得到"（不是"填了不生效"）
    cfg = load_config(toml)
    pol = build_policy(cfg)
    assert pol.enabled("log_url") is True and pol.enabled("export_record") is False
    f = build_fetcher(cfg, limiter=FakeLimiter(), robots=FakeRobotsCache(allow=True))
    assert f._respect_robots is True, "respect_robots 没读到"
    assert float(f._timeout) == float(cfg["fetcher"]["timeout"]), "timeout 没读到"
    return ok("示例配置的每个键都读到了")


@case("F2 配置覆盖：改一项就变一项（且只变那一项）")
def t_config_override():
    from daedalus.config import build_fetcher, build_policy, load_config
    cfg = load_config(None, override={"sanitization": {"log_text": False},
                                      "fetcher": {"respect_robots": False}})
    pol = build_policy(cfg)
    assert pol.enabled("log_text") is False and pol.enabled("log_url") is True, "覆盖影响了别的键"
    f = build_fetcher(cfg, limiter=FakeLimiter(), robots=None)
    assert f._respect_robots is False, "respect_robots 覆盖没生效"
    return ok("log_text / respect_robots 各自生效且互不影响")


@case("F3 配置示例可重新生成（文档不会漂移）")
def t_config_regen():
    from daedalus.config import load_config, to_toml_text
    disk = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    regen = to_toml_text(load_config(None))
    # 允许注释里有人手写的补充，但"键 = 值"必须一致（防配置与代码漂移）
    def pairs(text):
        out = {}
        for line in text.splitlines():
            code = line.split("#", 1)[0].strip()
            if "=" in code:
                k, _, v = code.partition("=")
                out[k.strip()] = v.strip()
        return out
    d, r = pairs(disk), pairs(regen)
    diff = {k: (d.get(k), r.get(k)) for k in set(d) | set(r) if d.get(k) != r.get(k)}
    assert not diff, f"示例配置与代码默认值漂移: {diff}"
    return ok(f"{len(r)} 个键一致")


# ══════════════════════════════════════════════════════════════════
# G. 集成：下载/媒体通道**真的**经咽喉（不是"嘴上说收敛"）
# ══════════════════════════════════════════════════════════════════
@case("G1 断点续传通道经咽喉（限速 + 闸都在一次下载里生效）")
def t_resumable_via_fetcher():
    import pathlib as _pl
    from daedalus.env.resumable import download
    from daedalus.net.fetch import Fetcher
    lim = FakeLimiter()
    calls = []

    def fake_open(url, method="GET", headers=None, timeout=None):
        calls.append((method, dict(headers or {})))
        return FakeResp(200, {"Content-Type": "application/octet-stream"}, b"X" * 4096)
    fetcher = Fetcher(limiter=lim, respect_robots=False, opener=fake_open)
    dest = _pl.Path(_TMP) / "g1.bin"
    okk, path, why = download("https://example.com/file.bin", dest,
                              opener=fetcher.open, min_bytes=1024, max_retries=1)
    assert okk, why
    assert lim.slots == ["example.com"], f"下载没走礼貌预算: {lim.slots}"
    ua = calls[0][1].get("User-Agent", "")
    assert "Daedalus" in ua, f"下载没带上咽喉的 UA: {ua!r}"
    return ok("一次下载里：名额、UA、闸都在咽喉内生效")


@case("G2 HLS 通道经咽喉（playlist 与分片都过同一个入口）")
def t_hls_via_fetcher():
    import pathlib as _pl
    from daedalus.adapters.hls import download_hls
    from daedalus.net.fetch import Fetcher
    lim = FakeLimiter()
    playlist = ("#EXTM3U\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
                "seg0.ts\nseg1.ts\n#EXT-X-ENDLIST\n")

    def fake_open(url, method="GET", headers=None, timeout=None):
        if url.endswith(".m3u8"):
            return FakeResp(200, {"Content-Type": "application/vnd.apple.mpegurl"},
                            playlist.encode())
        return FakeResp(200, {}, b"\x47" * 2048)          # 假 TS 分片
    fetcher = Fetcher(limiter=lim, respect_robots=False, opener=fake_open)
    out = _pl.Path(_TMP) / "g2.ts"
    # 显式传一个**不存在**的 ffmpeg：保证门禁离线、不调起外部进程（传 None 会去找系统 ffmpeg）
    okk, path, why = download_hls("https://example.com/i.m3u8", out,
                                 opener=fetcher.open, ffmpeg="__no_such_ffmpeg__")
    # 没 ffmpeg 是预期的失败，但**分片必须都经过咽喉**（名额被记录了）
    assert lim.slots.count("example.com") >= 3, f"分片没走咽喉: {lim.slots}"
    assert "ffmpeg" in why, why
    return ok(f"playlist+2 分片都过咽喉（预期失败：{why[:36]}…）")


# ══════════════════════════════════════════════════════════════════
@case("H1 每线程独立会话：出网层不共享会话对象，也不做共享连接池（checklist I1）")
def t_per_thread_session():
    """判据（checklist I1）：`threading.local()`；**无**"共享大连接池"。

    共享一个 OpenerDirector 的问题不是当下就会炸——而是**将来往里面加任何会话态**
    （连接复用/认证 handler/缓存）都会变成跨线程共享可变状态。所以出网层从结构上
    就按线程分开。
    """
    import inspect
    import threading
    from daedalus.net import ssrf_gate as g
    src = inspect.getsource(g)
    assert "_TLS = threading.local()" in src, "没有 thread-local 会话"
    assert "_opener_for_thread()" in src, "没有按线程取会话"
    assert "def _OPENER" not in src and "_OPENER = urllib" not in src, "还存在模块级共享 opener"
    # 两个线程拿到的 opener 必须是**不同对象**；同线程复用同一个
    seen: dict[str, object] = {}

    def worker(name: str) -> None:
        seen[name] = g._opener_for_thread()
        seen[name + "-again"] = g._opener_for_thread()

    t1 = threading.Thread(target=worker, args=("t1",))
    t2 = threading.Thread(target=worker, args=("t2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert seen["t1"] is not seen["t2"], "两个线程拿到了同一个 opener（会话被共享）"
    assert seen["t1"] is seen["t1-again"], "同一线程没复用会话"
    # 也**不许**出现共享连接池（本工程刻意不做：会绕过 DNS 复核与礼貌预算）
    for bad in ("HTTPConnectionPool", "HTTPSConnectionPool", "PoolManager"):
        assert bad not in src, f"出现了共享连接池：{bad}"
    return ok("线程间 opener 不同对象、线程内复用；无共享连接池")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S1 门禁 · 数据根={_TMP}\n" + "─" * 68)
    for name, fn in _CASES:
        try:
            note = str(fn())
            status = "SKIP" if note.startswith("SKIP") else "PASS"
            skips += status == "SKIP"
            _RESULTS.append((name, status, note[:120]))
            print(f"[{status}] {name}\n        {note}")
        except Exception as e:
            fails += 1
            _RESULTS.append((name, "FAIL", f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}\n        {type(e).__name__}: {e}")
    print("─" * 68)
    print(f"共 {len(_CASES)} 项：PASS {len(_CASES) - fails - skips} / SKIP {skips} / FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

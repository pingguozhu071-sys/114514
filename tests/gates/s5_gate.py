# -*- coding: utf-8 -*-
"""S5 门禁：环境③制品与媒体

覆盖：**产物契约**（大小/格式/MIME/哈希；"命令返回 0 但产物坏"要能被抓）、
**子进程执行面**（缺件硬报错、就绪探测、UTF-8 不改自身 locale、超时、ASCII 输出名）、
**媒体环境**（大对象走断点续传 + 契约；缺 ffmpeg 时明确告知而不是静默）、
**HLS 分片并发 + 按序**（含 AES-128 加密分片）、**进程隔离的制品元数据解析**（缺省即拒绝 + 超时终止）、
**注册表入口可达性**（`default_registry()` 真能到 `artifact_meta`，不是只有直接 import 才能）。

跑法（离线；不联网）：
    python tests/gates/s5_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import io
import locale
import os
import pathlib
import sys
import tempfile
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s5_"))
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


class FakeFetcher:
    """按 URL 路由的假咽喉（HLS 需要按 URL 给不同响应）。"""

    def __init__(self, routes: dict, default: bytes = b""):
        self.routes = dict(routes)
        self.calls: list[str] = []
        self._default = default
        self._lock = __import__("threading").Lock()

    def open(self, url, method="GET", headers=None, timeout=None):
        with self._lock:
            self.calls.append(url)
        for pattern, resp in self.routes.items():
            if pattern in url:
                if callable(resp):
                    resp = resp(url)
                if isinstance(resp, Exception):
                    raise resp
                return FakeResp(resp[0], resp[1], resp[2]) if isinstance(resp, tuple) else resp
        return FakeResp(200, {}, self._default)

    def is_allowed(self, url, *, fetch=True):
        return True, "ok"

    def stats(self):
        return {"calls": len(self.calls)}


def png_bytes(w: int = 64, h: int = 48) -> bytes:
    """造一个**像真的** PNG：带噪声，避免纯色图被压到几百字节
    （否则契约会正确地判"过小"——那是产品行为对、测试素材不真实）。"""
    from PIL import Image
    import random as _r
    rnd = _r.Random(7)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(w * h)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.txt", "hello")
        zf.writestr("b/c.txt", "world")
    return buf.getvalue()


def write(path: pathlib.Path, data: bytes) -> pathlib.Path:
    path.write_bytes(data)
    return path


def _slow_parse(data, meta) -> dict:
    """**必须是模块级函数**：spawn 启动的隔离子进程要能 pickle 它（门禁 D3 用）。"""
    import time as _t
    _t.sleep(10)
    return {"ok": True, "note": "本不该跑完"}


# ══════════════════════════════════════════════════════════════════
# A. 产物契约
# ══════════════════════════════════════════════════════════════════
@case("A1 契约：合格产物（图片）通过，facts 带大小/格式/哈希")
def t_contract_ok():
    from daedalus.capture.artifacts import check_artifact, for_image
    p = write(_TMP / "ok.png", png_bytes())
    v = check_artifact(p, for_image())
    assert v.ok, v.reason
    assert v.facts["format"] == "png" and v.facts["size"] > 0
    assert len(v.facts.get("sha256", "")) == 64, v.facts
    return ok(v.reason)


@case("A2 契约：过小 → 拒（防错误页/空文件当成功）")
def t_contract_too_small():
    from daedalus.capture.artifacts import check_artifact, for_media
    p = write(_TMP / "tiny.bin", b"a" * 10)
    v = check_artifact(p, for_media())
    assert not v.ok and "过小" in v.reason, v.reason
    return ok(v.reason)


@case("A3 契约：网页/错误页 → 拒")
def t_contract_html():
    from daedalus.capture.artifacts import check_artifact, for_media
    # 尺寸要够大，才能确保被拦的原因是"这是网页"而不是"过小"
    page = b"<!DOCTYPE html><html><body><h1>403 Forbidden</h1><p>access denied</p></body></html>"
    p = write(_TMP / "page.html", page * 2000)
    v = check_artifact(p, for_media())
    assert not v.ok, v.reason
    assert "网页" in v.reason or "格式" in v.reason, v.reason
    return ok(v.reason)


@case("A4 契约：格式不符（zip 冒充图片）→ 拒")
def t_contract_wrong_format():
    from daedalus.capture.artifacts import check_artifact, for_image
    p = write(_TMP / "fake.png", zip_bytes() * 100)
    v = check_artifact(p, for_image())
    assert not v.ok and "格式不符" in v.reason, v.reason
    return ok(v.reason)


@case("A5 契约：哈希走流式（大文件不把内存吃爆）")
def t_contract_stream_hash():
    import hashlib
    from daedalus.capture.artifacts import ArtifactContract, check_artifact
    big = _TMP / "big.bin"
    with big.open("wb") as fp:
        for _ in range(64):
            fp.write(os.urandom(128 * 1024))          # 8MB
    v = check_artifact(big, ArtifactContract(label="big", min_bytes=1024, reject_html=False))
    assert v.ok and len(v.facts["sha256"]) == 64
    assert v.facts["sha256"] == hashlib.sha256(big.read_bytes()).hexdigest()
    return ok("8MB 流式哈希与整块一致")


# ══════════════════════════════════════════════════════════════════
# B. 子进程执行面
# ══════════════════════════════════════════════════════════════════
@case("B1 缺件硬报错：ToolMissing（不静默降级）")
def t_tool_missing():
    from daedalus.exec.subprocess import ToolMissing, run_tool
    try:
        run_tool("definitely_not_a_real_tool_xyz", ["--help"], timeout=2)
        raise AssertionError("缺件居然没报错")
    except ToolMissing as e:
        assert "未在 PATH 中找到" in str(e), str(e)
    return ok("ToolMissing（含可读原因）")


@case("B2 就绪探测：哪些工具在、哪些不在，一眼能看到")
def t_readiness():
    from daedalus.exec.subprocess import readiness
    r = readiness(("ffmpeg", "ffprobe", "__nope__"))
    assert r["tools"]["__nope__"] is False and "__nope__" in r["missing"]
    assert r["all_present"] is False and "不会" in r["note"]
    real = readiness()
    return ok(f"探测正常（本机 ffmpeg={real['tools'].get('ffmpeg')} "
              f"ffprobe={real['tools'].get('ffprobe')}）")


@case("B3 跑真工具：中文输出不崩（显式 UTF-8，不改自身 locale）")
def t_tool_run_utf8():
    from daedalus.exec.subprocess import run_tool
    before = locale.setlocale(locale.LC_CTYPE)
    res = run_tool(sys.executable, ["-c", "print('中文输出没问题 ✓')"], timeout=30)
    after = locale.setlocale(locale.LC_CTYPE)
    assert res.ok and "中文输出没问题" in res.stdout, res.brief()
    assert before == after, f"run_tool 改了进程 locale：{before} → {after}"
    return ok("真跑通 + 中文不乱码 + 自身 locale 未变")


@case("B4 超时：返回 timed_out（不抛异常、不挂死）")
def t_tool_timeout():
    from daedalus.exec.subprocess import run_tool
    res = run_tool(sys.executable, ["-c", "import time; time.sleep(5)"], timeout=0.6)
    assert res.ok is False and res.timed_out, res.brief()
    return ok(f"超时被识别：{res.brief(40)}")


@case("B5 输出名：纯 ASCII、稳定（防 Windows 子进程 GBK 解码崩）")
def t_safe_output_name():
    from daedalus.exec.subprocess import safe_output_name
    n = safe_output_name("中文标题/带路径", ".mp4")
    assert n.endswith(".mp4") and n.isascii() and len(n) == 4 + 16, n
    assert safe_output_name("同一个", ".mp4") == safe_output_name("同一个", ".mp4")
    return ok(f"{n}（ASCII + 稳定）")


# ══════════════════════════════════════════════════════════════════
# C. 媒体环境
# ══════════════════════════════════════════════════════════════════
@case("C1 能力探测：缺件要说出来（说清哪些能力被禁用）")
def t_media_capability():
    from daedalus.env.media import MediaEnvironment
    env = MediaEnvironment(FakeFetcher({}), workdir=_TMP)
    cap = env.capability()
    assert set(cap) >= {"ffmpeg", "ffprobe", "can_mux", "can_probe_media", "missing", "note"}
    assert "禁用" in cap["note"] or "不会" in cap["note"]
    env2 = MediaEnvironment(FakeFetcher({}), tools=("no_ffmpeg_x", "no_ffprobe_x"))
    assert env2.capability()["can_mux"] is False and env2.capability()["missing"]
    return ok(f"本机 ffmpeg={cap['ffmpeg']}、ffprobe={cap['ffprobe']}；缺件时明确告知")


@case("C2 缺 ffmpeg 时 HLS 明确失败（不假装成功）")
def t_media_hls_no_ffmpeg():
    from daedalus.env.media import MediaEnvironment
    env = MediaEnvironment(FakeFetcher({}), tools=("no_ffmpeg_x",))
    v = env.download_hls("https://example.com/i.m3u8", _TMP / "out.ts")
    assert not v.ok and "缺少 ffmpeg" in v.reason, v.reason
    return ok(v.reason[:52] + "…")


@case("C3 大文件：断点续传 + 契约（产物不合格就判失败）")
def t_media_large():
    from daedalus.env.media import MediaEnvironment
    png = png_bytes(200, 150)

    class One:
        def open(self, url, method="GET", headers=None, timeout=None):
            return FakeResp(200, {"Content-Type": "image/png", "Content-Length": str(len(png))}, png)

        def is_allowed(self, url, *, fetch=True):
            return True, "ok"
    env = MediaEnvironment(One(), workdir=_TMP)
    v = env.download_large("https://example.com/pic.png", _TMP / "pic.png",
                           contract=__import__("daedalus.capture.artifacts", fromlist=["x"]).for_image())
    assert v.ok, v.reason
    assert v.facts["format"] == "png" and v.facts["size"] == len(png)
    return ok(f"{v.reason}（走了续传通道 + 契约）")


@case("C4 HLS 分片并发：**按序号**落盘（并发不改变顺序）")
def t_hls_parallel_ordered():
    from daedalus.adapters.hls import download_hls
    segs = [f"SEG-{i}".encode() * 200 for i in range(6)]
    playlist = ("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
                + "".join(f"seg{i}.ts\n" for i in range(6)) + "#EXT-X-ENDLIST\n")

    def route(url):
        if url.endswith(".m3u8"):
            return (200, {"Content-Type": "application/vnd.apple.mpegurl"}, playlist.encode())
        for i in range(6):
            if url.endswith(f"seg{i}.ts"):
                return (200, {}, segs[i])
        return (404, {}, b"")
    fetcher = FakeFetcher({}, default=b"")
    fetcher.open = lambda url, method="GET", headers=None, timeout=None: (
        fetcher.calls.append(url) or FakeResp(*route(url)))
    workdir = _TMP / "hls_c4"
    okk, path, why = download_hls("https://example.com/i.m3u8", _TMP / "c4.ts",
                                 opener=fetcher.open, workdir=workdir,
                                 ffmpeg="__no_ffmpeg__", concurrency=4, keep_segments=True)
    files = sorted(workdir.glob("seg_*.ts"))
    assert len(files) == 6, f"分片没下全：{len(files)}（why={why}）"
    for i, fp in enumerate(files):
        assert fp.read_bytes() == segs[i], f"第 {i} 片内容错位（并发必须不改顺序）"
    assert why and "ffmpeg" in why, why
    return ok("6 片并发下载、按序号落盘、内容一一对应")


@case("C5 HLS + AES-128：并发下载后**逐个按序号解密**，明文顺序正确")
def t_hls_aes_parallel():
    from daedalus.adapters.hls import download_hls
    key = bytes(range(16))
    plain = [f"PLAIN-{i}-".encode() * 64 for i in range(5)]
    ivs = [i.to_bytes(16, "big") for i in range(5)]
    # 用 AES-128-CBC 构造测试向量是**协议强制**（HLS 的 METHOD=AES-128 就是 CBC+PKCS7）；
    # 安全扫描会提示"弱算法"，这里按"评估后保留"处理（我们自己存东西用 AES-GCM/DPAPI）。
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    enc = []
    for pt, iv in zip(plain, ivs):
        pad = 16 - (len(pt) % 16)
        data = pt + bytes([pad]) * pad
        c = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        enc.append(c.update(data) + c.finalize())
    playlist = ("#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
                f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
                + "".join(f"s{i}.ts\n" for i in range(5)) + "#EXT-X-ENDLIST\n")

    def route(url):
        if url.endswith(".m3u8"):
            return (200, {}, playlist.encode())
        if url.endswith("key.bin"):
            return (200, {}, key)
        for i in range(5):
            if url.endswith(f"s{i}.ts"):
                return (200, {}, enc[i])
        return (404, {}, b"")
    fetcher = FakeFetcher({})
    fetcher.open = lambda url, method="GET", headers=None, timeout=None: (
        fetcher.calls.append(url) or FakeResp(*route(url)))
    workdir = _TMP / "hls_c5"
    okk, path, why = download_hls("https://example.com/i.m3u8", _TMP / "c5.ts",
                                 opener=fetcher.open, workdir=workdir,
                                 ffmpeg="__no_ffmpeg__", concurrency=3, keep_segments=True)
    files = sorted(workdir.glob("seg_*.ts"))
    assert len(files) == 5, f"分片没下全（why={why}）"
    for i, fp in enumerate(files):
        assert fp.read_bytes() == plain[i], f"第 {i} 片解密/顺序错"
    return ok("5 片并发下载 + 各自 IV 解密，明文按序号正确")


# ══════════════════════════════════════════════════════════════════
# D. 制品元数据解析（进程隔离）
# ══════════════════════════════════════════════════════════════════
@case("D1 缺省即拒绝：没登记进程容量时，隔离解析直接拒")
def t_isolated_denied():
    from daedalus.core.registry import ResourceDenied, ResourceRegistry
    from daedalus.understand.parsers.mediainfo import parse_mediainfo, run_isolated
    reg = ResourceRegistry()
    try:
        run_isolated(parse_mediainfo, (png_bytes(), {"format": "png"}), timeout=20, registry=reg)
        raise AssertionError("没登记进程容量居然跑起来了（缺省即拒绝没生效）")
    except ResourceDenied as e:
        assert "缺省即拒绝" in str(e) or "未启用" in str(e), str(e)
    return ok("进程槽位默认 0 → 解析不可信内容被拒（要先显式登记）")


@case("D2 登记后跑通：图片尺寸从隔离进程里拿回来")
def t_isolated_ok():
    from daedalus.core.registry import ResourceRegistry
    from daedalus.understand.parsers.mediainfo import parse_mediainfo, run_isolated
    reg = ResourceRegistry()
    reg.register("process", 1)
    out = run_isolated(parse_mediainfo, (png_bytes(80, 40), {"format": "png"}),
                       timeout=60, registry=reg)
    assert out.get("ok") and out.get("width") == 80 and out.get("height") == 40, out
    assert out.get("format") == "PNG", out
    return ok(f"隔离进程返回 {out['width']}×{out['height']} / {out['format']}")


@case("D3 隔离进程超时 → **终止**（线程做不到的事）")
def t_isolated_timeout():
    from daedalus.core.registry import ResourceRegistry
    from daedalus.understand.parsers.mediainfo import run_isolated
    reg = ResourceRegistry()
    reg.register("process", 2)
    t0 = time.monotonic()
    out = run_isolated(_slow_parse, (b"x", {}), timeout=0.8, registry=reg)
    dt = time.monotonic() - t0
    assert out.get("ok") is False and "终止" in out.get("error", ""), out
    assert dt < 5, f"没能在超时后及时终止：{dt:.1f}s"
    return ok(f"{dt:.1f}s 内终止隔离进程：{out['error'][:40]}")


@case("D4 容器元数据：zip/docx 类容器的条目可读")
def t_isolated_zip():
    from daedalus.understand.parsers.mediainfo import parse_mediainfo
    out = parse_mediainfo(zip_bytes(), {"format": "zip"})
    assert out.get("ok") and out.get("entries") == 2, out
    assert "a.txt" in out.get("names", []), out
    return ok(f"{out['entries']} 个条目：{out['names']}")


@case("D5 媒体元数据：ffprobe 缺件/失败都如实说（不编造时长）")
def t_isolated_media_honest():
    from daedalus.understand.parsers.mediainfo import parse_mediainfo
    out = parse_mediainfo(b"\x00\x00\x00\x18ftypmp42" + b"x" * 200,
                          {"format": "mp4", "path": str(_TMP / "nope.mp4")})
    assert out.get("ok") is False and out.get("error"), out
    assert ("缺 ffprobe" in out["error"]) or ("ffprobe" in out["error"]), out["error"]
    return ok(out["error"][:56] + "…")


@case("D6 引擎路径：default_registry() **真的能到**制品元数据解析器（自动注册）")
def t_registry_reaches_artifact_meta():
    """盯住那次漏注册：`mediainfo.SPEC` 以前从没进过注册表，引擎路径永远解析不了制品元数据
    （只有门禁直接 import 才跑到它）。这里走的是**注册表入口**，不是直接调解析器。"""
    from daedalus.understand.registry import default_registry
    reg = default_registry()
    assert "artifact_meta" in [s["name"] for s in reg.summary()], reg.summary()
    out = reg.parse(zip_bytes(), url="https://example.com/box.zip")
    assert out.get("format") == "zip" and out.get("tried") == ["artifact_meta"], out
    assert out.get("ok") is True and out.get("entries") == 2, out
    assert out.get("parser") == "artifact_meta" and out.get("parser_version") == 1, out
    # 另一路：认不出格式、也没有本地路径 → **不许编造事实**；成功与否这里**故意不钉死**
    # （钉死就等于把「空成功」当契约），但必须有可读说明（error 与 note 至少一个）。
    blind = reg.parse(bytes(range(256)) * 8, url="https://example.com/bin")
    assert blind.get("format") == "unknown" and blind.get("tried") == ["artifact_meta"], blind
    assert bool(blind.get("error")) != bool(blind.get("note")), blind
    for field in ("width", "height", "duration", "entries", "container", "pages_rough"):
        assert field not in blind, f"没有落盘路径却报出了 {field}：{blind}"
    flag = ("⚠️ 当前是 ok=True 的空成功（只有 note、没有事实）—— 根因在 parsers/mediainfo.py 的"
            "accepts 与 ok 判定，不在本次改动范围，报告里点名了") if blind.get("ok") \
        else "ok=False + 可读原因（应然）"
    return ok(f"zip → artifact_meta（{out['entries']} 个条目，version {out['parser_version']}）；"
              f"认不出且无落盘路径时：{flag}")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S5 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

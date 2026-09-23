# -*- coding: utf-8 -*-
"""S3 门禁：捕获面 + 理解面骨架

覆盖：原始层（内容寻址/去重/压缩/血缘/可校验）、探测链、解析器注册表（同构返回/可插拔）、
归一化、质量闸（拒收≠重试）、**离线重放（reparse，不联网）**、去重与增量台账、
**中文全文检索（FTS5 + 预分词）**。

跑法（离线）：
    python tests/gates/s3_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s3_"))
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


def stack(name: str):
    """(store, index, writer, registry, ledger)"""
    from daedalus.capture.index import ArtifactIndex
    from daedalus.capture.rawstore import RawStore
    from daedalus.store.db import Database
    from daedalus.store.deadletter import DeadLetter
    from daedalus.store.writer import SingleWriter
    from daedalus.understand.ledger import ChangeLedger
    from daedalus.understand.registry import default_registry
    db = Database(_TMP / f"{name}.db")
    dl = DeadLetter(path=_TMP / f"{name}.deadletter.jsonl", db=db)
    writer = SingleWriter(db, dead_letter=dl, batch_rows=500, flush_interval=0.05).start()
    store = RawStore(_TMP / f"{name}_data", db, writer)
    return store, ArtifactIndex(db), writer, default_registry(), ChangeLedger(name)


HTML = ("<html><head><title>测试页</title>"
        '<meta name="description" content="一个用于门禁的页面">'
        '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Article",'
        '"headline":"结构化标题"}</script></head>'
        "<body><h1>标题</h1><p>" + ("这是正文内容，用来让质量闸通过。" * 40) + "</p>"
        '<a href="/next">下一页</a><a href="https://other.example/x#frag">外链</a></body></html>')
RSS = ("<?xml version='1.0'?><rss version='2.0'><channel><title>示例订阅</title>"
       "<item><title>第一篇</title><link>https://example.com/a</link>"
       "<pubDate>Wed, 21 Oct 2026 07:28:00 GMT</pubDate><description>摘要一</description></item>"
       "<item><title>第二篇</title><link>https://example.com/b</link></item></channel></rss>")
JSONDOC = '{"@context":"https://schema.org","name":"示例","tags":["a","b"],"n":3}'
SHELL = "<html><head><title>请开启 JavaScript</title></head><body>登录 注册 导航</body></html>"


# ══════════════════════════════════════════════════════════════════
# A. 原始层
# ══════════════════════════════════════════════════════════════════
@case("A1 原始层：内容寻址 + 元数据齐全（血缘）")
def t_raw_put():
    store, index, writer, _, _ = stack("a1")
    try:
        art = store.put(HTML.encode(), url="https://example.com/p1", status=200,
                        headers={"Content-Type": "text/html; charset=utf-8"},
                        mime="text/html", source="network/http", session_id="s1",
                        parent_task="t1", discovery_path="seed")
        assert art["sha256"] and len(art["sha256"]) == 64
        assert art["path"].startswith(f"raw/{art['sha256'][:2]}/"), art["path"]
        assert art["size"] == len(HTML.encode()) and art["status"] == 200
        rows = index.by_task("t1")
        assert len(rows) == 1 and rows[0].sha256 == art["sha256"]
        assert rows[0].discovery_path == "seed" and rows[0].source == "network/http"
        assert store.verify(art["sha256"], art["size"])[0] is True
        return ok(f"{art['path']}（{art['size']} 字节，血缘齐全）")
    finally:
        writer.stop()


@case("A2 去重：同内容同 URL 两次 → 磁盘一份、索引一条")
def t_raw_dedup():
    store, index, writer, _, _ = stack("a2")
    try:
        a1 = store.put(HTML.encode(), url="https://example.com/same", mime="text/html")
        a2 = store.put(HTML.encode(), url="https://example.com/same", mime="text/html")
        assert a1["sha256"] == a2["sha256"]
        files = list((_TMP / "a2_data" / "raw").rglob("*.bin*"))
        assert len(files) == 1, f"磁盘上不止一份：{files}"
        assert len(index.by_url("https://example.com/same")) == 1, "索引重复登记"
        return ok("磁盘 1 份 + 索引 1 条（(sha256,url) 去重）")
    finally:
        writer.stop()


@case("A3 内容寻址跨 URL：同内容共用同一份字节")
def t_raw_same_content():
    store, index, writer, _, _ = stack("a3")
    try:
        a = store.put(RSS.encode(), url="https://example.com/feed.xml", mime="application/rss+xml")
        b = store.put(RSS.encode(), url="https://mirror.example/feed.xml", mime="application/rss+xml")
        assert a["sha256"] == b["sha256"] and a["path"] == b["path"]
        assert len(index.by_content(a["sha256"])) == 2, "两个 URL 都该有索引行"
        return ok("同内容不同 URL：一份字节、两条索引（血缘各自保留）")
    finally:
        writer.stop()


@case("A4 逐记录压缩：长文本压成 .gz，读回字节一致")
def t_raw_compress():
    store, index, writer, _, _ = stack("a4")
    try:
        big = ("<html><body>" + "内容" * 5000 + "</body></html>").encode()
        art = store.put(big, url="https://example.com/big", mime="text/html")
        assert art["path"].endswith(".bin.gz"), art["path"]
        assert store.read(art["sha256"]) == big, "读回的字节与写进去的不一致"
        small = store.put(b"tiny", url="https://example.com/tiny", mime="text/html")
        assert small["path"].endswith(".bin"), "小文件不该压缩"
        return ok("长文本 .bin.gz（自描述后缀），小文件不压")
    finally:
        writer.stop()


@case("A5 校验能证伪：磁盘被改坏时 verify 必须失败")
def t_raw_verify_falsify():
    store, index, writer, _, _ = stack("a5")
    try:
        art = store.put(b"hello world" * 20, url="https://example.com/v", mime="text/plain")
        p = store.path_for(art["sha256"])
        p.write_bytes(b"tampered")                      # 手动改坏
        good, why = store.verify(art["sha256"])
        assert good is False and "不符" in why or "失败" in why, why
        return ok(f"篡改被发现：{why[:40]}…")
    finally:
        writer.stop()


# ══════════════════════════════════════════════════════════════════
# B. 探测链
# ══════════════════════════════════════════════════════════════════
@case("B1 探测链：魔数优先（PNG/PDF/ZIP/GZIP/MP3/MP4）")
def t_detect_magic():
    from daedalus.understand.detect import detect
    cases = [(b"\x89PNG\r\n\x1a\n" + b"x" * 40, "png"),
             (b"%PDF-1.7\n" + b"x" * 40, "pdf"),
             (b"PK\x03\x04" + b"x" * 40, "zip"),
             (b"\x1f\x8b\x08" + b"x" * 40, "gzip"),
             (b"ID3\x04" + b"x" * 40, "mp3"),
             (b"\x00\x00\x00\x18ftypmp42" + b"x" * 40, "mp4")]
    for blob, want in cases:
        g = detect(blob, {"content_type": "text/html"})     # 故意给错声明的类型
        assert g.name == want and g.how == "magic", f"{want} → {g}"
    return ok(f"{len(cases)} 种魔数都对（且压过了错误的 Content-Type）")


@case("B2 探测链：XML 根元素分辨 RSS / Atom / Sitemap")
def t_detect_xml():
    from daedalus.understand.detect import detect
    assert detect(RSS.encode()).name == "rss"
    assert detect(b"<?xml version='1.0'?><feed xmlns='...'><entry/></feed>").name == "atom"
    assert detect(b"<?xml version='1.0'?><urlset></urlset>").name == "sitemap"
    return ok("rss / atom / sitemap 由根元素判定（how=xml-root）")


@case("B3 探测链：扩展名 / Content-Type / 文本 / 兜底")
def t_detect_rest():
    from daedalus.understand.detect import detect
    assert detect(b"not m3u8 really", url="https://h/x.m3u8").name == "hls"       # 扩展名
    assert detect(b"no marker", meta={"content_type": "text/html"}).how == "content-type"
    assert detect(("<html><body>hi</body></html>" * 3).encode()).name == "html"
    assert detect(json.dumps({"a": 1}).encode()).name == "json"
    g = detect(bytes(range(256)) * 8)                     # 随机二进制
    assert g.name == "unknown" and g.how == "fallback" and g.known is False, g
    return ok("扩展名/声明类型/文本/兜底 都对；认不出时**如实说认不出**")


# ══════════════════════════════════════════════════════════════════
# C. 解析器注册表
# ══════════════════════════════════════════════════════════════════
@case("C1 注册表：同构返回（成功带字段、失败带可读原因与 tried）")
def t_registry_homogeneous():
    _, _, _, reg, _ = stack("c1")
    good = reg.parse(HTML.encode(), url="https://example.com/p1")
    assert good["ok"] is True and good["parser"] == "html_text"
    assert good["title"] == "测试页" and good["format"] == "html"
    assert len(good["links"]) == 2 and good["links"][0] == "https://example.com/next"
    assert good["links"][1] == "https://other.example/x", good["links"]     # 去 fragment
    assert good["json_ld"] and good["json_ld"][0]["headline"] == "结构化标题"
    bad = reg.parse(bytes(range(256)) * 8, url="https://example.com/bin")
    assert bad["ok"] is False and bad["error"] and bad["tried"] == [], bad
    assert "没有解析器" in bad["error"] and "原始数据已存" in bad["error"], bad["error"]
    return ok("成功：title/links/json_ld 齐全；未知格式：可读原因 + 不改行为")


@case("C2 可插拔：新注册的解析器立刻生效（历史数据可重扫的前提）")
def t_registry_plugin():
    _, _, _, reg, _ = stack("c2")
    from daedalus.understand.registry import ParserSpec

    def parse_csv(data, meta):
        text = data.decode("utf-8", "replace")
        rows = [ln.split(",") for ln in text.strip().splitlines() if ln]
        if not rows:
            return {"ok": False, "error": "空 CSV"}
        return {"ok": True, "rows": rows, "row_count": len(rows)}

    reg.register(ParserSpec(name="csv_basic", version=1, accepts=("csv",), parse=parse_csv,
                            order=5, note="门禁用：证明可插拔"))
    out = reg.parse(b"a,b\n1,2\n", url="https://example.com/t.csv")
    assert out["ok"] is True and out["parser"] == "csv_basic" and out["row_count"] == 2, out
    assert any(p["name"] == "csv_basic" and p["version"] == 1 for p in reg.summary())
    return ok("注册即生效（含版本号，进派生记录）")


@case("C3 解析器崩了不塌上层：自动落到下一个候选")
def t_registry_failure_isolation():
    _, _, _, reg, _ = stack("c3")
    from daedalus.understand.registry import ParserSpec

    def boom(data, meta):
        raise RuntimeError("我就是坏的")

    reg.register(ParserSpec(name="always_boom", version=1, accepts=("html",), parse=boom, order=1))
    out = reg.parse(HTML.encode(), url="https://example.com/p1")
    assert out["ok"] is True and out["parser"] == "html_text", out
    assert out["tried"] == ["always_boom", "html_text"], out["tried"]
    return ok("坏解析器被跳过，好解析器接手（tried 可见）")


# ══════════════════════════════════════════════════════════════════
# D. 离线重放（reparse）
# ══════════════════════════════════════════════════════════════════
def seed_three(store):
    """写三条原始数据（HTML / RSS / JSON）。"""
    return [
        store.put(HTML.encode(), url="https://example.com/p1", mime="text/html",
                  parent_task="t-seed", discovery_path="seed"),
        store.put(RSS.encode(), url="https://example.com/feed.xml",
                  mime="application/rss+xml", parent_task="t-seed"),
        store.put(JSONDOC.encode(), url="https://example.com/data.json",
                  mime="application/json", parent_task="t-seed"),
    ]


@case("D1 重放：三条原始数据全部重新解释，且**不联网**（模块级保证）")
def t_replay_no_network():
    src = (ROOT / "src" / "daedalus" / "capture" / "replay.py").read_text(encoding="utf-8")
    assert "daedalus.net" not in src, "重放模块不允许引用取流组件（必须离线）"
    store, index, writer, reg, ledger = stack("d1")
    try:
        seed_three(store)
        got = []
        rp = type("R", (), {})  # 占位，实际用下面的类
        from daedalus.capture.replay import Reparser
        r = Reparser(store, reg, index, ledger)
        stats = r.run(parent_task="t-seed", deliver=lambda rec, row: got.append(rec))
        # 3 份原始数据 → 4 条记录（订阅里有 2 个条目，按条目展开）
        assert stats["scanned"] == 3 and stats["parsed_ok"] == 3, stats
        assert stats["records"] == 4 and stats["delivered"] == 4 and stats["accepted"] == 4, stats
        kinds = sorted(rec["format"] for rec in got)
        assert kinds == ["html", "json", "rss", "rss"], kinds
        assert all(rec["content_hash"] for rec in got), "派生记录必须带内容指纹"
        assert all(rec["source_sha256"] for rec in got), "派生记录必须指向原始层条目（可重放）"
        return ok(f"扫 {stats['scanned']} 份 → 展开 {stats['records']} 条 → 投递 {stats['delivered']} 条")
    finally:
        writer.stop()


@case("D2 增量：同一批重放第二次全是 unchanged（内容哈希=地面真值）")
def t_replay_incremental():
    store, index, writer, reg, ledger = stack("d2")
    try:
        seed_three(store)
        from daedalus.capture.replay import Reparser
        known: dict = {}
        r = Reparser(store, reg, index, ledger)
        s1 = r.run(parent_task="t-seed", deliver=lambda rec, row: known.update(
            {rec["content_hash"]: {"simhash": rec["simhash"], "url": rec["url"]}}))
        assert s1["states"].get("new") == 4, s1["states"]
        ledger.reset()
        s2 = r.run(parent_task="t-seed", deliver=lambda rec, row: None, known=known)
        assert s2["states"].get("unchanged") == 4, f"第二次不该是 new：{s2['states']}"
        assert s2["states"].get("new") in (None, 0)
        return ok("首轮 4×new，二轮 4×unchanged（'未变'是一等结果）")
    finally:
        writer.stop()


@case("D3 近似重复：改动少量文字 → 判为 dup 并在台账里可见")
def t_replay_near_dup():
    store, index, writer, reg, ledger = stack("d3")
    try:
        store.put(HTML.encode(), url="https://example.com/orig", mime="text/html")
        tweaked = HTML.replace("这是正文内容，用来让质量闸通过。", "这是正文内容，用来让质量闸通过！", 1)
        store.put(tweaked.encode(), url="https://example.com/copy", mime="text/html")
        from daedalus.capture.replay import Reparser
        seen = []
        r = Reparser(store, reg, index, ledger)
        stats = r.run(deliver=lambda rec, row: seen.append(rec))
        assert stats["delivered"] == 2, stats
        dup = [rec for rec in seen if rec["duplicate_of"]]
        assert dup, "近似重复没有被标出来"
        assert "汉明距离" in " ".join(ledger.events("new")[0].get("detail", "") for _ in [0]) \
            or any("近似" in e.get("detail", "") for e in ledger.events("new")), ledger.events("new")
        return ok(f"{len(dup)} 条标为重复（duplicate_of 指向原记录）")
    finally:
        writer.stop()


@case("D4 质量闸：空壳页被拒收（记为 failed，**不是**重试）")
def t_replay_quality_gate():
    store, index, writer, reg, ledger = stack("d4")
    try:
        store.put(SHELL.encode(), url="https://example.com/shell", mime="text/html")
        from daedalus.capture.replay import Reparser
        seen = []
        r = Reparser(store, reg, index, ledger)
        stats = r.run(deliver=lambda rec, row: seen.append(rec))
        assert stats["rejected"] == 1 and stats["delivered"] == 0, stats
        ev = ledger.events("failed")
        assert ev and "质量闸拒收" in ev[0]["detail"], ev
        return ok(f"拒收 1 条：{ev[0]['detail'][:48]}…")
    finally:
        writer.stop()


@case("D5 归一化：时间统一成 epoch；认不出就 None（不编造）")
def t_normalize_time():
    from daedalus.understand.normalize import normalize_record, to_epoch
    iso = normalize_record({"url": "https://e/x", "title": " t ", "published_at": "2026-10-21T07:28:00Z"})
    assert abs(iso["published_at"] - 1792567680) < 86400 * 2, iso["published_at"]
    rss = normalize_record({"url": "https://e/y", "pubDate": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert rss["published_at"] is not None
    assert to_epoch("不是时间") is None and to_epoch("") is None and to_epoch(None) is None
    assert to_epoch(1792567680000) == 1792567680.0        # 毫秒 → 秒
    rec = normalize_record({"url": "https://e/z", "title": "  多   空格  ", "links": ["https://a", "https://a", "ftp://x"]})
    assert rec["title"] == "多 空格" and rec["links"] == ["https://a"], rec
    return ok("ISO/RFC822/毫秒 都归一到 epoch；垃圾时间→None；链接去重")


# ══════════════════════════════════════════════════════════════════
# E. 台账
# ══════════════════════════════════════════════════════════════════
@case("E1 台账：五态计数、可读摘要、JSONL 追加")
def t_ledger():
    from daedalus.understand.ledger import LEDGER_STATES, ChangeLedger
    led = ChangeLedger("unit")
    led.record("new", url="https://e/1")
    led.record("unchanged", url="https://e/2")
    led.record("failed", url="https://e/3", detail="解析失败")
    s = led.summary()
    assert s["counts"]["new"] == 1 and s["counts"]["unchanged"] == 1 and s["total"] == 3, s
    lines = "\n".join(led.to_lines())
    assert "新增 1" in lines and "未变 1" in lines and "失败 1" in lines, lines
    p = _TMP / "ledger.jsonl"
    led.write_jsonl(p)
    led.write_jsonl(p)                                  # 追加，不覆盖
    content = p.read_text(encoding="utf-8").splitlines()
    assert len(content) >= 6, content                    # 两次 summary + 事件
    try:
        led.record("bogus")
        raise AssertionError("未知状态没被拒")
    except KeyError:
        pass
    assert LEDGER_STATES == ("new", "unchanged", "updated", "failed", "policy_denied")
    return ok(f"五态计数 + 可读摘要 + 追加式 JSONL（{len(content)} 行）")


# ══════════════════════════════════════════════════════════════════
# F. 中文全文检索（checklist G5）
# ══════════════════════════════════════════════════════════════════
@case("F1 中文全文检索：FTS5 + 预分词（「采集」能命中「数据采集引擎」）")
def t_chinese_fts():
    """判据来自 checklist G5：**用中文词检索要能命中**。

    为什么不能直接靠 FTS5 的内置分词：`unicode61` 按 Unicode 词边界切，中文整段会被当成
    一个词——「数据采集引擎」用「采集」搜不到。所以入库时用本工程的 `tokens_of()`
    （中文二元组）预分词，查询时用同一套分词构造短语查询。
    """
    import pathlib
    import sqlite3
    import tempfile
    root = pathlib.Path(tempfile.mkdtemp(prefix="dae_fts3_"))
    conn = sqlite3.connect(str(root / "fts.db"))
    conn.row_factory = sqlite3.Row
    from daedalus.frontier.migrations import apply_migrations
    names = apply_migrations(conn)
    assert any("0002" in n for n in names), names
    from daedalus.store.search import delete_page, index_page, search
    index_page(conn, url_hash="h1", url="https://x/1", title="数据采集引擎设计",
               text="这是一台统一采集与感知引擎。先捕获后理解，原始层永不丢失。")
    index_page(conn, url_hash="h2", url="https://x/2", title="Unified acquisition engine",
               text="capture first, understand later.")
    cases = {"采集": 1, "引擎": 1, "原始层": 1, "捕获": 1, "capture": 1,
             "understand later": 1, "不存在的词": 0, "": 0}
    got = {q: len(search(conn, q)) for q in cases}
    bad = {q: (got[q], n) for q, n in cases.items() if got[q] != n}
    assert not bad, f"检索命中数不符（实际, 期望）：{bad}"
    # 索引是派生层：删页面就要删索引；重建靠**重放**（不是从 pages 表重建——那表里没有正文）
    delete_page(conn, url_hash="h1")
    assert len(search(conn, "采集")) == 0, "删页后索引还在"
    assert len(search(conn, "capture")) == 1, "删一页影响了另一页"
    src = (ROOT / "src" / "daedalus" / "store" / "search.py").read_text(encoding="utf-8")
    head = src.split("为什么这里")[0]
    assert "def rebuild_from_pages" not in head, "又出现了「从 pages 重建」的错误设计"
    conn.close()
    return ok(f"{len(cases)} 个查询命中数全部正确（含中文分词、多词与空查询）；删页即删索引")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S3 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

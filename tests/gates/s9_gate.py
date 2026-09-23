# -*- coding: utf-8 -*-
"""S9 门禁：命令行（CLI = 一台引擎的机器接口）

覆盖：
  A 元信息与配置    A1 `version`｜A2 `config` 显示**引擎真正读到的**配置｜A3 `--write-example`/`--toml`
  B 自检            B1 `doctor` 结构与结论｜B2 缺件如实报（warnings/problems 分类对）
  C 采集闭环        C1 `collect` 真跑（注入假咽喉）→ 原始层 + 派生行 + 台账 + JSON 报告
                    C2 退出码语义（全成功 0 / 有失败 1 / 全被拒 3 / 用法错 2）
                    C3 **dry-run 自证**：`network_calls == 0`（实测，不是嘴上说）
  D 离线重放        D1 `reparse` 用新解析器重扫历史原始数据，**网络调用增量为 0**
  E 导出与观测      E1 `export` 产出 JSONL（台账 + 任务）｜E2 `metrics`/`alerts` 的退出码
  F 接口纪律        F1 `--json` 时 stdout 只有 JSON（日志走 stderr）｜F2 未预期异常 → 退出码 4

跑法（离线；不联网）：
    python tests/gates/s9_gate.py       # 退出码 0 = 全通过
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import contextlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s9_"))
os.environ["DAEDALUS_DATA_ROOT"] = str(_TMP)

_CASES: list[tuple[str, object]] = []


def case(name: str):
    def deco(fn):
        _CASES.append((name, fn))
        return fn
    return deco


def ok(note: str = "") -> str:
    return f"PASS {note}".strip()


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """跑 CLI，返回 (退出码, stdout, stderr)。

    ⚠️ **一次 CLI 命令 = 一个 app 生命周期**：命令结束就关闭整台引擎（关闭链唯一）。
    所以测试里 CLI 调用**之后**不能再碰 app 的组件（写线程已停），只能读库或看 JSON 输出。
    """
    from daedalus import cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return int(code), out.getvalue(), err.getvalue()


def _ro_connect(path):
    """CLI 跑完后用**独立只读连接**查库（不碰 app 的任何组件）。"""
    import sqlite3
    return sqlite3.connect(f"file:{pathlib.Path(path).as_posix()}?mode=ro", uri=True)


# SQL 一律**字面量内联**（本机安全策略禁动态 SQL：`execute()` 的第一个实参必须是字面量）：
# 所以每张表一个小函数，而不是一个收 SQL 字符串的通用计数器（那是被禁的形状）。
def count_tasks(path) -> int:
    conn = _ro_connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    finally:
        conn.close()


def count_raw(path) -> int:
    conn = _ro_connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM raw_artifacts").fetchone()[0])
    finally:
        conn.close()


def count_pages(path) -> int:
    conn = _ro_connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    finally:
        conn.close()


class _AppStack:
    """注入用的"测试 app"：真组件 + 假咽喉（与基准/长跑同一套闭环）。"""

    def __init__(self, root: pathlib.Path, *, shell_urls=()):
        from _harness import BenchPayload, build_offline_stack
        bodies = {u: ("text/html; charset=utf-8",
                      b"<!DOCTYPE html><html><head><title>Please enable JavaScript</title>"
                      b"</head><body>login</body></html>") for u in shell_urls}
        stack, fetcher = build_offline_stack(root, payload=BenchPayload(pages=20),
                                             extra_bodies=bodies)
        self._stack = stack
        self.fetcher = fetcher
        self.data_root = root
        # 把 stack 的组件挂成 EngineApp 的形状（CLI 只用到这些）
        self.db = stack["db"]
        self.writer = stack["writer"]
        self.frontier = stack["frontier"]
        self.store = stack["store"]
        self.index = stack["store"].index if hasattr(stack["store"], "index") else None
        self.ledger = stack["ledger"]
        self.registry = stack["runner"].registry and stack["runner"].registry or None
        self.runner = stack["runner"]
        self.browser_env = None
        self.sampler = None
        self.plan = stack["plan"]
        self._summary = None
        from daedalus.obs.drilldown import Drilldown
        from daedalus.understand.registry import default_registry
        self.parser_registry = default_registry()
        self.drilldown = Drilldown(self.db, ledger=self.ledger)

    # ── CLI 用到的接口 ───────────────────────────────────────────
    def shutdown(self) -> dict:
        rep = self._stack["writer"].stop(drain=True)
        try:
            self._stack["db"].close()
        except Exception:
            pass
        return {"ok": True, "writer": rep}

    def run_targets(self, urls, *, workers=4, budget=None, goal="", **kw):
        from daedalus.core.task import ResourceRequest, Task
        summary = self._stack and None
        # 直接用 runner + frontier 跑（与 EngineApp.run_targets 同形状）
        from daedalus.core.app import RunSummary
        s = RunSummary()
        import time as _t
        import threading as _th
        t0 = _t.monotonic()
        for u in urls:
            s.notes.append(f"入队 {u}")
            self.frontier.enqueue(Task.acquire(str(u), goal=goal,
                                               resources=ResourceRequest(network=1),
                                               budget=budget))
        lock = _th.Lock()

        def worker(wid):
            while True:
                batch = self.frontier.claim_batch(1, wid)
                if not batch:
                    return
                for task in batch:
                    t1 = _t.monotonic()
                    rep = self.runner.run_one(task)
                    with lock:
                        s.add(str(rep.final_state), _t.monotonic() - t1, rep.to_dict())

        threads = [_th.Thread(target=worker, args=(f"w{i}",), name=f"dae-w{i}") for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        s.seconds = _t.monotonic() - t0
        self._summary = s
        return s

    def metrics(self) -> dict:
        from daedalus.obs.alerts import Thresholds, evaluate
        from daedalus.obs.metrics import METRICS
        summary = METRICS.summary()
        alerts = evaluate(summary, thresholds=Thresholds(), queue_limits=self.plan.queues(),
                          queue_depths={})
        return {"summary": summary,
                "alerts": [{"key": a.key, "level": a.level, "message": a.message} for a in alerts],
                "fetcher": self.fetcher.stats(), "frontier": self.frontier.stats(),
                "writer": self._stack["writer"].stats(), "ledger": self.ledger.summary()}

    def doctor(self) -> dict:
        from daedalus import about
        from daedalus.exec.subprocess import readiness
        return {"about": about(), "paths": {"data_root": str(self.data_root), "db": str(self.db.path)},
                "disk": {"free_pct": 42.0}, "db": self.db.stats(), "plan": self.plan.to_dict(),
                "memory_arithmetic": self.plan.memory_arithmetic(), "sanitization": {},
                "logging": {"file": None}, "tools": readiness(("ffmpeg",)),
                "browser": {"available": False, "reason": "测试未启用"},
                "secrets": {"dpapi": True}, "net": {"dns_cache": {}},
                "metrics": {}, "problems": [], "warnings": [], "ok": True}

    def reparse(self, *, since=None, url_like=None, limit=None, dry_run=False) -> dict:
        from daedalus.capture.index import ArtifactIndex
        from daedalus.capture.replay import Reparser
        idx = ArtifactIndex(self.db)
        if dry_run:
            rows = (idx.url_like(url_like, limit=limit or 500) if url_like
                    else idx.since(since if since is not None else 0.0, limit=limit))
            return {"dry_run": True, "scanned": len(rows),
                    "network_calls": int(self.fetcher.stats().get("calls", 0)),
                    "parsers": [p["name"] for p in self.parser_registry.summary()]}
        rp = Reparser(self._stack["store"], self.parser_registry, idx, ledger=self.ledger)
        delivered: list[dict] = []

        def deliver(record, artifact):
            delivered.append({"url": record.get("url"), "hash": record.get("content_hash")})
            self.writer.run_now(lambda conn: conn.execute(
                "INSERT OR REPLACE INTO pages (url_hash, url, fetched_at, status, content_hash,"
                " simhash, duplicate_of, size, source_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(record.get("content_hash") or ""), str(record.get("url") or ""),
                 __import__("time").time(), 200, record.get("content_hash"),
                 record.get("simhash"), record.get("duplicate_of"),
                 len(record.get("text") or ""), getattr(artifact, "sha256", ""))),
                label="s9.reparse")

        stats = rp.run(since=since, url_like=url_like, limit=limit, deliver=deliver)
        stats["delivered"] = len(delivered)
        stats["network_calls"] = int(self.fetcher.stats().get("calls", 0))
        stats["parsers"] = [p["name"] for p in self.parser_registry.summary()]
        return stats

    def export_jsonl(self, limit: int = 10000) -> str:
        return self.drilldown.export_jsonl(limit=limit)


def with_app(fn, *, shell_urls=(), name="s9", root=None):
    """装好注入 app 跑一段。`root` 可显式指定（**跨命令共用一个数据根**时要用它，
    例如"先 collect 再 reparse"——两次调用必须是同一个数据根，否则重扫的是空库）。"""
    from daedalus import cli
    r = pathlib.Path(root) if root is not None else \
        (_TMP / f"{name}_{abs(hash(fn.__name__)) % 10000}")
    app = _AppStack(r, shell_urls=shell_urls)
    cli.set_app_factory(lambda args: app)
    try:
        return fn(app)
    finally:
        cli.set_app_factory(None)
        app.shutdown()


# ══════════════════════════════════════════════════════════════════
@case("A1 version：元信息齐全（名字/版本/语言）")
def t_version():
    code, out, err = run_cli(["--json", "version"])
    assert code == 0, (code, err)
    d = json.loads(out)
    assert d["name"] == "Daedalus" and d["name_zh"] == "代达罗斯" and d["name_ja"] == "ダイダロス"
    assert d["version"] and d["locales"] == ["zh-CN", "ja-JP", "en-US"], d
    return ok(f"{d['name']} {d['version']} / {len(d['locales'])} 种语言")


@case("A2 config：显示的是**引擎读到的**完整配置（六个段）")
def t_config():
    code, out, err = run_cli(["--json", "config"])
    assert code == 0, (code, err)
    d = json.loads(out)
    for seg in ("fetcher", "sanitization", "logging", "paths", "alerts", "limits"):
        assert seg in d["config"], f"缺段 {seg}：{list(d['config'])}"
    assert d["config"]["fetcher"]["per_domain_concurrency"] <= 8, d["config"]["fetcher"]
    return ok(f"段：{list(d['config'])}")


@case("A3 config --toml / --write-example：可写出示例配置")
def t_config_write():
    p = _TMP / "example.toml"
    code, out, err = run_cli(["config", "--write-example", str(p)])
    assert code == 0 and p.exists(), (code, err)
    text = p.read_text(encoding="utf-8")
    assert "[fetcher]" in text and "[limits]" in text and "[alerts]" in text, text[:200]
    code2, out2, err2 = run_cli(["config", "--toml"])
    assert code2 == 0 and "[fetcher]" in out2, (code2, err2)
    return ok(f"写出 {p.name}（{len(text)} 字节）；--toml 可用")


@case("B1 doctor：结构完整、结论明确（注入 app，不联网）")
def t_doctor():
    def body(app):
        code, out, err = run_cli(["--json", "doctor"])
        assert code == 0, (code, err)
        d = json.loads(out)
        for key in ("about", "paths", "db", "plan", "memory_arithmetic", "tools", "browser",
                    "problems", "warnings", "ok"):
            assert key in d, f"doctor 缺 {key}"
        assert "≤ 4096 MB" in d["memory_arithmetic"], d["memory_arithmetic"]
        return ok(f"ok={d['ok']}；warnings={len(d['warnings'])}；内存自证有")
    return with_app(body, name="b1")


@case("B2 doctor：问题与警告分类正确（问题 → 非 0 退出码）")
def t_doctor_problems():
    from daedalus import cli

    class Bad(_AppStack):
        def doctor(self):
            d = super().doctor()
            d["problems"] = ["本机不支持 DPAPI"]
            d["warnings"] = ["缺 ffmpeg"]
            d["ok"] = False
            return d

    root = _TMP / "b2"
    app = Bad(root)
    cli.set_app_factory(lambda args: app)
    try:
        code, out, err = run_cli(["doctor"])
        assert code == 1, f"有问题时应返回 1，实际 {code}"
        # 人类可读输出用 ASCII 标记（`[x]`/`[!]`）：GBK 控制台也能显示，见 F3 用例
        assert "[x]" in err and "[!]" in err, err[-300:]
        return ok("有问题 → 退出码 1，且问题(✗)/警告(⚠)分开显示")
    finally:
        cli.set_app_factory(None)
        app.shutdown()


@case("C1 collect：真跑闭环 → 原始层 + 派生行 + 台账 + JSON 报告")
def t_collect():
    def body(app):
        code, out, err = run_cli(["--json", "collect", "https://bench.local/a",
                                  "https://bench.local/b", "-w", "2"])
        assert code == 0, (code, err[-800:])
        d = json.loads(out)
        assert d["summary"]["tasks"] == 2, d["summary"]
        assert d["summary"]["states"] == {"done": 2}, d["summary"]
        assert d["ledger"]["new"] == 2, d["ledger"]
        # CLI 已关闭引擎 → 用独立只读连接查库
        raw = count_raw(app.db.path)
        pages = count_pages(app.db.path)
        assert raw == 2 and pages == 2, (raw, pages)
        return ok(f"2 任务全 done；原始层 {raw}；派生 {pages}；台账 {d['ledger']}")
    return with_app(body, name="c1")


@case("C2 退出码语义：用法错 2 / 被拦 3（真闸本地判定，零出网）")
def t_exit_codes():
    # ① 用法错：没有目标 → 2
    code, out, err = run_cli(["collect"])
    assert code == 2, f"没目标应返回 2，实际 {code}"
    # ② 被拦：私网地址 → SSRF 闸拦下 → policy_denied → 3
    #    **用真 app**（真闸）：闸在出网前就判掉，所以这条完全离线（不起任何 socket）
    root = _TMP / "c2_real"
    code2, out2, err2 = run_cli(["--json", "--data-root", str(root),
                                 "collect", "http://127.0.0.1:9/x"])
    d = json.loads(out2) if out2.strip().startswith("{") else {}
    states = (d.get("summary") or {}).get("states") or {}
    assert states.get("policy_denied", 0) >= 1, f"私网目标没被拦：{states}（code={code2}）"
    assert code2 == 3, f"全被拦应返回 3，实际 {code2}（states={states}）"
    return ok(f"用法错 → 2；私网目标 → {code2}（states={states}，本地判定零出网）")


@case("C3 dry-run 自证：不建任务、不出网（network_calls 实测 0）")
def t_dry_run():
    def body(app):
        before = app.fetcher.stats()["calls"]          # CLI 之前读（之后写线程已关）
        db_path = app.db.path
        code, out, err = run_cli(["--json", "collect", "--dry-run",
                                  "https://bench.local/a", "https://bench.local/b"])
        assert code == 0, (code, err)
        d = json.loads(out)
        assert d["dry_run"] is True and d["network_calls"] == 0, d
        assert d["targets"] == 2 and len(d["plan"]) == 2, d
        after = app.fetcher.stats()["calls"]           # fetcher 是注入对象，仍可读
        assert after == before, f"dry-run 竟然出网了：{before} → {after}"
        # CLI 已关闭引擎 → 用独立只读连接查库：dry-run 不该留下任何任务
        tasks = count_tasks(db_path)
        assert tasks == 0, f"dry-run 竟然建了 {tasks} 个任务"
        return ok(f"dry_run=true / network_calls=0（实测 {before}→{after}）/ 库里零任务")
    return with_app(body, name="c3")


@case("D1 reparse：用当前解析器离线重扫历史原始数据（网络增量 0）")
def t_reparse():
    shared = _TMP / "d1_shared"          # **两次命令共用同一个数据根**（先采集、再重扫）

    def body(app):
        run_cli(["--json", "collect", "https://bench.local/r1", "https://bench.local/r2"])
        return None
    with_app(body, name="d1_seed", root=shared)

    def body2(app):
        idx = app.index if app.index is not None else None
        from daedalus.capture.index import ArtifactIndex
        idx = ArtifactIndex(app.db)
        n = idx.count()
        assert n >= 2, f"原始层没有可重扫的数据：{n}"
        before = app.fetcher.stats()["calls"]
        code, out, err = run_cli(["--json", "reparse", "--limit", "10"])
        assert code == 0, (code, err[-600:])
        d = json.loads(out)
        assert d["scanned"] >= 2, d
        assert d["network_calls"] == before, f"重放联网了：{before} → {d['network_calls']}"
        assert d["delivered"] >= 2, f"重放没落库：{d}"
        code2, out2, _ = run_cli(["--json", "reparse", "--limit", "10", "--dry-run"])
        d2 = json.loads(out2)
        assert d2["dry_run"] is True and d2["scanned"] >= 2, d2
        return ok(f"扫描 {d['scanned']} 份 → 落库 {d['delivered']} 条；网络增量 0；"
                  f"dry-run 只数不写（{d2['scanned']} 份）")
    return with_app(body2, name="d1", root=shared)


@case("E1 export：JSONL 含台账与任务，可写文件")
def t_export():
    def body(app):
        run_cli(["--json", "collect", "https://bench.local/e1"])
        code, out, err = run_cli(["export"])
        assert code == 0, (code, err)
        lines = [json.loads(x) for x in out.strip().splitlines() if x.strip()]
        kinds = {x.get("kind") for x in lines}
        assert "ledger" in kinds and "task" in kinds, kinds
        p = _TMP / "export.jsonl"
        code2, _, err2 = run_cli(["export", "--out", str(p)])
        assert code2 == 0 and p.exists(), (code2, err2)
        return ok(f"{len(lines)} 行；类型 {sorted(kinds)}；文件导出可用")
    return with_app(body, name="e1")


@case("E2 metrics / alerts：退出码反映最严重级别（两次命令 = 两个引擎生命周期）")
def t_metrics_alerts():
    def body(app):
        code, out, err = run_cli(["--json", "metrics"])
        assert code == 0, (code, err)
        d = json.loads(out)
        assert "summary" in d and "alerts" in d, d.keys()
        return ok(f"metrics 退出码 0（{len(d['summary'])} 项指标、{len(d['alerts'])} 条告警）")

    def body2(app):
        code, out, err = run_cli(["--json", "alerts"])
        assert code in (0, 1), (code, err)
        d = json.loads(out)
        assert d["level"] in ("ok", "warn", "critical", "unknown"), d
        assert isinstance(d["alerts"], list), d
        return ok(f"alerts 退出码 {code} / level={d['level']}（{len(d['alerts'])} 条）")

    first = with_app(body, name="e2a")
    second = with_app(body2, name="e2b")
    return first + "；" + second


@case("F1 --json 时 stdout 只有 JSON（结构化日志走 stderr）")
def t_json_purity():
    def body(app):
        code, out, err = run_cli(["--json", "doctor"])
        assert code == 0, code
        json.loads(out)                        # 能被解析 → stdout 是纯 JSON
        assert out.strip().startswith("{") and out.strip().endswith("}"), out[:120]
        return ok(f"stdout {len(out)} 字节纯 JSON；stderr {len(err)} 字节（日志）")
    return with_app(body, name="f1")


@case("F2 未预期异常 → 退出码 4（**绝不用 0 掩盖**）")
def t_internal_error():
    from daedalus import cli

    class Boom(_AppStack):
        def metrics(self):
            raise RuntimeError("内部炸了")

    root = _TMP / "f2"
    app = Boom(root)
    cli.set_app_factory(lambda args: app)
    try:
        code, out, err = run_cli(["metrics"])
        assert code == 4, f"内部错误应返回 4，实际 {code}"
        assert "内部错误" in err or "RuntimeError" in err, err[-200:]
        return ok("异常 → 退出码 4 且错误信息可见")
    finally:
        cli.set_app_factory(None)
        app.shutdown()


@case("F3 GBK 控制台不崩（打包态真实场景：cmd 默认代码页不是 UTF-8）")
def t_gbk_console():
    """真实事故（S10b 打包冒烟抓到）：`doctor` 的人类可读输出里有 `⚠`/`✗`，
    在 GBK 代码页的 cmd 里 `UnicodeEncodeError` 直接把 CLI 打崩；而在 Git Bash（UTF-8）
    里跑完全正常——所以之前的 CLI 用例全绿也没发现。
    修法：① 输出流设 `errors="replace"`；② 人类可读输出改用 ASCII 标记。"""
    import io
    from daedalus import cli
    buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict", newline="")
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, buf
    try:
        code = cli.main(["alerts"])                 # 这条命令的输出里含符号与中文
    finally:
        try:
            buf.flush()
        finally:
            sys.stdout, sys.stderr = real_out, real_err
    assert code in (0, 1), f"GBK 控制台下退出码异常：{code}"
    src = (ROOT / "src" / "daedalus" / "cli.py").read_text(encoding="utf-8")
    assert "_make_output_safe" in src and 'reconfigure(errors="replace")' in src, "缺编码安全处理"
    return ok(f"GBK 严格模式下退出码 {code}（不再抛 UnicodeEncodeError）")


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S9 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

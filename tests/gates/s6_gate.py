# -*- coding: utf-8 -*-
"""S6 门禁：路由收口 + 端到端闭环（Task → 环境 → 证据 → Router → Frontier → 台账）

覆盖（把"分层各自跑通"验证成"串起来也对"）：
  A 直连成功：原始层落盘 + 派生行 + 打卡（CAS + 产物同一事务）+ 台账 new + 证据链
  A2 解析失败：原始留下、派生不写、打卡照旧成功（延期解释）
  B 内容空壳：记 browser 候选并终止（V0.1 未启用浏览器，**不偷偷起环境**）
  C 大对象/制品：转制品环境（另一套资源模型 + 契约）
  D 被 robots 拦下：policy_denied + **零出网** + 不重试
  E 限流：独立计数（attempts 不变）→ retry
  F 永久失败（404）：dead
  G 有界：转移/预算耗尽 → dead（防"坏目标黑洞"）
  H 打卡失守：lease_lost → **一行都不写**
  I 发现链接线：成功后自动产生子任务入队（新事实 → 新任务）

跑法（离线；不联网）：
    python tests/gates/s6_gate.py        # 退出码 0 = 全通过
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="daedalus_s6_"))
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


class ScriptedFetcher:
    """按顺序吐响应的假咽喉（记录每次调用）。

    **桩必须与真接口行为一致**（纪律：桩签名与真接口一致，否则门禁是假绿）：
    `net/fetch.py` 的 `Fetcher.open()` 自己会先过 robots——所以这里的 `deny=True`
    也必须在**记录调用之前**抛 `RobotsDenied`，否则"robots 拒绝后零出网"这条断言
    测的就不是产品行为。
    """

    def __init__(self, script: list, deny: bool = False):
        self.script = list(script)
        self.calls: list[tuple] = []
        self._deny = deny

    def open(self, url, method="GET", headers=None, timeout=None):
        if self._deny:
            from daedalus.net.fetch import RobotsDenied
            raise RobotsDenied(f"robots.txt 不允许：{url}")
        self.calls.append((url, method, dict(headers or {})))
        if not self.script:
            return FakeResp(200, {}, b"default")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def is_allowed(self, url):
        return (not self._deny, "robots.txt 不允许" if self._deny else "ok")

    def stats(self):
        return {"calls": len(self.calls)}


HTML_OK = ("<html><head><title>闭环测试页</title></head><body>"
           + "<p>" + ("这是用于端到端闭环验证的正文内容。" * 40) + "</p>"
           + '<a href="/child/1">子页</a></body></html>').encode()
SHELL_HTML = b"<html><head><title>Please enable JavaScript</title></head><body>login</body></html>"
# 没有任何解析器认得的类型 + 全是控制字节（可打印比例 0）→ 探测兜底 unknown → 解析失败。
# 注意别用"看起来像文本"的字节：那会被解析器接住（诚实降级），不是"解析失败"这条路径。
UNPARSABLE = bytes(range(0x00, 0x09)) * 512
UNPARSABLE_CT = {"Content-Type": "application/x-msdownload"}


# ── 计数：**每个表一个显式函数**（本机安全策略要求 execute() 的 SQL 是字面量，
#    所以不能写 `count(db, table)` 那种拼表名的通用函数）────────────────────
def count_raw(db) -> int:
    conn = db.connect(readonly=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM raw_artifacts").fetchone()[0])
    finally:
        conn.close()


def count_pages(db) -> int:
    conn = db.connect(readonly=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    finally:
        conn.close()


def count_evidence(db) -> int:
    conn = db.connect(readonly=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0])
    finally:
        conn.close()


def build_stack(name: str, script: list, *, enable_browser: bool = False,
                max_transitions: int = 8, deny: bool = False):
    """搭一个完整的闭环栈：db/writer/frontier/router/net/media/store/ledger/discovery/runner"""
    from daedalus.capture.discovery import Discovery
    from daedalus.capture.rawstore import RawStore
    from daedalus.core.registry import ResourceRegistry
    from daedalus.core.router import Environment, Router
    from daedalus.core.runner import TaskRunner
    from daedalus.core.task import Task
    from daedalus.env.media import MediaEnvironment
    from daedalus.env.net import NetEnvironment
    from daedalus.frontier.frontier import Frontier
    from daedalus.store.db import Database
    from daedalus.store.deadletter import DeadLetter
    from daedalus.store.writer import SingleWriter
    from daedalus.understand.ledger import ChangeLedger

    db = Database(_TMP / f"{name}.db")
    dl = DeadLetter(path=_TMP / f"{name}.dead.jsonl", db=db)
    writer = SingleWriter(db, dead_letter=dl, batch_rows=200, flush_interval=0.05).start()
    frontier = Frontier(writer, max_queue=1000, lease_timeout=60)
    fetcher = ScriptedFetcher(script, deny=deny)
    net_env = NetEnvironment(fetcher, cache=None, cookies=None, retries=1,
                             sleep=lambda s: None)
    store = RawStore(_TMP / f"{name}_data", db, writer)
    media_env = MediaEnvironment(fetcher, workdir=_TMP / f"{name}_media")
    reg = ResourceRegistry()
    if enable_browser:
        reg.register("browser", 1)
    envs = [Environment.NETWORK, Environment.ARTIFACT] + \
        ([Environment.BROWSER] if enable_browser else [])
    router = Router(enabled_environments=tuple(envs), max_transitions=max_transitions)
    ledger = ChangeLedger(name)
    discovery = Discovery(base_hosts=("example.com",), fetcher=fetcher)
    runner = TaskRunner(frontier=frontier, router=router, net_env=net_env, media_env=media_env,
                        store=store, registry=reg, ledger=ledger, discovery=discovery)
    return dict(db=db, writer=writer, frontier=frontier, fetcher=fetcher, net_env=net_env,
                store=store, media_env=media_env, registry=reg, router=router, ledger=ledger,
                discovery=discovery, runner=runner, Task=Task)


def task_row(db, task_id):
    conn = db.connect(readonly=True)
    try:
        return conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()


def run_case(s, url="https://example.com/p1", **task_kw):
    t = s["Task"].acquire(url, **task_kw)
    done, why = s["frontier"].enqueue(t)
    claimed = s["frontier"].claim_batch(1, "w1")
    assert claimed, f"没领到任务（{why}）"
    task = claimed[0]
    rep = s["runner"].run_one(task)
    return rep, task


# ══════════════════════════════════════════════════════════════════
@case("A1 闭环：直连成功 → 原始层 + 派生行 + 打卡 + 台账 new")
def t_e2e_direct_ok():
    from daedalus.core.task import Task  # noqa: F401
    s = build_stack("a1", [FakeResp(200, {"Content-Type": "text/html; charset=utf-8"}, HTML_OK)])
    try:
        rep, task = run_case(s)
        assert rep.final_state == "done", (rep.final_state, rep.reason)
        assert count_raw(s["db"]) == 1, "原始层没落盘"
        assert count_pages(s["db"]) == 1, "派生行没落库"
        assert count_evidence(s["db"]) >= 2, "证据链太短"
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "done" and row["bytes_done"] == len(HTML_OK), dict(row)
        summ = s["ledger"].summary()["counts"]
        assert summ["new"] == 1, summ
        assert rep.derived and rep.derived.get("content_hash"), "没有派生记录"
        return ok(f"done；原始 1 条 + 派生 1 条；台账 {summ}")
    finally:
        s["writer"].stop()


@case("A2 闭环：解析失败仍保留原始（延期解释），打卡照旧成功")
def t_e2e_parser_failed():
    s = build_stack("a2", [FakeResp(200, UNPARSABLE_CT, UNPARSABLE)])
    try:
        rep, task = run_case(s)
        assert rep.final_state == "done", (rep.final_state, rep.reason, rep.to_dict())
        assert count_raw(s["db"]) == 1, "原始层必须留下"
        assert count_pages(s["db"]) == 0, "解析失败不该写派生行"
        conn = s["db"].connect(readonly=True)
        try:
            reasons = [r["reason"] or "" for r in
                       conn.execute("SELECT reason FROM task_evidence").fetchall()]
        finally:
            conn.close()
        assert any(("原始已存" in r) or ("解析失败" in r) or ("拒收" in r) for r in reasons), reasons
        return ok("原始留下、派生不写、打卡成功（延期解释）")
    finally:
        s["writer"].stop()


@case("B 闭环：内容空壳 → 记 browser 候选并终止（不偷偷起环境）")
def t_e2e_shell_candidate():
    s = build_stack("b", [FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML)])
    try:
        rep, task = run_case(s)
        conn = s["db"].connect(readonly=True)
        try:
            ev = conn.execute("SELECT signal, decision, reason FROM task_evidence "
                              "ORDER BY id").fetchall()
        finally:
            conn.close()
        pairs = [(e["signal"], e["decision"]) for e in ev]
        assert ("empty_content", "quality_rejected") in pairs, pairs
        assert rep.final_state in ("dead",), (rep.final_state, rep.reason)
        return ok(f"dead；证据末两条 {pairs[-2:]}（质量拒收 → 走不到解析）")
    finally:
        s["writer"].stop()


@case("C 闭环：制品（zip 大对象）→ 转制品环境（另一套资源模型 + 契约）")
def t_e2e_artifact():
    import io
    import os as _os
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # 反复模式压得太好，产物可能小于契约下限（1KB）→ 用不可压内容（真实一点的制品）
        zf.writestr("data.csv", "".join(f"{i},{_os.urandom(8).hex()}\n" for i in range(4000)))
    blob = buf.getvalue()
    assert len(blob) > 1024, len(blob)
    # 两条响应：① 直连（只看事实 → 判为大对象）② 制品环境取件（同一份字节）
    s = build_stack("c", [FakeResp(200, {"Content-Type": "application/octet-stream"}, blob),
                          FakeResp(200, {"Content-Type": "application/octet-stream"}, blob)])
    try:
        rep, task = run_case(s)
        assert "artifact" in " ".join(rep.steps), rep.to_dict()
        assert rep.final_state == "done", (rep.final_state, rep.reason, rep.to_dict())
        assert rep.artifacts, rep.to_dict()
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "done", dict(row)
        return ok(f"走制品环境：steps={rep.steps}；产物 {len(rep.artifacts)} 件（已验契约）")
    finally:
        s["writer"].stop()


@case("D 闭环：被 robots 拦下 → policy_denied + 零出网 + 不重试")
def t_e2e_policy_denied():
    s = build_stack("d", [FakeResp(200, {}, b"x")], deny=True)
    try:
        rep, task = run_case(s)
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "policy_denied", dict(row)
        assert row["attempts"] == 0, f"策略拒绝不该计重试：{row['attempts']}"
        assert s["ledger"].summary()["counts"]["policy_denied"] == 1
        assert len(s["fetcher"].calls) == 0, "robots 拒绝后仍然出网了"
        return ok("policy_denied + 零出网 + 台账可见（降速→停止→报告）")
    finally:
        s["writer"].stop()


@case("E 闭环：连续限流 → 独立计数（attempts 不变）→ 转死信")
def t_e2e_throttled():
    from daedalus.core.budget import Budget
    from daedalus.net.fetch import Throttled
    # 两次 429 + max_throttles=2：第 2 次就必须收口（限流走独立计数，**不占 attempts**）
    s = build_stack("e", [Throttled(429, 2.0, "https://example.com/p1"),
                          Throttled(429, 2.0, "https://example.com/p1")])
    try:
        rep, task = run_case(s, budget=Budget(max_throttles=2))
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "dead", dict(row)
        assert row["attempts"] == 0, f"限流被记成了重试：attempts={row['attempts']}"
        assert row["throttles"] >= 1, dict(row)
        assert "限流" in (rep.reason or ""), rep.to_dict()
        return ok(f"dead；attempts=0 / throttles={row['throttles']}（两本账分开）")
    finally:
        s["writer"].stop()


@case("F 闭环：永久失败（404）→ dead")
def t_e2e_permanent():
    s = build_stack("f", [FakeResp(404, {}, b"not found")])
    try:
        rep, task = run_case(s)
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "dead", dict(row)
        conn = s["db"].connect(readonly=True)
        try:
            sigs = [r["signal"] for r in conn.execute("SELECT signal FROM task_evidence")]
        finally:
            conn.close()
        assert "permanent_failure" in sigs, sigs
        return ok("404 → dead（不反复重试）")
    finally:
        s["writer"].stop()


@case("G 闭环：转移/预算耗尽 → dead（防'坏目标黑洞'）")
def t_e2e_bounded():
    from daedalus.core.budget import Budget
    s = build_stack("g", [FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML),
                          FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML),
                          FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML)],
                    enable_browser=True, max_transitions=3)
    try:
        # V0.1 里浏览器阶段只有"接缝"没有实现：即使路由把它列进候选，也必须是**收得住的失败**
        # （`resource_denied` → dead），而不是无限升级。
        rep, task = run_case(s, budget=Budget(max_attempts=1, max_transitions=2))
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "dead", dict(row)
        assert int(row["transitions"]) <= 3, f"转移次数超界：{row['transitions']}"
        assert len(s["fetcher"].calls) <= 2, f"出网次数超界：{len(s['fetcher'].calls)}"
        return ok(f"dead；transitions={row['transitions']}（有界，不会无限升级）")
    finally:
        s["writer"].stop()


@case("H 闭环：打卡失守 → lease_lost，派生行一行不写（原始层内容寻址幂等）")
def t_e2e_lease_lost():
    s = build_stack("h", [FakeResp(200, {"Content-Type": "text/html"}, HTML_OK)])
    try:
        t = s["Task"].acquire("https://example.com/p1")
        s["frontier"].enqueue(t)
        task = s["frontier"].claim_batch(1, "w1")[0]
        task.leased_at = (task.leased_at or 0) - 999          # 伪造过期令牌
        rep = s["runner"].run_one(task)
        assert rep.lease_lost and rep.final_state == "lease_lost", rep.to_dict()
        # CAS 保护的是"任务完成"这个**状态**与**派生行**：失守时它们一行都不许动。
        assert count_pages(s["db"]) == 0, "失守却写了派生行（CAS 语义破了）"
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "leased" and row["bytes_done"] == 0, dict(row)
        # 原始层是例外，且是**刻意**的：Capture First（进程随时会死，事实先落盘）+ 内容寻址幂等
        # （同 URL 同字节 → 同 sha256 → INSERT OR IGNORE；不同字节 → 两行各自是当时的真事实）。
        # 真正要防的是"把半截字节当完整产物"，那条由 too_big 标记与产物契约兜住。
        assert count_raw(s["db"]) <= 1, f"原始层出现重复行：{count_raw(s['db'])}"
        return ok(f"lease_lost；派生行 0（原始层 {count_raw(s['db'])} 条，内容寻址幂等）")
    finally:
        s["writer"].stop()


@case("I 发现链接线：成功后自动产生子任务入队（新事实 → 新任务）")
def t_e2e_children():
    s = build_stack("i", [FakeResp(200, {"Content-Type": "text/html"}, HTML_OK)])
    try:
        rep, task = run_case(s)
        assert rep.final_state == "done", rep.to_dict()
        assert rep.children >= 1, f"没有产生子任务：{rep.to_dict()}"
        conn = s["db"].connect(readonly=True)
        try:
            kids = conn.execute("SELECT target, parent_id, discovery_path FROM tasks "
                               "WHERE parent_id = ?", (task.task_id,)).fetchall()
        finally:
            conn.close()
        assert kids and "example.com/child/1" in kids[0]["target"], [dict(k) for k in kids]
        assert kids[0]["discovery_path"] == "html_link", dict(kids[0])
        return ok(f"子任务 {len(kids)} 条已入队（血缘 parent_id 保留）")
    finally:
        s["writer"].stop()


@case("J 可解释：每一步都留下「看到了什么 → 决定了什么 → 为什么」")
def t_e2e_explainable():
    s = build_stack("j", [FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML)])
    try:
        rep, task = run_case(s)
        conn = s["db"].connect(readonly=True)
        try:
            rows = conn.execute("SELECT signal, decision, reason, stage, facts_json "
                                "FROM task_evidence ORDER BY id").fetchall()
        finally:
            conn.close()
        assert rows, "一条证据都没留（无法解释、无法回放）"
        for r in rows:
            d = dict(r)
            assert d["signal"] and d["decision"], f"证据缺信号或决定：{d}"
            assert (d["reason"] or "").strip(), f"证据缺可读原因：{d}"
            assert d["stage"], f"证据缺阶段：{d}"
            assert d["facts_json"] and d["facts_json"] != "{}", f"证据缺事实：{d}"
        signals = [r["signal"] for r in rows]
        # 「响应事实」必须在链上（否则事后说不清"凭什么这么判"）
        assert "ok" in signals, f"响应事实丢了：{signals}"
        assert "empty_content" in signals, signals
        assert signals[-1] == "empty_content", signals
        return ok(f"{len(rows)} 条证据，字段齐全；信号序列 {signals}")
    finally:
        s["writer"].stop()


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    fails = skips = 0
    print(f"S6 门禁 · 数据根={_TMP}\n" + "─" * 68)
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

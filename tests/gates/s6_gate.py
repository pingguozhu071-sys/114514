# -*- coding: utf-8 -*-
"""S6 门禁：路由收口 + 端到端闭环（Task → 环境 → 证据 → Router → Frontier → 台账）

覆盖（把「分层各自跑通」验证成「串起来也对」）：
  A 直连成功：原始层落盘 + 派生行 + 打卡（CAS + 产物同一事务）+ 台账 new + 证据链
  A2 解析失败：原始留下、派生不写、打卡照旧成功（延期解释）
  B 内容空壳：记 browser 候选并终止（V0.1 未启用浏览器，**不偷偷起环境**）
  C 大对象/制品：转制品环境（另一套资源模型 + 契约）
  D 被 robots 拦下：policy_denied + **零出网** + 不重试
  E 限流：独立计数（attempts 不变）→ retry
  F 永久失败（404）：dead
  G 有界：转移/预算耗尽 → dead（防「坏目标黑洞」）
  H 打卡失守：lease_lost → **一行都不写**
  I 发现链接线：成功后自动产生子任务入队（新事实 → 新任务）
  K 路由收口·证据：**每一处 Decision 都带证据**（源码级 AST 扫描 + 运行期断言，两条都要）
  L 路由收口·表驱动：注入一行 `RULES` 即可改行为（`on_evidence` 本体不再是 if/else 链）
  M 路由收口·有界：转移上限 / 预算耗尽（且优先于上限）—— 语义不许退化
  N 路由收口·两本账：限流 ≠ 重试（各自独立收口）+ 无匹配规则 → 明确失败（不猜）

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
    也必须在**记录调用之前**抛 `RobotsDenied`，否则「robots 拒绝后零出网」这条断言
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

    def is_allowed(self, url, *, fetch=True):
        return (not self._deny, "robots.txt 不允许" if self._deny else "ok")

    def stats(self):
        return {"calls": len(self.calls)}


HTML_OK = ("<html><head><title>闭环测试页</title></head><body>"
           + "<p>" + ("这是用于端到端闭环验证的正文内容。" * 40) + "</p>"
           + '<a href="/child/1">子页</a></body></html>').encode()
SHELL_HTML = b"<html><head><title>Please enable JavaScript</title></head><body>login</body></html>"
# A2 的素材（**2026-09 换过一次**）：原来是「没有解析器认得的类型 + 全是控制字节」，判成
# `unknown`、解析必然失败。但注册表改成**扫目录**后 `artifact_meta` 自动注册，而它的 `accepts`
# 含 `unknown`（见 s3 门禁 C1 的同一处素材更换）——旧素材于是有了候选、解析**不再失败**，
# 结果 A2 测的变成「质量拒收」而不是「解析失败」这条分支。
# 所以换成**真没有解析器**的格式（SQLite 魔数，`detect` 按魔数判为 sqlite，无候选）：
# 只有它能保证「解析失败 → 原始留下 + 延期解释」这条分支可达。
UNPARSABLE = b"SQLite format 3\x00" + b"\x00\x01\x02\x03" * 64
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


@case("G 闭环：转移/预算耗尽 → dead（防「坏目标黑洞」）")
def t_e2e_bounded():
    from daedalus.core.budget import Budget
    s = build_stack("g", [FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML),
                          FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML),
                          FakeResp(200, {"Content-Type": "text/html"}, SHELL_HTML)],
                    enable_browser=True, max_transitions=3)
    try:
        # V0.1 里浏览器阶段只有「接缝」没有实现：即使路由把它列进候选，也必须是**收得住的失败**
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
        # CAS 保护的是「任务完成」这个**状态**与**派生行**：失守时它们一行都不许动。
        assert count_pages(s["db"]) == 0, "失守却写了派生行（CAS 语义破了）"
        row = task_row(s["db"], task.task_id)
        assert row["state"] == "leased" and row["bytes_done"] == 0, dict(row)
        # 原始层是例外，且是**刻意**的：Capture First（进程随时会死，事实先落盘）+ 内容寻址幂等
        # （同 URL 同字节 → 同 sha256 → INSERT OR IGNORE；不同字节 → 两行各自是当时的真事实）。
        # 真正要防的是「把半截字节当完整产物」，那条由 too_big 标记与产物契约兜住。
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
        # 「响应事实」必须在链上（否则事后说不清「凭什么这么判」）
        assert "ok" in signals, f"响应事实丢了：{signals}"
        assert "empty_content" in signals, signals
        assert signals[-1] == "empty_content", signals
        return ok(f"{len(rows)} 条证据，字段齐全；信号序列 {signals}")
    finally:
        s["writer"].stop()


# ══════════════════════════════════════════════════════════════════
# 路由收口（审计的三条尾巴）：**转移是表**、**每次转移都有证据**、**语义不退化**。
# 这一组用例全部离线（假任务 + 假证据，不联网、不入队）。
# ══════════════════════════════════════════════════════════════════
ROUTER_SRC = ROOT / "src" / "daedalus" / "core" / "router.py"


def fn_source(fname: str) -> str:
    """按 AST 取 router.py 里某个函数的源码（用来做源码级断言，不数文本行）。"""
    import ast
    text = ROUTER_SRC.read_text(encoding="utf-8")
    lines = text.splitlines()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fname:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"router.py 里没有函数 {fname}")


def decision_sites() -> list[tuple[int, set]]:
    """源码里**每一处** `Decision(...)` 构造点（AST 扫；文本扫会被注释与字符串骗过）。"""
    import ast
    tree = ast.parse(ROUTER_SRC.read_text(encoding="utf-8"))
    out: list[tuple[int, set]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if getattr(fn, "attr", getattr(fn, "id", "")) == "Decision":
                out.append((node.lineno, {k.arg for k in node.keywords}))
    return out


def mk_task(stage: str | None = None, url: str = "https://example.com/x", **kw):
    """造一个假任务（不联网、不入队）：只想问路由器「这一步会怎么走」。"""
    from daedalus.core.task import Task
    t = Task.acquire(url, **kw)
    if stage:
        t.policy["stage"] = stage
    return t


def done_task():
    """一个已经收口的假任务（走 start() 的「不需要再路由」那条分支）。"""
    from daedalus.core.task import TaskState
    t = mk_task(None)
    t.state = TaskState.DONE
    return t


# 旧语义矩阵（6 阶段 × 15 信号）：`*` = 任何阶段。这张表是从**改造前的 if/else 链**
# 逐条抄下来的——表驱动改造如果偷换了任何一格，这里就会红。
_ANY = "*"
_MATRIX: dict[str, dict[str, str]] = {
    "classified": {"classify": "stage:direct", _ANY: "terminal:dead"},
    "ok": {"classify": "stage:direct", _ANY: "terminal:done"},
    "empty_content": {"classify": "stage:direct", "direct": "stage:browser",
                      "inspect": "stage:browser", _ANY: "terminal:dead"},
    "media_manifest": {"classify": "stage:direct", "direct": "stage:artifact",
                       _ANY: "terminal:dead"},
    "large_object": {"classify": "stage:direct", "direct": "stage:artifact",
                     _ANY: "terminal:dead"},
    "stream": {"classify": "stage:direct", _ANY: "terminal:dead"},
    "unknown_format": {"classify": "stage:direct", "direct": "stage:artifact",
                       _ANY: "terminal:dead"},
    "parser_failed": {_ANY: "terminal:done"},
    "transient_failure": {_ANY: "stage:direct"},
    "throttled": {_ANY: "stage:direct"},
    "permanent_failure": {_ANY: "terminal:dead"},
    "policy_denied": {_ANY: "terminal:policy_denied"},
    "budget_exceeded": {"classify": "stage:direct", _ANY: "terminal:dead"},
    "resource_denied": {_ANY: "terminal:dead"},
    "network_activity": {"classify": "stage:direct", "browser": "terminal:done",
                         "observe": "terminal:done", _ANY: "terminal:dead"},
}


@case("K1 源码级：router.py 每处 Decision(...) 都带 evidence（AST 扫描，含 start 两处）")
def t_decisions_have_evidence_source():
    sites = decision_sites()
    assert len(sites) >= 6, f"Decision 构造点只有 {len(sites)} 处（是不是被合并成一处了）"
    missing = [ln for ln, kw in sites if "evidence" not in kw]
    assert not missing, f"这些构造点没带证据：行 {missing}"
    # 审计点名的两处之一：start() 的两个分支（任务已终态 / 初始直连）
    head = fn_source("start")
    assert head.count("Decision(") == 2, f"start() 里的 Decision 不是 2 处：{head.count('Decision(')}"
    assert head.count("evidence=") == 2, "start() 里还有不带证据的 Decision"
    # 另一处（CLASSIFY→DIRECT）不直接建 Decision，而是走 _to_stage —— 运行期断言见 K2。
    return ok(f"{len(sites)} 处构造点全部带 evidence；start() 两处都在")


@case("K2 运行期：每条典型路径的 Decision 都带非空证据 + 理由（含 start 与 CLASSIFY→DIRECT）")
def t_decisions_have_evidence_runtime():
    from daedalus.core.budget import Budget
    from daedalus.core.evidence import from_decision, from_parse, from_response
    from daedalus.core.router import Environment, Router, Stage
    from daedalus.core.task import TaskState
    r = Router(enabled_environments=Environment.ALL)
    no_browser = Router(enabled_environments=(Environment.NETWORK, Environment.ARTIFACT))

    t_budget = mk_task(Stage.DIRECT, budget=Budget(max_attempts=1))
    t_budget.attempts = 1                       # 预算耗尽（attempts 这一维）
    t_cap = mk_task(Stage.DIRECT)
    t_cap.transitions = 99                      # 转移次数超上限

    paths = [
        ("start：新任务 → 直连", r.start(mk_task(None)), Stage.DIRECT),
        ("start：任务已在终态", r.start(done_task()), TaskState.DONE),
        ("CLASSIFY→DIRECT", r.on_evidence(mk_task(Stage.CLASSIFY),
                                          from_decision("classified", "?", "分类结论")), Stage.DIRECT),
        ("DIRECT+ok", r.on_evidence(mk_task(Stage.DIRECT),
                                    from_response(200, {"Content-Type": "text/html"})), TaskState.DONE),
        ("DIRECT+空壳（浏览器已启用）",
         r.on_evidence(mk_task(Stage.DIRECT), from_decision("empty_content", "?", "空壳")),
         Stage.BROWSER),
        ("DIRECT+空壳（浏览器未启用：只记候选）",
         no_browser.on_evidence(mk_task(Stage.DIRECT), from_decision("empty_content", "?", "空壳")),
         TaskState.DEAD),
        ("DIRECT+大对象", r.on_evidence(mk_task(Stage.DIRECT),
                                        from_response(200, {"Content-Type": "application/octet-stream"})),
         Stage.ARTIFACT),
        ("DIRECT+未知二进制",
         r.on_evidence(mk_task(Stage.DIRECT), from_decision("unknown_format", "?", "认不出")),
         Stage.ARTIFACT),
        ("DIRECT+流式（记候选）",
         r.on_evidence(mk_task(Stage.DIRECT), from_response(200, {"Content-Type": "text/event-stream"})),
         TaskState.DEAD),
        ("DIRECT+限流", r.on_evidence(mk_task(Stage.DIRECT), from_response(429)), Stage.DIRECT),
        ("DIRECT+瞬时失败", r.on_evidence(mk_task(Stage.DIRECT), from_response(500)), Stage.DIRECT),
        ("DIRECT+404", r.on_evidence(mk_task(Stage.DIRECT), from_response(404)), TaskState.DEAD),
        ("DIRECT+解析失败", r.on_evidence(mk_task(Stage.DIRECT), from_parse(False, 0.0, "坏字节")),
         TaskState.DONE),
        ("DIRECT+策略拒绝",
         r.on_evidence(mk_task(Stage.DIRECT), from_decision("policy_denied", "stop", "robots 不允许")),
         TaskState.POLICY_DENIED),
        ("INSPECT+ok", r.on_evidence(mk_task(Stage.INSPECT), from_decision("ok", "?", "通过")),
         TaskState.DONE),
        ("INSPECT+质量不足",
         r.on_evidence(mk_task(Stage.INSPECT), from_decision("empty_content", "?", "不足")), Stage.BROWSER),
        ("ARTIFACT+ok", r.on_evidence(mk_task(Stage.ARTIFACT), from_decision("ok", "?", "制品合格")),
         TaskState.DONE),
        ("BROWSER+ok", r.on_evidence(mk_task(Stage.BROWSER), from_decision("ok", "?", "拿到内容")),
         TaskState.DONE),
        ("BROWSER+网络活动",
         r.on_evidence(mk_task(Stage.BROWSER), from_decision("network_activity", "?", "观测到")),
         TaskState.DONE),
        ("OBSERVE+媒体清单（无匹配）",
         r.on_evidence(mk_task(Stage.OBSERVE), from_decision("media_manifest", "?", "清单")),
         TaskState.DEAD),
        ("预算耗尽", r.on_evidence(t_budget, from_decision("ok", "?", "随便")), TaskState.DEAD),
        ("转移上限", r.on_evidence(t_cap, from_decision("ok", "?", "随便")), TaskState.DEAD),
    ]
    for label, d, want in paths:
        assert d.evidence is not None, f"{label}：Decision 没带证据"
        assert (d.evidence.reason or "").strip(), f"{label}：证据没带可读原因"
        assert (d.reason or "").strip(), f"{label}：Decision 没带理由（硬约束）"
        assert d.evidence.signal and d.evidence.stage, f"{label}：证据缺信号或阶段：{d.evidence}"
        got = d.next_stage if d.next_stage else d.terminal
        assert got == want, f"{label}：{got} ≠ {want}（{d.reason}）"
    # 审计点名的两处证据必须**写清跨了哪一步**，不是一句空话
    assert r.start(mk_task(None)).evidence.decision == "go_direct", "start() 的证据没写清去向"
    assert r.start(done_task()).evidence.decision == "already_terminal"
    cls_d = r.on_evidence(mk_task(Stage.CLASSIFY), from_decision("classified", "?", "分类结论"))
    assert cls_d.evidence.decision == "escalate_to_direct", cls_d.evidence
    assert "直连" in cls_d.evidence.reason, cls_d.evidence.reason
    return ok(f"{len(paths)} 条路径证据+理由全非空；start 与 CLASSIFY→DIRECT 都带标签")


@case("K3 全矩阵：6 阶段 × 15 信号逐格对照旧语义（表必须与旧 if/else 等价）")
def t_route_matrix():
    from daedalus.core.evidence import SIGNALS, from_decision
    from daedalus.core.router import Environment, Router, Stage
    assert set(_MATRIX) == set(SIGNALS), \
        f"矩阵漏了信号 {sorted(set(SIGNALS) - set(_MATRIX))}／多了 {sorted(set(_MATRIX) - set(SIGNALS))}"
    r = Router(enabled_environments=Environment.ALL)
    cells = 0
    for sig, spec in _MATRIX.items():
        for stage in Stage.ALL:
            d = r.on_evidence(mk_task(stage), from_decision(sig, "?", f"矩阵：{stage}+{sig}"))
            got = f"stage:{d.next_stage}" if d.next_stage else f"terminal:{d.terminal}"
            want = spec.get(stage, spec.get(_ANY))
            assert got == want, f"{stage}+{sig} → {got}（应为 {want}；理由：{d.reason}）"
            assert d.evidence is not None and (d.evidence.reason or "").strip(), \
                f"{stage}+{sig}：没带证据或证据没理由"
            assert (d.reason or "").strip(), f"{stage}+{sig}：没带理由"
            cells += 1
    return ok(f"{cells} 格逐格与旧语义一致（每格都有证据与理由）")


def _injected_predicate(ctx) -> bool:
    """注入用例的谓词（门禁里的谓词同样是**具名模块级函数**，不是表里的 lambda 串）。"""
    return ctx.note(what="注入的标记规则")


@case("L1 表驱动：往 RULES 注入一行 → 行为改变（on_evidence 函数体一个字节没动）")
def t_table_driven_injection():
    import daedalus.core.router as R
    from daedalus.core.evidence import from_decision
    from daedalus.core.task import TaskState
    router = R.Router(enabled_environments=R.Environment.ALL)
    task = mk_task(R.Stage.DIRECT)
    ev = from_decision("classified", "?", "注入用例")
    # 注入前：direct + classified 没有任何规则 → 明确失败（不猜）
    before = router.on_evidence(task, ev)
    assert before.terminal == TaskState.DEAD and "不猜" in before.reason, before
    code_before = R.Router.on_evidence.__code__
    original = R.RULES
    injected = R.TransitionRule(name="injected_marker", stages=(R.Stage.DIRECT,),
                                signal="classified", when=_injected_predicate,
                                action=R.ACTION_TERMINAL, target=TaskState.DONE,
                                reason="{what} → 注入生效")
    try:
        R.RULES = (injected,) + original          # **只改数据**，不动函数
        after = router.on_evidence(task, ev)
        summ = R.rules_summary()
    finally:
        R.RULES = original
    assert after.terminal == TaskState.DONE, f"注入没生效：{after.describe()}"
    assert "注入生效" in after.reason and after.evidence is not None, after
    assert R.Router.on_evidence.__code__ is code_before, "注入动了函数体（那就不是表驱动了）"
    assert any(s["name"] == "injected_marker" for s in summ), "表读不出来（rules_summary 没反映注入）"
    assert len(summ) == len(original) + 1, f"注入后表长 {len(summ)}"
    assert not any(s["name"] == "injected_marker" for s in R.rules_summary()), "注入没还原"
    again = router.on_evidence(task, ev)          # 还原后行为回到「不猜」
    assert again.terminal == TaskState.DEAD and "不猜" in again.reason, again
    return ok("注入一行 → done；还原 → 不猜（函数体哈希未变）")


@case("L2 表可被外部读出（RULES / rules_summary）+ on_evidence 本体不再是 if/else 链")
def t_table_shape():
    import daedalus.core.router as R
    from daedalus.core.evidence import SIGNALS
    summ = R.rules_summary()
    assert len(summ) == len(R.RULES) >= 20, f"表只有 {len(summ)} 行（要求 ≥20）"
    names = [x["name"] for x in summ]
    assert len(set(names)) == len(names), f"规则名重复：{names}"
    for x in summ:
        assert x["reason"].strip(), f"{x['name']} 没写理由模板"
        assert x["when"] and "lambda" not in x["when"], \
            f"{x['name']} 的谓词不是具名函数：{x['when']}（不许在表里塞 lambda 串）"
        assert x["target"] and x["action"], x
        for s in x["signal"].split("/"):
            assert s == "*" or s in SIGNALS, f"{x['name']} 用了未知信号 {s}"
    used = {x["action"] for x in summ}
    assert used >= {R.ACTION_TERMINAL, R.ACTION_STAGE, R.ACTION_RETRY, R.ACTION_THROTTLE,
                    R.ACTION_CANDIDATE}, f"有动作没人用：{used}"
    body = fn_source("on_evidence")
    assert "elif" not in body, "on_evidence 又长回 if/elif 链了"
    assert "ev.signal ==" not in body and "ev.signal in" not in body, "信号比较必须留在表/谓词里"
    assert len(body.splitlines()) <= 20, f"on_evidence 有 {len(body.splitlines())} 行（要求 ≤20）"
    assert "RULES" in body, "on_evidence 没在读表（那还叫表驱动吗）"
    return ok(f"{len(summ)} 行表、谓词全部具名；on_evidence {len(body.splitlines())} 行、无 elif")


@case("M1 有界：转移次数达上限 → dead（理由写清上限；证据信号是 budget_exceeded）")
def t_transitions_capped():
    from daedalus.core.evidence import from_decision
    from daedalus.core.router import Router
    from daedalus.core.task import TaskState
    r = Router(max_transitions=2)
    t0 = mk_task(None)
    t0.transitions = 1                        # 还差一步 → 正常路由（边界之内不算超）
    d0 = r.on_evidence(t0, from_decision("ok", "?", "还差一步"))
    assert d0.terminal == TaskState.DONE, d0
    t = mk_task(None)
    t.transitions = 2                         # 到上限 → dead
    d = r.on_evidence(t, from_decision("ok", "?", "到上限了"))
    assert d.terminal == TaskState.DEAD, d
    assert "上限" in d.reason and "2" in d.reason, d.reason
    assert d.evidence is not None and d.evidence.signal == "budget_exceeded", d.evidence
    assert d.next_stage is None, d
    return ok(f"transitions=1 → done；transitions=2 → dead（{d.reason}）")


@case("M2 预算耗尽 → dead，且**优先于**转移上限（判定顺序不许被换）")
def t_budget_exhausted():
    from daedalus.core.budget import Budget
    from daedalus.core.evidence import from_decision
    from daedalus.core.router import Router
    from daedalus.core.task import TaskState
    r = Router(max_transitions=2)
    t = mk_task(None, budget=Budget(max_attempts=1))
    t.attempts = 1                            # 重试账耗尽
    d = r.on_evidence(t, from_decision("ok", "?", "预算没了"))
    assert d.terminal == TaskState.DEAD and "预算耗尽（attempts）" in d.reason, d.reason
    assert d.evidence is not None and d.evidence.signal == "budget_exceeded", d.evidence
    t_sec = mk_task(None, budget=Budget(max_seconds=0.0))   # 墙钟这一维也要能收口
    d_sec = r.on_evidence(t_sec, from_decision("ok", "?", "时间没了"))
    assert "预算耗尽（seconds）" in d_sec.reason, d_sec.reason
    # 两个都超：必须先报**预算耗尽**（旧语义就是先判预算；顺序换了理由就变了 → 门禁要能抓到）
    both = mk_task(None, budget=Budget(max_attempts=1))
    both.attempts = 1
    both.transitions = 99
    d2 = r.on_evidence(both, from_decision("ok", "?", "两个都超"))
    assert "预算耗尽" in d2.reason and "上限" not in d2.reason, d2.reason
    return ok("预算耗尽（attempts/seconds）优先；两个都超时理由仍是预算耗尽")


@case("N1 两本账分开：限流只吃 throttles、重试只吃 attempts（各自独立收口）")
def t_two_ledgers():
    from daedalus.core.budget import Budget
    from daedalus.core.evidence import from_response
    from daedalus.core.router import Router
    from daedalus.core.task import TaskState
    r = Router()
    # ① 限流到上限（throttles=2 / max_throttles=3）→ 第 3 次收口；重试账一动不动
    t = mk_task(None, budget=Budget(max_attempts=9, max_throttles=3))
    t.throttles = 2
    d = r.on_evidence(t, from_response(429))
    assert d.terminal == TaskState.DEAD and "限流" in d.reason, d.reason
    assert "重试已达上限" not in d.reason, "被重试账拦下了（两本账串了）"
    assert t.attempts == 0, f"限流动了重试账：attempts={t.attempts}"
    # ② 重试到上限（attempts=2 / max_attempts=3）→ 收口；**限流余量再大也不许把它算进来**
    t2 = mk_task(None, budget=Budget(max_attempts=3, max_throttles=9))
    t2.attempts = 2
    d2 = r.on_evidence(t2, from_response(500))
    assert d2.terminal == TaskState.DEAD and "重试已达上限" in d2.reason, d2.reason
    assert "限流" not in d2.reason, "被限流账拦下了（两本账串了）"
    assert t2.throttles == 0, f"重试动了限流账：throttles={t2.throttles}"
    # ③ 都有余量时两条都只是「退避重试」，且**路由器自己不记账**（记账是运行器的事）
    t3 = mk_task(None, budget=Budget(max_attempts=9, max_throttles=9))
    d3 = r.on_evidence(t3, from_response(429))
    d4 = r.on_evidence(t3, from_response(500))
    assert d3.next_stage == "direct" and "独立计数" in d3.reason, d3.reason
    assert d4.next_stage == "direct" and "第 1 次" in d4.reason, d4.reason
    assert (t3.attempts, t3.throttles) == (0, 0), "路由器越权改了任务的账（记账必须在运行器）"
    return ok("限流/重试各吃各的账、各自独立收口；路由器不记账")


@case("N2 无匹配规则 → 明确失败（不猜）：不静默降级、也不偷偷升级")
def t_no_match_is_loud():
    from daedalus.core.evidence import from_decision
    from daedalus.core.router import Router, Stage
    from daedalus.core.task import TaskState
    r = Router()
    combos = ((Stage.OBSERVE, "media_manifest"), (Stage.BROWSER, "media_manifest"),
              (Stage.ARTIFACT, "empty_content"), (Stage.INSPECT, "network_activity"),
              (Stage.DIRECT, "classified"))
    for stage, sig in combos:
        d = r.on_evidence(mk_task(stage), from_decision(sig, "?", "没有规则管它"))
        assert d.terminal == TaskState.DEAD, f"{stage}+{sig} 没明确失败：{d.describe()}"
        assert d.next_stage is None and d.environment is None, d
        assert "不猜" in d.reason and stage in d.reason and sig in d.reason, d.reason
        assert d.evidence is not None and d.evidence.signal == sig, d.evidence
    return ok(f"{len(combos)} 种没规则的组合都写成「明确失败 + 说清哪个阶段哪个信号」")


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

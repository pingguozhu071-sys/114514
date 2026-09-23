# -*- coding: utf-8 -*-
"""命令行：**一台引擎的机器接口**（GUI 与它共用 `core/app.py` 的同一套装配）

子命令
    version                    版本与元信息
    config [--json]            显示**引擎真正读到**的配置（不是文件里写了什么）
    doctor [--json]            自检：路径/磁盘/库/计划/脱敏/日志/工具/浏览器/凭据/网络缓存
    collect URL... [选项]      采集（真跑闭环：取流 → 原始层 → 理解 → 打卡 → 台账）
    reparse [选项]             用当前解析器**离线**重扫历史原始数据（不联网）
    export [选项]              导出 JSONL（台账 + 任务 + 原始层）
    metrics [--json]           指标快照（含告警）
    alerts [--json]            只看告警（退出码 = 最严重级别）

退出码（**写死，脚本可依赖**）
    0  成功 / 无告警
    1  有失败任务、或有非严重告警（部分成功）
    2  用法或配置错误（参数不对、配置读不出来、资源计划不自洽）
    3  被规矩挡住：全部目标都被策略拒绝（robots / SSRF / 槽位为 0）
    4  内部错误（未预期异常——**绝不用 0 掩盖**）

两条纪律：
  * **危险动作默认 dry-run，且先自证是 dry-run**：`collect --dry-run` 会打印/返回
    `dry_run: true` 与**实测的网络调用数 0**（不是嘴上说"不会出网"）；
  * `--json` 输出**只有 JSON**（人看的文字一律走 stderr），这样管道里接 `jq` 不会被污染。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from daedalus import VERSION, about

__all__ = ["main", "EXIT_OK", "EXIT_PARTIAL", "EXIT_USAGE", "EXIT_BLOCKED", "EXIT_INTERNAL"]

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 3
EXIT_INTERNAL = 4

_JSON_STATE = {"json": False}
# 测试注入点：门禁要离线驱动 CLI（不联网），所以允许替换「怎么造 app」。
# 产品默认永远是 `EngineApp.build`（真咽喉、真库、真闭环）。
_INJECT: dict = {"factory": None}


def _make_output_safe(json_mode: bool = False) -> None:
    """让控制台输出**永不因编码崩**（Windows 打包态的 GBK 控制台是真实存在的场景）。

    真实事故（S10b 打包冒烟抓到的）：`doctor` 的人类可读输出里有 `⚠`/`✗`，在 GBK 代码页的
    cmd 控制台里 `UnicodeEncodeError` 直接把 CLI 打崩——而在 Git Bash（UTF-8）里跑完全正常，
    所以之前 13 个 CLI 用例全绿也没发现。两层修法：
      1) 这里把 stdout/stderr 的 `errors` 设成 `replace`（编不出的字符退化成 `?`，不崩）；
      2) 人类可读输出尽量用 ASCII 标记（`[!]`/`[x]`），符号类只出现在 GUI 与日志文件里。
    第 3 条：`--json` 是**机器读的契约**，编码恒为 UTF-8——跟控制台码页无关。第二起真实事故：
    打包态 `--json doctor` 在 GBK 控制台按 GBK 写出，管道那头 `json.load` 直接 `UnicodeDecodeError`。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            if json_mode and stream is sys.stdout:
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def set_app_factory(fn) -> None:
    """**测试专用**：替换 app 构造（门禁用它注入假咽喉跑真闭环）。"""
    _INJECT["factory"] = fn


def _emit(payload, *, human: str = "", level: str = "out") -> None:
    """统一输出：`--json` 时 stdout **只**出 JSON，人话走 stderr。"""
    if _JSON_STATE["json"]:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    elif human:
        stream = sys.stderr if level == "err" else sys.stdout
        stream.write(human.rstrip() + "\n")


def _load_app(args):
    from daedalus.core.app import EngineApp
    from daedalus.core.limits import PlanViolation
    cfg = None
    if args.config:
        p = pathlib.Path(args.config)
        if not p.exists():
            raise SystemExit(f"配置文件不存在：{p}")
        from daedalus.config import load_config
        cfg = load_config(p)
    # CLI 的默认日志级别：人读时只报 WARNING（别把命令行刷满）；`--json` 时留 INFO
    # （管道/脚本要的就是结构化事件流）。显式 `--log-level` 永远优先。
    from daedalus.config import merged
    cfg = merged(cfg)
    lg = dict(cfg.get("logging") or {})
    lg["level"] = str(args.log_level).upper() if args.log_level else ("INFO" if args.json else "WARNING")
    if args.log_dir:
        lg["dir"] = str(args.log_dir)
    cfg["logging"] = lg
    if _INJECT["factory"] is not None:
        return _INJECT["factory"](args)
    try:
        return EngineApp.build(cfg, data_root=args.data_root,
                               enable_browser=bool(getattr(args, "browser", False)),
                               browser_contexts=(getattr(args, "browser_contexts", None)),
                               browser_pages=(getattr(args, "browser_pages", None)))
    except PlanViolation as e:
        raise SystemExit(f"资源计划不自洽（拒绝启动）：{e}")


# ══════════════════════════════════════════════════════════════════
def cmd_search(args) -> int:
    """中文全文检索（FTS5 + 预分词：中文二元组让「采集」能命中「数据采集引擎」）。"""
    app = _load_app(args)
    try:
        hits = app.search(args.query, limit=args.limit, offset=args.offset)
        _emit({"query": args.query, "hits": hits, "count": len(hits)},
              human=(f"「{args.query}」命中 {len(hits)} 条：\n"
                     + "\n".join(f"  {h['score']:>9}  {h['title'][:40]:40}  {h['url'][:80]}"
                                 for h in hits) if hits else f"「{args.query}」没有命中"),
              level="err" if not hits else "out")
        return EXIT_OK
    finally:
        app.shutdown()


def cmd_ui(args) -> int:
    """启动图形界面（与 CLI 共用 `core/app.py` 的同一套装配）。

    `--no-engine`：只开界面不启动引擎（排查界面问题用；界面上依然能看自检与设置）。
    """
    from daedalus.ui.app import run_ui
    from daedalus.config import load_config
    cfg = load_config(args.config) if args.config else None
    return int(run_ui(cfg=cfg, data_root=args.data_root, start_engine=not args.no_engine))


def cmd_version(args) -> int:
    info = about()
    info["python"] = sys.version.split()[0]
    _emit(info, human=f"{info['name']} {info['version']}（{info['name_zh']} / {info['name_ja']}）")
    return EXIT_OK


def cmd_config(args) -> int:
    from daedalus.config import load_config, merged, to_toml_text
    cfg = merged(load_config(args.config) if args.config else load_config(None))
    if args.write_example:
        p = pathlib.Path(args.write_example)
        p.write_text(to_toml_text(cfg), encoding="utf-8")
        _emit({"written": str(p)}, human=f"示例配置已写入 {p}")
        return EXIT_OK
    if args.toml:
        sys.stdout.write(to_toml_text(cfg))
        return EXIT_OK
    # 关键：这里显示的是**引擎解析后的**配置（一层映射的终点），不是文件原文
    _emit({"config": cfg, "source": args.config or "默认值"},
          human=json.dumps(cfg, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_doctor(args) -> int:
    app = _load_app(args)
    try:
        rep = app.doctor()
    finally:
        app.shutdown()
    lines = [f"数据根：{rep['paths']['data_root']}",
             f"库：{rep['paths']['db']}（{rep['db']['journal_mode']}，"
             f"busy_timeout={rep['db']['busy_timeout']}）",
             f"磁盘：剩余 {rep['disk'].get('free_pct', 0):.1f}%",
             f"内存自证：{rep['memory_arithmetic']}",
             f"日志：{rep['logging'].get('file') or '（仅控制台）'}"]
    cap = rep["browser"]
    lines.append(f"浏览器：{'可用' if cap.get('available') else '不可用'}"
                 + (f"（{cap.get('reason')}）" if not cap.get("available") else
                    f"（{pathlib.Path(cap.get('executable', '')).name}）"))
    lines.append("工具：" + ("齐备" if rep["tools"]["all_present"]
                           else "缺 " + "、".join(rep["tools"]["missing"])))
    for w in rep["warnings"]:
        lines.append(f"[!] {w}")          # ASCII 标记：GBK 控制台也能显示
    for p in rep["problems"]:
        lines.append(f"[x] {p}")
    lines.append("结论：" + ("可用" if rep["ok"] else "有问题（见上）"))
    _emit(rep, human="\n".join(lines), level="err" if not rep["ok"] else "out")
    return EXIT_OK if rep["ok"] else EXIT_PARTIAL


def cmd_collect(args) -> int:
    from daedalus.core.budget import Budget
    targets: list[str] = list(args.urls or [])
    if args.file:
        for line in pathlib.Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                targets.append(line)
    if not targets:
        _emit({"error": "没有目标"}, human="没有目标：给 URL，或用 --file 给一个每行一个 URL 的文件",
              level="err")
        return EXIT_USAGE

    app = _load_app(args)
    try:
        if args.dry_run:
            # **先自证是 dry-run**：不建任务、不出网，只做纯判定与计划展示
            plan = []
            for u in targets:
                allowed, why = app.fetcher.is_allowed(u)
                plan.append({"url": u, "allowed": bool(allowed), "why": why,
                             "budget": (args.max_seconds and {"max_seconds": args.max_seconds}) or None,
                             "workers": args.workers})
            calls = app.fetcher.stats().get("calls", 0)
            out = {"dry_run": True, "targets": len(targets), "plan": plan,
                   "network_calls": int(calls), "note": "dry-run 不建任务、不出网（network_calls 实测）"}
            assert int(calls) == 0, "dry-run 竟然出网了"
            _emit(out, human=f"[dry-run] {len(targets)} 个目标；网络调用实测 {calls} 次\n"
                             + "\n".join(f"  {'允许' if p['allowed'] else '拒绝'} {p['url']}"
                                         f"（{p['why']}）" for p in plan))
            return EXIT_OK
        budget = Budget(max_seconds=float(args.max_seconds)) if args.max_seconds else Budget.small()
        summary = app.run_targets(targets, workers=int(args.workers), budget=budget,
                                  goal=args.goal or "CLI 采集")
        ledger = app.ledger.summary().get("counts", {})
        alerts = app.metrics()["alerts"]
        worst = "critical" if any(a["level"] == "critical" for a in alerts) else \
            ("warn" if any(a["level"] == "warn" for a in alerts) else "ok")
        out = {"summary": summary.to_dict(), "ledger": ledger, "alerts": alerts,
               "data_root": str(app.data_root)}
        human = [f"采集完成：{summary.describe()}", f"台账：{ledger}"]
        if alerts:
            human += [f"  [{a['level']}] {a['message']}" for a in alerts]
        human.append(f"数据根：{app.data_root}")
        _emit(out, human="\n".join(human), level="err" if worst == "critical" else "out")
        states = summary.states
        if states and set(states) <= {"policy_denied"}:
            return EXIT_BLOCKED
        if states.get("dead") or states.get("retry") or states.get("failed"):
            return EXIT_PARTIAL
        return EXIT_OK
    finally:
        app.shutdown()


def cmd_reparse(args) -> int:
    app = _load_app(args)
    try:
        before = app.fetcher.stats().get("calls", 0)
        stats = app.reparse(since=(time.time() - args.days * 86400) if args.days else None,
                            url_like=args.url_like, limit=args.limit, dry_run=args.dry_run)
        after = app.fetcher.stats().get("calls", 0)
        stats["network_calls_delta"] = int(after) - int(before)
        if not args.dry_run:
            assert stats["network_calls_delta"] == 0, "重放竟然联网了"
        human = (f"[重放{'（dry-run）' if stats.get('dry_run') else ''}] "
                 f"扫描 {stats['scanned']} 份原始数据，"
                 + ("" if stats.get("dry_run") else
                    f"解析成功 {stats.get('parsed_ok', 0)} / 失败 {stats.get('parsed_failed', 0)}，"
                    f"落库 {stats.get('delivered', 0)} 条")
                 + f"；网络调用 +{stats['network_calls_delta']}（应为 0）")
        _emit(stats, human=human)
        return EXIT_OK
    finally:
        app.shutdown()


def cmd_export(args) -> int:
    app = _load_app(args)
    try:
        text = app.export_jsonl(limit=args.limit)
        if args.out:
            p = pathlib.Path(args.out)
            p.write_text(text, encoding="utf-8")
            _emit({"written": str(p), "lines": text.count("\n") + 1,
                   "data_root": str(app.data_root)},
                  human=f"已导出 {p}（{text.count(chr(10)) + 1} 行）",
                  level="err")
        else:
            sys.stdout.write(text + "\n")
        return EXIT_OK
    finally:
        app.shutdown()


def cmd_metrics(args) -> int:
    app = _load_app(args)
    try:
        m = app.metrics()
        alerts = m["alerts"]
        s = m["summary"]
        human = [f"页/秒 {s['pages_per_sec']}｜MB/s {s['mb_per_sec']}｜"
                 f"请求 p95 {s['net_latency_p95'] * 1000:.1f} ms",
                 f"任务 总 {s['tasks_total']:.0f} / 成功 {s['tasks_done']:.0f} / "
                 f"失败 {s['tasks_failed']:.0f} / 策略拒绝 {s['tasks_policy_denied']:.0f}",
                 f"队列背压 {s['queue_blocked_puts']:.0f}｜活动线程 {s['active_threads']:.0f}｜"
                 f"RSS {s['rss_mb']:.1f} MB｜盘剩余 {s['disk_free_mb']:.0f} MB"]
        human += [f"  [{a['level']}] {a['message']}" for a in alerts]
        _emit(m, human="\n".join(human), level="err")
        return EXIT_OK
    finally:
        app.shutdown()


def cmd_alerts(args) -> int:
    from daedalus.obs.alerts import worst_level
    app = _load_app(args)
    try:
        alerts = app.metrics()["alerts"]
        level = "critical" if any(a["level"] == "critical" for a in alerts) else \
            ("warn" if any(a["level"] == "warn" for a in alerts) else "ok")
        _emit({"level": level, "alerts": alerts},
              human=f"最严重级别：{level}" + ("".join(f"\n  [{a['level']}] {a['message']}"
                                                     for a in alerts) if alerts else ""),
              level="err" if level != "ok" else "out")
        return EXIT_OK if level == "ok" else EXIT_PARTIAL
    finally:
        app.shutdown()


# ══════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="daedalus",
                                 description=f"{about()['name']} {VERSION} · "
                                             f"{about()['tagline_zh']}")
    ap.add_argument("--json", action="store_true", help="机器可读输出（stdout 只有 JSON）")
    ap.add_argument("--config", help="配置文件路径（TOML）")
    ap.add_argument("--data-root", help="数据根（默认按便携/用户级规则）")
    # CLI 默认只报 WARNING：结构化日志是给文件/排查用的，别把命令行刷满
    ap.add_argument("--log-level", default=None, help="日志级别（默认 WARNING）")
    ap.add_argument("--log-dir", default=None, help="同时写日志文件的目录（带轮转）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("version", help="版本与元信息")
    p.set_defaults(fn=cmd_version)

    p = sub.add_parser("config", help="显示引擎真正读到的配置")
    p.add_argument("--toml", action="store_true", help="以 TOML 形式输出")
    p.add_argument("--write-example", help="把示例配置写到指定路径")
    p.set_defaults(fn=cmd_config)

    for name, fn, helptext in (("doctor", cmd_doctor, "自检"),
                              ("metrics", cmd_metrics, "指标快照"),
                              ("alerts", cmd_alerts, "只看告警")):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(fn=fn)

    p = sub.add_parser("collect", help="采集（真跑闭环）")
    p.add_argument("urls", nargs="*", help="目标 URL")
    p.add_argument("--file", help="从文件读 URL（每行一个，# 开头是注释）")
    p.add_argument("-w", "--workers", type=int, default=4, help="领取线程数（默认 4）")
    p.add_argument("--max-seconds", type=float, default=None, help="单任务墙钟上限（秒）")
    p.add_argument("--goal", default="", help="任务目标（人话，记进任务）")
    p.add_argument("--dry-run", action="store_true", help="只做判定与计划，不出网、不建任务")
    p.add_argument("--browser", action="store_true", help="启用浏览器环境（空壳页走浏览器）")
    p.add_argument("--browser-contexts", type=int, default=None)
    p.add_argument("--browser-pages", type=int, default=None)
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("reparse", help="离线重扫历史原始数据（用当前解析器）")
    p.add_argument("--days", type=float, default=None, help="只看最近 N 天的原始数据")
    p.add_argument("--url-like", default=None, help="URL 模糊匹配（%% 通配）")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="只数一下会扫多少份，不落库")
    p.set_defaults(fn=cmd_reparse)

    p = sub.add_parser("export", help="导出 JSONL")
    p.add_argument("--out", help="输出文件（不给就打到 stdout）")
    p.add_argument("--limit", type=int, default=10000)
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("search", help="中文全文检索（FTS5 + 预分词）")
    p.add_argument("query", help="查询词（支持中文与英文，空格分隔多词）")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("ui", help="启动图形界面（Fluent 玻璃拟态）")
    p.add_argument("--no-engine", action="store_true", help="只开界面，不启动引擎")
    p.set_defaults(fn=cmd_ui)
    return ap


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()                          # 控制台编码安全（打包态 GBK 场景；先兜住 argparse 的报错路径）
    args = build_parser().parse_args(argv)
    _JSON_STATE["json"] = bool(args.json)
    _make_output_safe(_JSON_STATE["json"])       # --json：stdout 固定 UTF-8（机器读的契约，与码页无关）
    try:
        return int(args.fn(args))
    except SystemExit as e:                      # 配置/用法问题：退出码 2
        code = e.code
        if isinstance(code, int):
            raise
        _emit({"error": str(code)}, human=f"错误：{code}", level="err")
        return EXIT_USAGE
    except KeyboardInterrupt:
        _emit({"error": "被用户中断"}, human="被中断（Ctrl+C）", level="err")
        return EXIT_PARTIAL
    except Exception as e:                       # 未预期异常：**绝不用 0 掩盖**
        import traceback
        _emit({"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-2000:]},
              human=f"内部错误：{type(e).__name__}: {e}", level="err")
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())

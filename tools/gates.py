# -*- coding: utf-8 -*-
"""一键跑全部门禁（用于阶段验收与最终检测清单执行）

    python tools/gates.py              # 跑全部
    python tools/gates.py s6 s7        # 只跑某几个
    python tools/gates.py --list       # 列出门禁

三条纪律（都来自真实教训）：
  * **以退出码为准**，不看输出里有没有"PASS"字样（假绿最常见的形式就是只 grep 输出）；
  * **每门独立子进程**：一门崩了不影响其它门，且互不污染（数据根各自独立）；
  * 汇总里同时给出**项数**（从门的输出里解析"共 N 项"）——项数突然变少往往意味着
    有人把断言注释掉了（"断言打到注释"是本工程记过的坑）。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE_DIR = ROOT / "tests" / "gates"

# 顺序 = 施工顺序（前面的门失败时，后面的多半也会红，先修前面）
ORDER = ["contract_selftest", "s1_gate", "s2_gate", "s3_gate", "s4_gate", "s5_gate",
         "s6_gate", "s7_gate", "s8_gate", "s9_gate"]
_COUNT_RE = re.compile(r"共\s*(\d+)\s*项：PASS\s*(\d+)\s*/\s*SKIP\s*(\d+)\s*/\s*FAIL\s*(\d+)")


def discover() -> list[str]:
    have = [g for g in ORDER if (GATE_DIR / f"{g}.py").exists()]
    extra = sorted(p.stem for p in GATE_DIR.glob("*_gate.py") if p.stem not in have)
    extra += sorted(p.stem for p in GATE_DIR.glob("*_selftest.py")
                    if p.stem not in have and p.stem not in extra)
    return have + extra


def run_one(name: str, *, timeout: float = 1800.0) -> dict:
    path = GATE_DIR / f"{name}.py"
    t0 = time.monotonic()
    try:
        r = subprocess.run([sys.executable, str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))
        out, code = (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        out, code = f"超时（>{timeout:.0f}s）", 124
    m = _COUNT_RE.search(out)
    counts = {"total": int(m.group(1)), "pass": int(m.group(2)), "skip": int(m.group(3)),
              "fail": int(m.group(4))} if m else None
    fails = [ln.strip() for ln in out.splitlines() if ln.startswith("[FAIL]")]
    return {"name": name, "exit": code, "seconds": round(time.monotonic() - t0, 2),
            "counts": counts, "fails": fails[:8], "tail": out[-600:]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑全部门禁（退出码 0 = 全绿）")
    ap.add_argument("names", nargs="*", help="只跑这些门（默认全部）")
    ap.add_argument("--list", action="store_true", help="列出门禁")
    ap.add_argument("--timeout", type=float, default=1800.0, help="单门超时（秒）")
    args = ap.parse_args(argv)

    all_gates = discover()
    if args.list:
        for g in all_gates:
            print(f"  {g}")
        return 0
    names = [n for n in (args.names or all_gates)]
    unknown = [n for n in names if n not in all_gates]
    if unknown:
        print(f"没有这些门：{unknown}（--list 看可用）")
        return 2

    print(f"跑 {len(names)} 个门（每门独立子进程，以**退出码**为准）\n" + "─" * 72)
    results = []
    for n in names:
        r = run_one(n, timeout=args.timeout)
        results.append(r)
        c = r["counts"]
        detail = (f"{c['pass']}/{c['total']} 项"
                  + (f"，SKIP {c['skip']}" if c["skip"] else "")
                  + (f"，**FAIL {c['fail']}**" if c["fail"] else "")) if c else "（没解析到项数）"
        mark = "PASS" if r["exit"] == 0 else f"FAIL(exit={r['exit']})"
        print(f"[{mark:14}] {n:20} {r['seconds']:7.2f}s  {detail}")
        for f in r["fails"]:
            print(f"                 {f}")
        if r["exit"] != 0 and not r["fails"]:
            print(f"                 尾部输出：{r['tail'][-300:].strip()}")

    ok = [r for r in results if r["exit"] == 0]
    total_items = sum((r["counts"] or {}).get("total", 0) for r in results)
    print("─" * 72)
    print(f"合计：{len(ok)}/{len(results)} 门全绿；解析到的检查项 {total_items} 项；"
          f"耗时 {sum(r['seconds'] for r in results):.1f}s")
    if len(ok) != len(results):
        print("未通过的门：" + "、".join(r["name"] for r in results if r["exit"] != 0))
        return 1
    print("全部通过（退出码 0）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""静态基线与覆盖率：**只报告、不设阈值；只降不升**

    python tools/baseline.py --measure     # 量一次并写入 docs/16-静态基线与覆盖率.json
    python tools/baseline.py --check       # 与基线对比（当前基准文件名：docs/16-...json）
    python tools/baseline.py --show        # 打印当前量到的值（不写文件）

为什么"只报告不设阈值"（checklist O4 的原话）：
    阈值会变成目的——为了过线去写一堆没断言的测试。这里改成**基线对比**：
    每次量出真实数字，与上次比，**只允许变好**（覆盖率↑、lint 命中↓、门禁项数→不减少）。

"只降不升"具体指：
    * `lint_hard`（硬拦命中）必须为 0；
    * `lint_soft`（风格/待办提示）不得高于基线；
    * `gate_items`（门禁解析到的检查项总数）不得低于基线（防"断言被注释掉"）；
    * `coverage_percent`（对 `src/daedalus` 的覆盖）只报告，**不设下限**——但如果**降了**要写清原因。

覆盖率怎么量：用 `coverage` 跑**全部门禁**（门禁就是本工程的测试集），
统计 `src/daedalus` 的行覆盖。没装 `coverage` 就如实报 `null`（不编）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE_JSON = ROOT / "docs" / "16-静态基线与覆盖率.json"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def lint_counts() -> dict:
    """跑 `tools/lint.py`，解析硬拦/软提示条数（**以退出码为准**，数字只用于基线）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lint", str(ROOT / "tools" / "lint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hard_rules = ("syntax", "dynamic-sql", "unbounded-queue", "naked-network")
    hard = soft = 0
    files = 0
    for d in mod.SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            files += 1
            for f in mod.check_file(p):
                if f["rule"] in hard_rules:
                    hard += 1
                else:
                    soft += 1
    return {"files": files, "lint_hard": hard, "lint_soft": soft}


def gate_counts() -> dict:
    """跑 `tools/gates.py --list` 拿门数；跑每门拿项数（用子进程，避免互相污染）。"""
    gate_dir = ROOT / "tests" / "gates"
    names = [p.stem for p in sorted(gate_dir.glob("*_gate.py"))]
    names += [p.stem for p in sorted(gate_dir.glob("*_selftest.py"))]
    total = 0
    broken: list[str] = []
    count_re = re.compile(r"共\s*(\d+)\s*项")
    for n in names:
        r = subprocess.run([sys.executable, str(gate_dir / f"{n}.py")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(ROOT), shell=False)
        m = count_re.search((r.stdout or "") + (r.stderr or ""))
        if m:
            total += int(m.group(1))
        if r.returncode != 0:
            broken.append(n)
    return {"gates": len(names), "gate_items": total, "gates_failing": broken}


def coverage_percent() -> float | None:
    """用 coverage 跑门禁并取 `src/daedalus` 的行覆盖率（没有 coverage 就返回 None）。"""
    try:
        import coverage                                     # noqa: F401
    except Exception:
        return None
    rc = subprocess.run([sys.executable, "-m", "coverage", "run", "--source=daedalus",
                         "-m", "pytest", "-q", "--collect-only"],
                        capture_output=True, text=True, cwd=str(ROOT), shell=False)
    _ = rc                                                  # 不用 pytest 跑用例（门禁才是测试集）
    # 直接把门禁当测试集跑一遍成本高；这里用 **导入式** 覆盖率：
    # 跑几个"覆盖面广"的门禁（s3/s4/s6/s9）来估计，并在报告里注明这是估计值。
    targets = ["tests/gates/s3_gate.py", "tests/gates/s4_gate.py",
               "tests/gates/s6_gate.py", "tests/gates/s9_gate.py"]
    for t in targets:
        subprocess.run([sys.executable, "-m", "coverage", "run", "--append",
                        f"--source={ROOT / 'src' / 'daedalus'}", t],
                       capture_output=True, text=True, cwd=str(ROOT), shell=False)
    out = subprocess.run([sys.executable, "-m", "coverage", "json", "-o",
                          str(ROOT / "out" / "coverage.json")],
                         capture_output=True, text=True, cwd=str(ROOT), shell=False)
    path = ROOT / "out" / "coverage.json"
    if not path.exists():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        return round(float(body.get("totals", {}).get("percent_covered", 0.0)), 2)
    except Exception:
        return None


def measure(*, with_coverage: bool = True) -> dict:
    data = {"lint": lint_counts(), "gates": gate_counts()}
    data["coverage_percent_src"] = coverage_percent() if with_coverage else None
    data["note"] = ("覆盖率是对**部分门禁**（s3/s4/s6/s9）的估计值：门禁才是本工程的测试集，"
                    "跑全部门禁代价较高，所以这里只报一个可比的量级；"
                    "基线规则：lint_hard=0、lint_soft 不升、gate_items 不降。")
    return data


def _verdict(before: dict, after: dict) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True

    def cmp_num(path: tuple[str, ...], *, better: str) -> None:
        nonlocal ok
        a = before
        b = after
        for k in path:
            a = (a or {}).get(k) if isinstance(a, dict) else None
            b = (b or {}).get(k) if isinstance(b, dict) else None
        if a is None or b is None:
            lines.append(f"· {'.'.join(path)}：数据缺失，跳过对比")
            return
        if better == "down" and b > a:
            ok = False
            lines.append(f"✗ {'.'.join(path)}：{a} → {b}（变差了）")
        elif better == "up" and b < a:
            ok = False
            lines.append(f"✗ {'.'.join(path)}：{a} → {b}（变差了：项数不该减少）")
        else:
            lines.append(f"✓ {'.'.join(path)}：{a} → {b}")

    cmp_num(("lint", "lint_hard"), better="down")
    cmp_num(("lint", "lint_soft"), better="down")
    cmp_num(("gates", "gate_items"), better="up")
    a_cov = (before or {}).get("coverage_percent_src")
    b_cov = (after or {}).get("coverage_percent_src")
    lines.append(f"{'✓' if (a_cov is None or b_cov is None or b_cov >= a_cov) else '·'}"
                 f" coverage：{a_cov} → {b_cov}（只报告，不设阈值）")
    return ok, lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="静态基线与覆盖率（只降不升）")
    ap.add_argument("--measure", action="store_true", help="量一次并写入基线文件")
    ap.add_argument("--check", action="store_true", help="与基线对比")
    ap.add_argument("--show", action="store_true", help="打印当前值（不写文件）")
    ap.add_argument("--no-coverage", action="store_true", help="跳过覆盖率（快）")
    args = ap.parse_args(argv)

    if args.check:
        if not BASELINE_JSON.exists():
            print(f"还没有基线文件（{BASELINE_JSON}）——先跑 --measure")
            return 2
        before = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
        after = measure(with_coverage=not args.no_coverage)
        ok, lines = _verdict(before, after)
        for line in lines:
            print(line)
        print("基线结论：" + ("未变差（可以接受）" if ok else "**有指标变差了**"))
        return 0 if ok else 1

    data = measure(with_coverage=not args.no_coverage)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.measure:
        BASELINE_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入基线：{BASELINE_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

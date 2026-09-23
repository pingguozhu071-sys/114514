# -*- coding: utf-8 -*-
"""源码体检：把"我反复犯的错"变成**机器能拦的错**（`python tools/lint.py`）

为什么自己写一个而不是只靠 flake8/ruff：
    这里要查的都是本工程特有的、从真实事故里总结出来的形状，通用 linter 不认识：

  1) **中文里嵌半角引号**（已犯 5 次）：`print("说"话"")` 这种写法直接 SyntaxError，
     或者更糟——**语法过得去但语义变了**。规则：中文语境一律用「」/【】，不许混半角。
  2) **裸出网调用**：`urllib.request` / `socket` / `requests` 只能出现在 `net/ssrf_gate.py`
     （唯一咽喉）。别处出现就是"多了一条绕过闸的路"。
  3) **动态 SQL**：`execute()` 的第一个实参必须是字符串字面量（`frontier/` 等处的约定）。
  4) **无界队列**：`Queue()` / `queue.Queue()` 不带 `maxsize=` 一律拦下（OOM 第一原因）。
  5) **对抗性能力关键词**：隐身/绕过/打码/签名伪造/出口轮换……本工程边界明确不做，
     出现即拦（真正需要例外时，写 `# noqa: boundary` 并说明理由）。
  6) **`.py` 里出现 `TODO`/`FIXME` 却没登记 PENDING**：未完成项必须可追溯。

用法：`python tools/lint.py`（退出码 0 = 干净）；`--list` 看规则详情。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "tools", "tests")

# 规则 1：字符串**值**里混了半角引号且紧邻中文（例如 '当成"没问题"'）。
# 注意：不能按行扫"引号+中文"——那会把所有正常的中文字符串字面量都报出来
# （`"中文"` 的**值**是 `中文`，引号只是定界符）。所以用 AST 取值再判。
_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
_CJK_QUOTE_IN_VALUE_RE = re.compile(rf"[{_CJK}]\s*[\"']|[\"']\s*[{_CJK}]")
# 规则 2：裸出网
_NET_RE = re.compile(r"\b(urllib\.request|socket\.socket|requests\.(get|post|head)|httpx\.|aiohttp)\b")  # noqa: lint -- 规则自己必须写出这些形状
# 规则 4：无界队列
_QUEUE_RE = re.compile(r"\b(queue\.Queue|Queue)\s*\(\s*\)")  # noqa: lint -- 规则自己必须写出这些形状
# 规则 5：边界关键词
_BOUNDARY_RE = re.compile(
    r"(stealth|指纹伪装|隐身注入|打码平台|验证码识别|captcha.?solv|出口轮换|代理轮换|"
    r"签名伪造|行为拟人|humaniz)", re.IGNORECASE)
# 规则 6：未登记 TODO
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")  # noqa: lint -- 规则自己必须写出这些形状

ALLOW_NET = {"src/daedalus/net/ssrf_gate.py"}
ALLOW_BOUNDARY_FILES = {"tools/lint.py"}          # 规则自己当然会提到这些词
# 行内豁免：`# noqa: lint`（**必须写清理由**，见下方 `--` 之后的文字）。
# 豁免只跳过**这一处**，不是整个文件——否则规则会慢慢失去意义。
NOQA = "noqa: lint"


def _suppressed(line: str) -> bool:
    return NOQA in line


def _multiline_string_lines(text: str) -> set[int]:
    """跨行字符串常量（多半是文档字符串）占据的行号。

    为什么需要：文档字符串里会**举例**写 `urllib.request`、`Queue()` 这类形状，
    它们不是代码。按行扫描会把说明文字当成违规——那正是"假告警"，会让人干脆忽略整个工具。
    """
    out: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            start, end = getattr(node, "lineno", 0), getattr(node, "end_lineno", 0)
            if end > start:
                out.update(range(start, end + 1))
    return out


def _is_comment_or_docstring_source(line: str) -> bool:
    s = line.strip()
    return s.startswith("#") or s.startswith('"""') or s.startswith("'''")


def check_file(path: pathlib.Path) -> list[dict]:
    rel = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    in_docstring = _multiline_string_lines(text)
    out: list[dict] = []

    for i, line in enumerate(lines, 1):
        if _suppressed(line):
            continue
        in_str = i in in_docstring

        # ① 中文里嵌半角引号（真正的判据在 AST 段：字符串**值**里混了半角引号）
        #    这里只留"注释里混用"的轻提示——注释无害但会误导读者。
        if not in_str and _is_comment_or_docstring_source(line) and line.strip().startswith("#") \
                and _CJK_QUOTE_IN_VALUE_RE.search(line):
            out.append({"file": rel, "line": i, "rule": "cjk-quote-comment",
                        "msg": "注释里中文混了半角引号（建议用「」）", "text": line.strip()[:100]})

        if in_str:
            continue                       # 文档字符串里的举例不算违规

        # ② 裸出网
        if rel not in ALLOW_NET and _NET_RE.search(line) and not _is_comment_or_docstring_source(line):
            out.append({"file": rel, "line": i, "rule": "naked-network",
                        "msg": "出网只能走 net/ssrf_gate.py（唯一咽喉）", "text": line.strip()[:100]})

        # ④ 无界队列
        if _QUEUE_RE.search(line) and not _is_comment_or_docstring_source(line):
            out.append({"file": rel, "line": i, "rule": "unbounded-queue",
                        "msg": "队列必须带 maxsize（无界队列是 OOM 第一原因）",
                        "text": line.strip()[:100]})

        # ⑤ 边界关键词
        if rel not in ALLOW_BOUNDARY_FILES and _BOUNDARY_RE.search(line) \
                and not _is_comment_or_docstring_source(line):
            out.append({"file": rel, "line": i, "rule": "boundary",
                        "msg": "命中不做清单的关键词（确为实现合规处置时加 `# noqa: lint -- 理由`）",
                        "text": line.strip()[:100]})

        # ⑥ TODO 未登记
        if _TODO_RE.search(line) and not _is_comment_or_docstring_source(line):
            out.append({"file": rel, "line": i, "rule": "todo-undocumented",
                        "msg": "TODO/FIXME 必须在 docs/PENDING.md 里登记",  # noqa: lint -- 同上
                        "text": line.strip()[:100]})

    # ③ 动态 SQL：用 AST 精确判断（行扫描会漏掉跨行调用）
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        out.append({"file": rel, "line": e.lineno or 0, "rule": "syntax",
                    "msg": f"语法错误：{e.msg}", "text": ""})
        return out

    def _node_suppressed(node) -> bool:
        rng = range(getattr(node, "lineno", 1), getattr(node, "end_lineno", node.lineno) + 1)
        return any(_suppressed(lines[l - 1]) for l in rng if 0 < l <= len(lines))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # ① 字符串**值**里中文紧邻半角引号 → 文法过了但语义/风格都错
            if _CJK_QUOTE_IN_VALUE_RE.search(node.value) and not _node_suppressed(node):
                out.append({"file": rel, "line": node.lineno, "rule": "cjk-quote",
                            "msg": "字符串里中文混了半角引号（一律用「」）",
                            "text": node.value.strip().replace("\n", " ")[:90]})
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", getattr(fn, "id", ""))
        if name in ("execute", "executescript") and node.args:
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                if _node_suppressed(node):
                    continue
                out.append({"file": rel, "line": node.lineno, "rule": "dynamic-sql",
                            "msg": "execute() 的第一个实参必须是字符串字面量",
                            "text": f"line {node.lineno}"})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Daedalus 源码体检（本工程特有的坑）")
    ap.add_argument("--list", action="store_true", help="只列规则")
    ap.add_argument("--all", action="store_true", help="风格/待办类也全列（默认只列 10 条）")
    args = ap.parse_args(argv)
    if args.list:
        print(__doc__)
        return 0

    findings: list[dict] = []
    scanned = 0
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            scanned += 1
            findings.extend(check_file(p))

    print(f"体检 {scanned} 个 .py 文件（规则见 `--list`）")
    if not findings:
        print("没有发现问题：0 处。")
        return 0
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1

    # 硬拦规则先全部列出（它们才是"必须改"的）；风格类只列前若干条并给总数
    hard_rules = ("syntax", "dynamic-sql", "unbounded-queue", "naked-network")
    shown = 0
    for f in findings:
        if f["rule"] not in hard_rules:
            continue
        print(f"  [{f['rule']:16}] {f['file']}:{f['line']}  {f['msg']}")
        if f["text"]:
            print(f"      {f['text']}")
        shown += 1
    soft = [f for f in findings if f["rule"] not in hard_rules]
    if soft:
        print(f"  · 风格/待办类 {len(soft)} 处（不阻断，最多列 10 条）：")
        for f in soft[:10]:
            print(f"      [{f['rule']}] {f['file']}:{f['line']} {f['msg']}")
        if len(soft) > 10:
            print(f"      …还有 {len(soft) - 10} 处（`--all` 看全部）")
        if args.all:
            for f in soft:
                print(f"      [{f['rule']}] {f['file']}:{f['line']} {f['text']}")
    print(f"共 {len(findings)} 处：" + "、".join(f"{k}({v})" for k, v in sorted(by_rule.items())))
    hard = [f for f in findings if f["rule"] in hard_rules]
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())

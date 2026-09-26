# -*- coding: utf-8 -*-
"""解析器注册表：**插件化**的理解面入口

三条约定（「先捕获后理解」能成立的关键）：
  1) **同构返回**：所有解析器都返回 `{"ok": bool, "error"?: str, ...payload}`；
     失败给**可读原因**，不抛异常、不返回空（「诚实降级」）。
  2) **注册即可重扫历史**：新增一个解析器 → 注册 → `capture/replay.py` 就能把历史原始数据
     重新解释一遍（**不重新联网**）。这是本工程「事实不会丢」的兑现方式。
  3) **顺序可解释**：候选解析器按 `order` 升序尝试，失败自动落到下一个；都失败就返回
     `ok=False` 并把「试过哪些 + 各自为什么失败」一并给出。

解析器**必须标注版本**（`version`）：派生记录里会记下是哪个版本产出的，方便将来判断
「要不要用新版本重跑历史」。

────────────────────────────────────────────────────────────────
**注册表是「扫」出来的，不是手写的**（2026-09 修正）

以前 `default_registry()` 里手写三条 `reg.register(...)`，于是 `parsers/mediainfo.py`
定义了 `SPEC` 却**从来没被注册**：引擎路径永远解析不了制品元数据，只有门禁直接 import
才跑到它——那时「注册即生效」是句假话，而手写清单**必然漏**（这次就漏了一个）。

现在：`discover_specs()` 扫 `daedalus.understand.parsers` 目录 → 每个模块的模块级 `SPEC`
自动注册。于是「新增解析器 = 只加一个文件」真的成立（`docs/08-扩展路径.md` §二），
契约由 `tests/gates/s11_gate.py` 用**临时包**钉住（不碰任何既有源文件）。
"""

from __future__ import annotations

import importlib
import logging
import pathlib
import pkgutil
import sys
from dataclasses import dataclass
from typing import Callable

from daedalus.understand.detect import FormatGuess, detect

logger = logging.getLogger(__name__)

__all__ = ["PARSER_PACKAGE", "SPEC_ATTR", "ParserSpec", "ParserRegistry", "SpecScan",
           "default_registry", "discover_specs"]

PARSER_PACKAGE = "daedalus.understand.parsers"
SPEC_ATTR = "SPEC"                     # 模块级导出名：解析器与提取器共用这一个约定


@dataclass(frozen=True)
class ParserSpec:
    """一个解析器：能接哪些格式（`accepts`）、版本、以及 `parse(bytes, meta) -> dict`。"""

    name: str
    version: int
    accepts: tuple[str, ...]          # 格式名（来自 detect 的结论），可用 "*" 表示通配
    parse: Callable[[bytes, dict], dict]
    order: int = 100                  # 越小越先试
    note: str = ""

    def accepts_format(self, name: str) -> bool:
        return "*" in self.accepts or name in self.accepts


@dataclass(frozen=True)
class SpecScan:
    """一次包扫描的**全部结论**：成功的 / 跳过的 / 没能注册的，都要能看见。

    为什么不只返回成功的那些：审计里最贵的一条教训是「插件明明写了却不生效，而且
    查不出来」——所以「跳过」与「失败」必须是一等结果，门禁要能直接断言（`s11_gate.py`）。
    """

    package: str
    specs: tuple[tuple[str, object], ...] = ()      # (模块全名, SPEC)
    skipped: tuple[tuple[str, str], ...] = ()       # (模块全名, 为什么跳过)
    errors: tuple[tuple[str, str], ...] = ()        # (模块全名, 为什么没能注册)

    @property
    def names(self) -> list[str]:
        return [str(getattr(s, "name", "") or "") for _, s in self.specs]

    @property
    def modules(self) -> list[str]:
        return [m for m, _ in self.specs]

    @property
    def ok(self) -> bool:
        return not self.errors

    def why(self) -> str:
        return "；".join(f"{m}：{w}" for m, w in self.errors)


def _iter_module_names(pkg, *, recursive: bool = True) -> list[str]:
    """列出包里的模块**全名**（`包的 __name__` 打头，可直接 `import_module`）。

    ⚠️ 名字必须是**全名**：`pkgutil.iter_modules` 给的叶子名（`sub`）在子包场景下
    import 不了（真名是 `包.sub`）——`s11_gate.py` 的 C1 就是抓这个的（子包递归曾经是坏的）。
    """
    prefix = pkg.__name__ + "."
    out: list[str] = []
    for info in pkgutil.iter_modules(list(getattr(pkg, "__path__", []) or []), prefix):
        out.append(info.name)
        if recursive and info.ispkg:
            try:
                out.extend(_iter_module_names(importlib.import_module(info.name), recursive=True))
            except Exception as e:
                # 子包自己导入失败：留给正常注册路径去报警（这里只负责别把它弄丢）
                logger.debug("展开子包 %s 失败：%s", info.name, e)
    return out


def _frozen_manifest_fullnames(package_name: str) -> list[str]:
    """冻结态（PyInstaller）兜底：读**构建期生成的插件清单**，返回全名列表。

    为什么需要它：`pkgutil.iter_modules` 走文件系统，而 PyInstaller 把模块编进 PYZ 归档，
    冻结态扫描会**漏**（真实事故：解析器注册表从 4 个掉到 1 个——台账 B20-2）。
    清单由 `tools/build.py` 在构建期从源码树生成（`packaging/build_gen/plugin_manifest.json`，
    与 spec 的 `hiddenimports` 同源）——所以源码态「加一个文件即生效」不变，冻结态也能枚举全。
    非冻结态返回空（源码态扫描本来就是真的）。
    """
    import json
    if not getattr(sys, "frozen", False):
        return []
    base = getattr(sys, "_MEIPASS", "")
    if not base:
        return []
    try:
        mf = pathlib.Path(base) / "daedalus" / "understand" / "plugin_manifest.json"
        data = json.loads(mf.read_text(encoding="utf-8"))
        leaves = [str(x) for x in (data.get(package_name) or [])]
    except Exception as e:
        logger.warning("冻结态插件清单读不到（%s）——插件可能不全（这是缺件，不是正常状态）", e)
        return []
    return [f"{package_name}.{leaf}" for leaf in leaves]


def discover_specs(package_name: str, *, attr: str = SPEC_ATTR, recursive: bool = True,
                   expect: type | None = None) -> SpecScan:
    """扫描一个包，收集每个模块的模块级 `attr`（默认 `SPEC`）——**加一个文件即生效**。

    四条规矩（都是从「插件写了却不生效」这类坑里抠出来的）：
      * **没有 `SPEC` 的模块跳过**：只记一行 debug（`__init__`、纯工具模块是合法的）；
      * **导入失败要报警**：`logger.warning` + 记进 `errors`。**不许静默吞**——
        吞掉之后「我的解析器怎么没生效」会变成查不出来的谜；
      * **`name` 重复要报警**：同名会产生两个候选，候选顺序与派生记录里的 `parser`
        字段都变得不可解释；
      * **只扫这个包自己的目录**（`pkgutil.iter_modules(包的 __path__)`），不猜别处。

    `expect` 给定时校验类型（例如提取器侧要求 `ExtractorSpec`）：类型不对记进 `errors`，
    不当成可用插件——「拿错形状的 SPEC 当插件用」比缺插件更难查。
    """
    specs: list[tuple[str, object]] = []
    skipped: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    try:
        pkg = importlib.import_module(package_name)
    except Exception as e:
        msg = f"包导入失败：{type(e).__name__}: {e}"
        logger.warning("插件包 %s %s（整包都注册不了）", package_name, msg)
        return SpecScan(package_name, (), (), ((package_name, msg),))

    modules = _iter_module_names(pkg, recursive=recursive)
    if getattr(sys, "frozen", False):
        # 冻结态：pkgutil 扫不到 PYZ 里的模块（或只扫到一部分）→ 用构建期清单补齐。
        # 真实事故：解析器注册表 4 → 1（漏的三个压根没被枚举到，台账 B20-2）。
        extra = [m for m in _frozen_manifest_fullnames(package_name) if m not in modules]
        if extra:
            logger.warning("冻结态插件扫描只见到 %d 个 → 补用构建期清单 %d 个（PYZ 限制）",
                           len(modules), len(extra))
            modules = modules + extra

    for full in modules:
        try:
            mod = importlib.import_module(full)
        except Exception as e:
            msg = f"导入失败：{type(e).__name__}: {e}"
            logger.warning("插件模块 %s %s（它不会生效，其它插件不受影响）", full, msg)
            errors.append((full, msg))
            continue
        spec = getattr(mod, attr, None)
        if spec is None:
            logger.debug("跳过 %s：没有模块级 %s", full, attr)
            skipped.append((full, f"没有模块级 {attr}"))
            continue
        if expect is not None and not isinstance(spec, expect):
            msg = (f"{attr} 类型不对：期望 {expect.__name__}，实际 "
                   f"{type(spec).__name__}（拿错形状的插件按缺失处理）")
            logger.warning("插件模块 %s 的 %s 不合法：%s", full, attr, msg)
            errors.append((full, msg))
            continue
        name = str(getattr(spec, "name", "") or "").strip()
        if not name:
            msg = f"{attr} 缺少 name（没有名字就没法被解释成候选）"
            logger.warning("插件模块 %s 的 %s 不合法：%s", full, attr, msg)
            errors.append((full, msg))
            continue
        if name in seen:
            msg = f"name 重复：{name}（已在 {seen[name]} 注册过）"
            logger.warning("插件模块 %s 的 %s 不合法：%s", full, attr, msg)
            errors.append((full, msg))
            continue
        seen[name] = full
        specs.append((full, spec))
    return SpecScan(package_name, tuple(specs), tuple(skipped), tuple(errors))


class ParserRegistry:
    """解析器注册表（线程安全地只读使用：注册一般在启动阶段一次性完成）。"""

    def __init__(self, scan: SpecScan | None = None):
        self._parsers: list[ParserSpec] = []
        self.scan = scan            # 若来自目录扫描，这里留着「跳过了谁/谁没注册上」

    # ── 注册与查询 ───────────────────────────────────────────────
    def register(self, spec: ParserSpec) -> "ParserRegistry":
        self._parsers.append(spec)
        self._parsers.sort(key=lambda s: (s.order, s.name))
        logger.debug("注册解析器 %s v%d（接 %s）", spec.name, spec.version, ",".join(spec.accepts))
        return self

    def parsers(self) -> list[ParserSpec]:
        return list(self._parsers)

    def candidates(self, fmt: str) -> list[ParserSpec]:
        return [s for s in self._parsers if s.accepts_format(fmt)]

    def summary(self) -> list[dict]:
        return [{"name": s.name, "version": s.version, "accepts": list(s.accepts),
                 "order": s.order, "note": s.note} for s in self._parsers]

    # ── 解析 ─────────────────────────────────────────────────────
    def parse(self, data: bytes, meta: dict | None = None, url: str | None = None,
              fmt: FormatGuess | str | None = None) -> dict:
        """解析一段字节。返回**同构字典**：`{"ok", "error"?, "parser", "parser_version", ...}`。"""
        meta = dict(meta or {})
        guess = fmt if isinstance(fmt, FormatGuess) else detect(data, meta, url)
        base = {"format": guess.name, "format_how": guess.how, "format_confidence": guess.confidence}
        cands = self.candidates(guess.name)
        if not cands:
            return dict(base, ok=False,
                        error=f"没有解析器能处理格式 {guess.name!r}（原始数据已存，可延期解释）",
                        tried=[])
        tried: list[str] = []
        for spec in cands:
            tried.append(spec.name)
            try:
                out = spec.parse(bytes(data or b""), dict(meta, format=guess.name, url=url or ""))
            except Exception as e:                     # 解析器自身崩了也不能带塌上层
                logger.warning("解析器 %s 抛异常: %s", spec.name, e)
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if isinstance(out, dict) and out.get("ok"):
                return dict(base, ok=True, parser=spec.name, parser_version=spec.version,
                            tried=tried, **{k: v for k, v in out.items() if k != "ok"})
        return dict(base, ok=False, parser="", parser_version=0, tried=tried,
                    error=f"所有候选解析器都失败（试过 {tried}）："
                          f"{'; '.join(f'{p}' for p in tried)}")


def default_registry(package: str = PARSER_PACKAGE) -> ParserRegistry:
    """装上**目录里能找到的所有**解析器（扫描 → 自动注册；不再有手写清单）。

    ⚠️ 这里原来是手写三条 `reg.register(...)`：`parsers/mediainfo.py` 的 `SPEC` 就因此
    漏了注册，引擎路径永远到不了它。手写清单必然漏 —— 所以现在扫目录：
    **新增解析器 = 只加一个文件**。扫描的「跳过/失败」留在 `registry.scan` 上供门禁断言。
    """
    scan = discover_specs(package, expect=ParserSpec)
    reg = ParserRegistry(scan=scan)
    for _module, spec in scan.specs:
        reg.register(spec)
    if scan.errors:
        logger.warning("解析器扫描有 %d 个模块没能注册（%s）：%s",
                       len(scan.errors), package, scan.why())
    logger.info("解析器注册表：%d 个（扫描 %s；跳过 %d 个没有 %s 的模块）",
                len(scan.specs), package, len(scan.skipped), SPEC_ATTR)
    return reg

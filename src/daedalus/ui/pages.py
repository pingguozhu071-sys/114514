# -*- coding: utf-8 -*-
"""页面：概览 / 任务 / 日志 / 设置 / 关于（设置固定在最下面）

三条纪律（机主明确点过的）：
  * **界面文案一律走 i18n**（`ui/i18n.py` 的键），这里**不许出现裸的可视文案**——
    否则「安装时选了简体中文、装完蹦日文」这类问题会从任何一个漏网的字符串里冒出来；
    门禁会扫本文件里是否还有未走文案表的中文。
  * **布局照 Kiana 的规格**（外边距 28 / 卡片间距 14 / 卡内 18,14 / 卡内间距 4,10,12），
    宁可留白也不要挤：卡片间距必须大于卡内间距，页面内容不拉伸（`PageBase` 末尾有 stretch）。
  * **每页自带标题 + 一行 12px 副标题**（`_header`）：外壳换成库的 `FluentWindow` 之后，
    库不再提供页面标题区；副标题的作用是**破掉「一上来就是一屏卡」的拥挤感**，
    顺便一句话说清这一页能干什么。

每页都接受 `ctx`（引擎句柄 + 设置 + 刷新回调）；`ctx` 为空时**照样能画出来**（离线测试、
或引擎还没启动时）——UI 不该因为「引擎没跑」就白屏或崩掉。

日志页是**真数据源**（不是摆设）：`_QtLogHandler`（logging.Handler 子类）把每一行交给
`_make_log_bridge()` 建的 `QObject` 桥（`Signal(str)`，跨线程自动排队，绝不在子线程碰控件），
落界面**之前**过 `obs.policy` 的脱敏；界面自己的提示（`ctx.notes`）另外用定时器镜进去
（前缀 `[界面]`）——否则用户看不到「为什么这里是空的」。上限 5000 行（有界，不随运行时长涨内存）。
"""

from __future__ import annotations

import logging
import re

from daedalus.ui.i18n import translator
from daedalus.ui.widgets import GlassCard, PageBase, StatCard

logger = logging.getLogger(__name__)

__all__ = ["build_pages", "page_titles", "PAGE_KEYS", "task_detail_text", "LOG_VIEW_MAX",
           "read_txt_text", "extract_url_from_line", "parse_txt_links", "group_urls_by_domain"]

# 页面键与**文案键**（标题从文案表取，不再写死）
PAGE_KEYS = ("overview", "tasks", "logs", "settings", "about")

# 日志面板的行数上限（有界：内存不随运行时长增长；Kiana 给的数值）
LOG_VIEW_MAX = 5000

# 界面提示镜进日志面板的节奏（ms）
NOTES_POLL_MS = 500

# 设置项分组：`(分组文案键, ((设置键, 文案键, 控件类型), …))`
_SETTINGS_GROUPS = (
    ("settings.group.theme", (("light", "settings.light", "bool"),)),
    ("settings.group.accent", (("accent", "settings.accent", "accent"),
                               ("accent_locked", "settings.accent_locked", "bool"))),
    ("settings.group.glass", (("panel_alpha", "settings.alpha", "int"),)),
    ("settings.group.wallpaper", (("wallpaper", "settings.wallpaper", "file"),
                                  ("blur", "settings.blur", "int"),
                                  ("dim_manual", "settings.dim", "int"),
                                  ("focus", "settings.focus", "focus"),
                                  ("downsample_max", "settings.downsample", "int"))),
    ("settings.group.type", (("font_pt", "settings.font", "float"),
                             ("density", "settings.density", "choice"))),
    ("settings.group.motion", (("animations", "settings.animations", "bool"),
                               ("fade_ms", "settings.fade", "int"),
                               ("debounce_ms", "settings.debounce", "int"),
                               ("fps_cap", "settings.fps", "int"))),
    ("settings.group.logs", (("log_autoscroll", "settings.log_autoscroll", "bool"),)),
    # ⚠️ `expert_mode`（专家模式：单卡覆写玻璃参数）**未实现**——它只被主题令牌读一次、
    #    没有任何行为分支，摆了就是个「点了没反应」的假开关，所以**不暴露**。
    #    （存储层与令牌仍保留该字段；将来真做了再把它加回这张表。）
    ("settings.group.other", (("signature", "settings.signature", "bool"),)),
)


def page_titles(t) -> tuple[tuple[str, str], ...]:
    """按当前语言给出 `((页面键, 标题), …)`。"""
    return tuple((k, t(f"nav.{k}")) for k in PAGE_KEYS)


def build_pages(window, tokens, ctx=None) -> dict:
    """建好所有页面，返回 `{key: page}`。语言从 `tokens.locale` 来（由 app 解析后注入）。"""
    t = translator(getattr(tokens, "locale", "en-US"))
    return {
        "overview": _overview(window, tokens, ctx, t),
        "tasks": _tasks(window, tokens, ctx, t),
        "logs": _logs(window, tokens, ctx, t),
        "settings": _settings(window, tokens, ctx, t),
        "about": _about(window, tokens, t),
    }


def _header(page, lay, tokens, t, key: str) -> int:
    """每页顶部的**标题 + 一行 12px 灰副标题**；返回「正文从第几个位置开始」。

    为什么页面自己出标题：外壳换成了库的 `FluentWindow`，它只管导航与切页动画，
    不再给页面留标题区——不补这一块，每页一打开就是一屏卡（挤）。
    """
    from qfluentwidgets import TitleLabel
    title = TitleLabel(t(f"nav.{key}"), page)
    title.setObjectName("pageTitle")
    sub = PageBase.subtitle(page, tokens, t(f"page.{key}.sub"))
    lay.insertWidget(0, title)
    lay.insertWidget(1, sub)
    page._title = title             # noqa: SLF001 - 语言切换后整页重建，这里只是便于自检
    page._subtitle = sub            # noqa: SLF001
    return 2


# ── 概览 ─────────────────────────────────────────────────────────
def _overview(window, tokens, ctx, t):
    page, lay = PageBase.make(tokens, name="overview", parent=window)
    at = _header(page, lay, tokens, t, "overview")
    cards = [StatCard.make(tokens, t("stat.pages_per_sec"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.mb_per_sec"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.tasks_ok_fail"), "—", name="statCard"),
             StatCard.make(tokens, t("stat.request_p95"), "—", name="statCard")]
    row = GlassCard.make(tokens, name="card")
    rl = GlassCard.body(row, tokens)
    from PySide6.QtWidgets import QHBoxLayout, QWidget
    holder = QWidget(row)
    hl = QHBoxLayout(holder)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(tokens.spacing()["gap"])
    for c in cards:
        hl.addWidget(c)
    rl.addWidget(holder)
    # 内存自证与资源计划：把「嘴上说的 ≤4GB」贴到界面上——**有引擎就贴运行中的那份**，
    # 没有就贴出厂默认并**说清楚是哪一种**（免得用户以为这就是当前生效的参数）。
    plan = GlassCard.make(tokens, name="card")
    pl = GlassCard.body(plan, tokens)
    from daedalus.core.limits import ResourcePlan
    eng = getattr(ctx, "engine", None)                 # 只经 ctx 拿引擎（D5 纪律）
    live_plan = getattr(eng, "plan", None)
    if live_plan is not None:
        plan_txt = live_plan.memory_arithmetic()
        src_key = "overview.plan_live"
    else:
        plan_txt = ResourcePlan.from_config(None).memory_arithmetic()
        src_key = "overview.plan_default"
    lab = _label(t("overview.plan", text=plan_txt), tokens)
    pl.addWidget(lab)
    pl.addWidget(_label(t(src_key), tokens, muted=True))
    pl.addWidget(_label(t("overview.plan_note"), tokens, muted=True))
    lay.insertWidget(at, row)
    lay.insertWidget(at + 1, _quick_collect(page, tokens, ctx, t))
    lay.insertWidget(at + 2, plan)
    page._stat_cards = cards          # noqa: SLF001 - 刷新时要用
    page._plan_label = lab            # noqa: SLF001 - `MainWindow.refresh_pages` 用它刷新
    return page


# ── TXT 链接导入（机主原话：直接提取 txt 里面的链接…一行一个…断掉的排除…小分类过滤）──
# URL 记号：从 http(s):// 起，到**空白或 CJK 字符/标点**为止——中文排版里标题常紧贴
# 逗号（形如「https://a/b，标题」），只按空白截断会把整段标题吃进记号；半角括号**不在**
# 排除集里，维基式 URL（/wiki/X_(Y)）不会被截坏。raw 字符串里的 \uXXXX 由 re 解释。
_URL_TOKEN_RE = re.compile(
    r"https?://[^\s\u3000-\u303f\uff00-\uffef\u2018-\u201f\u2026\u4e00-\u9fff]+")

# 行尾要剥的中文标点（机主点名的「，。；、）」】 等」；都在上面的排除集里，这里只是兜底）
_TRAILING_PUNCT = "，。；、！？）」』】〉》…·"


def read_txt_text(path) -> str:
    """读 TXT 并探测编码：有 BOM 按 utf-8-sig / utf-16 → 否则依次试 utf-8、gbk。

    都解不开时用 utf-8 + replace 兜底（链接是 ASCII 的，个别乱码字不伤提取；
    整个文件直接报失败反而更不诚实——里面明明还有能用的链接）。
    """
    import pathlib
    data = pathlib.Path(str(path)).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")                 # 带 BOM 的 utf-16 自动判端序
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_url_from_line(line: str) -> str:
    """从一行里提取**第一个 http(s):// 记号**；容忍「1. url 标题」这类前后缀。"""
    m = _URL_TOKEN_RE.search(str(line))
    if m is None:
        return ""
    tok = m.group(0)
    while tok and tok[-1] in _TRAILING_PUNCT:        # 剥掉行尾标点（，。；、）」】 等）
        tok = tok[:-1]
    return tok


def _url_host(tok: str) -> str:
    """取记号的域名部分（scheme 之后、第一个 / ？# 之前）。"""
    rest = tok.split("://", 1)[1] if "://" in tok else ""
    return rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def _valid_url_token(tok: str) -> bool:
    """机主的校验：scheme 是 http/https、域名非空、记号无空白——缺一即「断掉的链接」。"""
    if not tok or not (tok.startswith("http://") or tok.startswith("https://")):
        return False
    if any(ch.isspace() for ch in tok):
        return False
    return bool(_url_host(tok))


def parse_txt_links(text: str) -> dict:
    """TXT 文本 → 采集目标清单（**纯函数**，界面与门禁共用同一套口径）。

    逐行提取 → 无效排除（M）→ 去重保序（D）→ 剩下的就是导入清单（N，保持首次出现顺序）；
    域名集合单独给出（K），供「小分类过滤」按域名分组输出。
    """
    urls: list[str] = []
    invalid = dup = 0
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue                                  # 空行不算链接、也不算失败
        tok = extract_url_from_line(line)
        if not _valid_url_token(tok):
            invalid += 1
            continue
        if tok in urls:
            dup += 1
            continue
        urls.append(tok)
    domains = sorted({_url_host(u) for u in urls})
    return {"urls": urls, "invalid": invalid, "dup": dup, "domains": domains}


def group_urls_by_domain(urls) -> str:
    """「小分类过滤」的输出：按域名分组、域名排序、组间空行（collect 本来就跳过空行）。"""
    groups: dict[str, list[str]] = {}
    for u in urls:
        groups.setdefault(_url_host(u), []).append(u)
    return "\n\n".join("\n".join(groups[d]) for d in sorted(groups))


def _quick_collect(page, tokens, ctx, t):
    """概览页的**快速采集**卡：贴一批目标 → 开始 → 看指标动 → 可停。

    为什么要有它：原来界面**只能看不能干**（每个页面都是只读的摆设，用户说「模块都用不了」），
    而这个引擎真正的入口只有命令行。这里给一条最短的可用路径：多行目标 + 开始/停止 + 状态。

    * 采集跑在**独立线程**里（`ctx.engine.run_targets` 会开自己的 worker），界面只读进度；
    * 「停止」走 `stop_event`：worker 下一轮领取前退出，**已领走的租约不丢**（会超时回队列）；
    * 引擎没起来时按钮**禁用**并说明原因（不是点了没反应）；
    * 「导入 TXT」：文件对话框只收 *.txt；编码探测（BOM → utf-8 → gbk）+ 逐行提取
      第一个 http(s):// 记号 + 无效排除 + 去重保序，再按域名分组（小分类过滤）写进文本框，
      状态栏与通知**如实回报**「导入 N 条（排除 M 条无效，去重 D 条），K 个域名」。
    """
    from PySide6.QtWidgets import QPlainTextEdit, QHBoxLayout, QPushButton
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("collect.title"), tokens))
    box = QPlainTextEdit(card)
    box.setObjectName("collectTargets")
    box.setPlaceholderText(t("collect.placeholder"))
    box.setFixedHeight(64)                       # 规格里的多行框高度
    cl.addWidget(box)
    row = QHBoxLayout()
    imp = QPushButton(t("collect.import_txt"), card)
    imp.setObjectName("collectImport")
    start = QPushButton(t("collect.start"), card)
    start.setObjectName("collectStart")
    stop = QPushButton(t("collect.stop"), card)
    stop.setObjectName("collectStop")
    stop.setEnabled(False)
    status = _label(t("collect.idle"), tokens, muted=True)
    row.addWidget(imp)
    row.addWidget(start)
    row.addWidget(stop)
    row.addWidget(status, 1)
    cl.addLayout(row)

    engine = getattr(ctx, "engine", None)         # 只经 ctx 拿引擎（D5 纪律）
    state = {"thread": None, "stop": None, "t0": 0.0}

    # 采集线程**自己**回报结束（跨线程走 Qt 信号）。
    # 为什么不能只靠每秒的指标轮询来恢复按钮：那条路要等 `ctx.refresh()` 成功返回——
    # 指标读失败时按钮会永远卡在「运行中」（门禁 E11 就是这么抓出来的）。
    from PySide6.QtCore import QObject, Signal

    class _Bridge(QObject):
        finished = Signal()

    bridge = _Bridge(card)

    if engine is None:
        start.setEnabled(False)
        status.setText(t("collect.no_engine"))

    def _refresh_status() -> None:
        th = state["thread"]
        running = th is not None and th.is_alive()
        if running:
            import time as _t
            status.setText(t("collect.running", n=int(_t.monotonic() - state["t0"])))
        # 引擎起得比窗口晚时，开始按钮要能自己解禁（同任务页那条教训）
        elif not state["stop"]:
            start.setEnabled(getattr(ctx, "engine", None) is not None)
            if getattr(ctx, "engine", None) is None:
                status.setText(t("collect.no_engine"))

    def _on_finished() -> None:
        start.setEnabled(engine is not None)
        stop.setEnabled(False)
        if state["t0"]:
            status.setText(t("collect.idle"))

    def _start() -> None:
        import threading
        urls = [u.strip() for u in box.toPlainText().splitlines() if u.strip()]
        if not urls:
            ctx.notify(t("collect.need_target"), error=True)
            return
        import time as _t
        state["stop"] = threading.Event()
        state["t0"] = _t.monotonic()

        def _run() -> None:
            try:
                s = ctx.engine.run_targets(urls, workers=4, stop_event=state["stop"])
                note = t("collect.done", n=getattr(s, "done", 0), f=getattr(s, "failed", 0),
                         why=t("collect.stopped") if getattr(s, "stopped_early", False) else "")
                ctx.notify(note)
            except Exception as e:               # 采集失败要看得见，不许静默
                ctx.notify(t("collect.failed", err=f"{type(e).__name__}: {e}"), error=True)
            finally:
                state["t0"] = 0.0
                bridge.finished.emit()           # 跨线程 → 排队回 GUI 线程

        state["thread"] = threading.Thread(target=_run, name="dae-ui-collect", daemon=True)
        state["thread"].start()
        start.setEnabled(False)
        stop.setEnabled(True)
        status.setText(t("collect.running", n=0))

    def _stop() -> None:
        if state["stop"] is not None:
            state["stop"].set()
        stop.setEnabled(False)
        status.setText(t("collect.stopping"))

    def _import_txt() -> None:
        """导入 TXT：探测编码 → 提取/排除/去重 → 按域名分组写进文本框 → 如实回报计数。

        每一步都**不静默**：读不出、没找到链接、导入成功，三种结局都落到状态栏 + 通知。
        """
        from PySide6.QtWidgets import QFileDialog
        import pathlib
        path, _ = QFileDialog.getOpenFileName(None, t("dialog.pick_txt"), "",
                                              f"{t('dialog.txt_filter')} (*.txt)")
        if not path:
            return
        try:
            text = read_txt_text(path)
        except Exception as e:                       # 读不出来要看得见（权限/被删等）
            msg = t("collect.import_failed", err=f"{type(e).__name__}: {e}")
            status.setText(msg)
            _toast(ctx, msg, error=True)
            return
        parsed = parse_txt_links(text)
        if not parsed["urls"]:
            msg = t("collect.import_none", name=pathlib.Path(str(path)).name)
            status.setText(msg)
            _toast(ctx, msg)
            return
        box.setPlainText(group_urls_by_domain(parsed["urls"]))
        msg = t("collect.import_done", n=len(parsed["urls"]), m=parsed["invalid"],
                d=parsed["dup"], k=len(parsed["domains"]))
        status.setText(msg)
        _toast(ctx, msg)

    bridge.finished.connect(_on_finished)
    start.clicked.connect(_start)
    stop.clicked.connect(_stop)
    imp.clicked.connect(_import_txt)
    page._refresh_collect = _refresh_status       # noqa: SLF001 - 轮询里顺手刷新「运行 N 秒」
    page._collect_bridge = bridge                 # noqa: SLF001 - 防被 GC + 便于门禁查看
    page._collect = {"start": start, "stop": stop, "box": box, "status": status,
                     "import": imp}                # noqa: SLF001
    return card


def _label(text: str, tokens, *, muted: bool = False):
    from PySide6.QtWidgets import QLabel
    from daedalus.ui.theme import status_colors
    lab = QLabel(str(text))
    lab.setWordWrap(True)
    if muted:
        lab.setStyleSheet(f"color: {status_colors(tokens.light)['muted']};"
                          f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    return lab


# ── 任务 ─────────────────────────────────────────────────────────
def _tasks(window, tokens, ctx, t):
    page, lay = PageBase.make(tokens, name="tasks", parent=window)
    at = _header(page, lay, tokens, t, "tasks")
    card = GlassCard.make(tokens, name="taskCard")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("tasks.hint"), tokens))
    from PySide6.QtWidgets import QTableWidget
    table = QTableWidget(0, 6, card)
    table.setObjectName("taskTable")
    table.setHorizontalHeaderLabels([t("tasks.col.state"), t("tasks.col.target"),
                                     t("tasks.col.counters"), t("tasks.col.evidence"),
                                     t("tasks.col.fingerprint"), t("tasks.col.bytes")])
    table.setStyleSheet("background: transparent; border: none;")
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    cl.addWidget(table)
    # 空态：一上来就没有任务时**说清楚怎么让它有**（比一张空表格有用）
    # `MainWindow.refresh_pages` 会按行数切换它——所以名字固定叫 `_empty_label`。
    empty = _label(t("tasks.empty"), tokens, muted=True)
    empty.setObjectName("tasksEmpty")
    cl.addWidget(empty)
    # 双击下钻：task_id 约定存在**第 0 列 item 的 UserRole** 里（写入方在 app.py）
    table.cellDoubleClicked.connect(lambda r, _c: _open_task_detail(page, ctx, table, r, t))
    # 工具行：三个**真操作**（不是摆设）——重试 / 导出选中 / 删除记录
    tools = _task_tools(page, table, tokens, ctx, t)
    lay.insertWidget(at, tools)
    lay.insertWidget(at + 1, card)
    page._table = table             # noqa: SLF001
    page._empty_label = empty       # noqa: SLF001
    return page


def _selected_task_ids(table) -> list[str]:
    """取选中行的 task_id（约定：第 0 列 item 的 UserRole）。空选返回空列表。"""
    from PySide6.QtCore import Qt
    rows = sorted({i.row() for i in table.selectedIndexes()})
    out: list[str] = []
    for r in rows:
        item = table.item(r, 0)
        if item is None:
            continue
        tid = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if tid:
            out.append(tid)
    return out


def _task_tools(page, table, tokens, ctx, t):
    """任务页的工具行：重试失败任务 / 导出选中结果 / 删除任务记录。

    三条都**走引擎的公开方法**（`retry_tasks` / `export_tasks_jsonl` / `forget_tasks`），
    并且都遵守同一条红线：**原始层只读**——重试只是重排队、导出只是读、删除只动任务行。
    """
    from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPushButton
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("tasks.tools"), tokens))
    row = QHBoxLayout()
    retry = QPushButton(t("tasks.retry"), card)
    retry.setObjectName("tasksRetry")
    export = QPushButton(t("tasks.export"), card)
    export.setObjectName("tasksExport")
    forget = QPushButton(t("tasks.forget"), card)
    forget.setObjectName("tasksForget")
    hint = _label(t("tasks.tools_hint"), tokens, muted=True)
    row.addWidget(retry)
    row.addWidget(export)
    row.addWidget(forget)
    row.addWidget(hint, 1)
    cl.addLayout(row)

    engine = getattr(ctx, "engine", None)         # 只经 ctx 拿引擎（D5 纪律）

    def _sync_enabled() -> None:
        """按「现在有没有引擎」同步三个按钮的可用状态。

        为什么要**每次都同步**（而不是建页时定死）：引擎起得比窗口晚（或测试里后注入桩）时，
        定死的状态会让按钮永远是灰的——用户看到的是「点了没反应」。走查就是这么抓到的。
        """
        live = getattr(ctx, "engine", None) is not None
        for b in (retry, export, forget):
            b.setEnabled(live)
        if not live:
            hint.setText(t("tasks.no_engine"))

    if engine is None:
        _sync_enabled()

    def _need() -> list[str] | None:
        ids = _selected_task_ids(table)
        if not ids:
            _toast(ctx, t("tasks.pick_row"), error=True)
            return None
        return ids

    def _retry() -> None:
        ids = _need()
        if ids is None:
            return
        try:
            rep = ctx.engine.retry_tasks(ids)
            bad = [r for r in rep.get("results", []) if not r.get("ok")]
            _toast(ctx, t("tasks.retry_done", n=rep.get("ok", 0), total=rep.get("requested", 0),
                          why=("；" + "；".join(str(b.get("why")) for b in bad[:3])) if bad else ""),
                   error=bool(bad) and rep.get("ok", 0) == 0)
        except Exception as e:                     # 失败要看得见
            _toast(ctx, t("tasks.retry_failed", err=f"{type(e).__name__}: {e}"), error=True)

    def _export() -> None:
        ids = _need()
        if ids is None:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(None, t("tasks.export"), "daedalus_tasks.jsonl",
                                              "JSONL (*.jsonl)")
        if not path:
            return
        try:
            text = ctx.engine.export_tasks_jsonl(ids)
            import pathlib
            pathlib.Path(path).write_text(text, encoding="utf-8")   # 显式 UTF-8，别靠系统默认
            _toast(ctx, t("tasks.export_done", n=len(ids), path=path))
        except Exception as e:
            _toast(ctx, t("tasks.export_failed", err=f"{type(e).__name__}: {e}"), error=True)

    def _forget() -> None:
        ids = _need()
        if ids is None:
            return
        # **先确认**：说明白删什么、不删什么（原始层一个字都不动）
        ans = QMessageBox.question(None, t("tasks.forget_title"),
                                   t("tasks.forget_ask", n=len(ids)),
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            pre = ctx.engine.forget_tasks(ids, dry_run=True)        # 先自证要删多少行
            rep = ctx.engine.forget_tasks(ids, dry_run=False)
            got = dict(rep.get("deleted") or {})
            _toast(ctx, t("tasks.forget_done", n=got.get("tasks", 0),
                          ev=got.get("task_evidence", 0), dry=pre.get("deleted", {}).get("tasks", 0)))
        except Exception as e:
            _toast(ctx, t("tasks.forget_failed", err=f"{type(e).__name__}: {e}"), error=True)

    retry.clicked.connect(_retry)
    export.clicked.connect(_export)
    forget.clicked.connect(_forget)
    page._tools = {"retry": retry, "export": export, "forget": forget,
                   "hint": hint, "sync": _sync_enabled}       # noqa: SLF001 - 门禁/走查要按真按钮点
    return card


def task_detail_text(view, t) -> str:
    """把 `obs.drilldown.Drilldown.task_view()` 的返回渲染成**只读文本**（纯函数，便于自测）。

    字段名以 `task_view()` 的实际返回为准：`task.{state,target,attempts,throttles,transitions}`、
    `evidence[].{stage,decision,reason}`、`artifacts/pages/children`、`summary`、`found`。
    """
    if not isinstance(view, dict):
        return t("tasks.detail_failed", why=type(view).__name__)
    tid = str(view.get("task_id") or "")
    if not view.get("found"):
        return t("tasks.detail_gone", id=tid)
    task = dict(view.get("task") or {})
    lines = [t("tasks.detail_id", v=tid),
             t("tasks.detail_state", v=task.get("state") or "—"),
             t("tasks.detail_target", v=task.get("target") or "—"),
             t("tasks.detail_counters", a=task.get("attempts", 0), b=task.get("throttles", 0),
               c=task.get("transitions", 0)),
             t("tasks.detail_counts", art=len(view.get("artifacts") or []),
               pg=len(view.get("pages") or []), kids=len(view.get("children") or []))]
    if view.get("summary"):
        lines.append(t("tasks.detail_summary", v=view["summary"]))
    ev = list(view.get("evidence") or [])
    lines += ["", t("tasks.detail_evidence", n=len(ev))]
    if not ev:
        lines.append(t("tasks.detail_no_evidence"))
    for e in ev[:200]:                                  # 有界：证据再多也不把界面撑爆
        row = e if isinstance(e, dict) else {}
        lines.append(t("tasks.detail_evidence_line",
                       stage=row.get("stage") or "—",
                       decision=row.get("decision") or "—",
                       reason=(str(row.get("reason") or "")[:120] or "—")))
    return "\n".join(lines)


def _open_task_detail(page, ctx, table, row: int, t) -> None:
    """双击一行 → 只读详情对话框。**取不到 task_id 就什么都不做**（双击空白行）。"""
    from PySide6.QtCore import Qt as _Qt
    item = table.item(int(row), 0)
    tid = item.data(_Qt.ItemDataRole.UserRole) if item is not None else None
    if not tid:
        return
    dd = getattr(getattr(ctx, "engine", None), "drilldown", None)   # 只经 ctx（D5 纪律）
    if dd is None or not hasattr(dd, "task_view"):
        _toast(ctx, t("tasks.no_engine"), error=True)
        return
    try:
        view = dd.task_view(str(tid))
    except Exception as e:                              # 引擎侧出错也不许把界面带崩
        _toast(ctx, t("tasks.detail_failed", why=f"{type(e).__name__}: {e}"), error=True)
        return
    box = getattr(page, "_detail_view", None)
    dlg = getattr(page, "_detail_dialog", None)
    if dlg is None:
        from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout
        from qfluentwidgets import PushButton
        dlg = QDialog(table)
        dlg.setObjectName("taskDetailDialog")
        dl = QVBoxLayout(dlg)
        box = QPlainTextEdit(dlg)
        box.setObjectName("taskDetailView")
        box.setReadOnly(True)
        dl.addWidget(box)
        close = PushButton(t("tasks.detail_close"), dlg)
        close.clicked.connect(dlg.accept)
        dl.addWidget(close)
        page._detail_dialog = dlg                       # noqa: SLF001 - 复用同一个（不反复建控件）
        page._detail_view = box                         # noqa: SLF001
    dlg.setWindowTitle(t("tasks.detail_title"))
    dlg.resize(680, 460)
    box.setPlainText(task_detail_text(view, t))
    # **非模态**：`exec()` 在无头/无人值守环境会永久阻塞调用方（门禁与走查都会卡死）
    dlg.show()
    dlg.raise_()


# ── 日志 ─────────────────────────────────────────────────────────
def _log_line(record: logging.LogRecord) -> str:
    """`时间 级别 模块 消息` 的**紧凑单行**（界面要能一眼扫；JSON 是落盘用的，不放这儿）。

    级别在**第二列**——桥的 `_on_line` 按这个契约取色，改格式要一起改。
    """
    import time as _time
    name = str(record.name or "")
    short = name[len("daedalus."):] if name.startswith("daedalus.") else name
    try:
        msg = str(record.getMessage())
    except Exception as e:                       # 参数不匹配之类：如实标注，不抛
        msg = f"<unformattable: {type(e).__name__}>"
    return (f"{_time.strftime('%H:%M:%S', _time.localtime(record.created))} "
            f"{record.levelname:<7} {short} {msg}").rstrip()


class _QtLogHandler(logging.Handler):
    """把日志行投进界面的 handler。**线程安全**：只有 `emit()`，且只发 Qt 信号。

    工作线程里 `logging.warning(...)` 会走到这里；`Signal.emit` 跨线程是**排队投递**的
    （Qt 的 AutoConnection 看到接收者在 GUI 线程就换成 QueuedConnection），
    所以这里**绝不能**碰控件——直接在子线程 setText 是崩界面的经典写法。
    """

    def __init__(self, bridge, *, level=logging.INFO):
        super().__init__(level)
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:      # noqa: D102 - logging 的固定接口
        try:
            self._bridge.line.emit(_log_line(record))
        except Exception:
            pass          # 日志出口自己出错时**绝不**再记日志（会绕成回环）


def _make_log_bridge(view, tokens, t, *, autoscroll):
    """建日志桥（`QObject` + `Signal(str)`）：信号 → 本对象的方法（在 GUI 线程里跑）。

    两条线程纪律：① handler 只 `emit`，绝不碰控件；② 落界面在**接收者线程**（GUI）执行。
    返回的对象就是 `page._bridge`（对外可用：`push()` 是非 logging 行的入口）。
    """
    from PySide6.QtCore import QObject, Signal

    class _Bridge(QObject):
        line = Signal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._view = view
            self._tokens = tokens
            self._t = t                        # 文案取词器（脱敏失败的兜底句也要走 i18n）
            self._empty = None                 # 空态标签（由 `_logs` 建好后塞进来）
            self._notes_seen = 0
            self._broken = False               # 出口坏了就闭嘴（避免日志回环）
            self.line.connect(self._on_line)   # 同线程直连；跨线程自动排队

        # ── 非 logging 的行（界面提示等）也走同一条通道 ──────────
        def push(self, text: str, level: str = "INFO", source: str = "ui") -> None:
            import time as _time
            try:
                self.line.emit(f"{_time.strftime('%H:%M:%S')} {level:<7} {source} {text}")
            except Exception:
                pass

        def _on_line(self, text: str) -> None:
            try:
                self.append(text)
            except Exception:
                # ⚠️ 这里**不许写日志**：本对象就是日志出口，再记一笔会无限回环。
                self._broken = True

        def append(self, text: str) -> None:
            """落界面：**先脱敏再显示**（铁律：界面日志里绝不出现凭据）。"""
            if self._broken:
                return
            from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
            safe = _scrub(text, self._t("logs.scrub_failed"))
            st = _status_colors(self._tokens)
            fmt = QTextCharFormat()
            lvl = safe.split(" ", 2)[1] if safe.count(" ") >= 2 else ""
            if lvl in ("WARNING", "WARN"):
                fmt.setForeground(QColor(st["warn"]))       # 语义色：告警
            elif lvl in ("ERROR", "CRITICAL"):
                fmt.setForeground(QColor(st["error"]))      # 语义色：错误
            else:
                fmt.setForeground(QColor(_text_color(self._tokens)))
            cur = self._view.textCursor()          # 取一份副本，不动用户的选区
            cur.movePosition(QTextCursor.MoveOperation.End)
            cur.insertText(safe + "\n", fmt)
            if self._empty is not None:
                self._empty.setVisible(False)
            if autoscroll():
                bar = self._view.verticalScrollBar()
                bar.setValue(bar.maximum())

        def push_notes(self, notes: list, *, prefix: str, trimmed: str) -> int:
            """把 `ctx.notes` 里**新出现的**那几条镜进来；返回这次追加的条数。"""
            cur = list(notes or [])
            if len(cur) < self._notes_seen:        # 队列被裁剪过（UI.notify 到上限会丢前面的）
                self._notes_seen = len(cur)
                self.push(prefix + trimmed, "INFO")
                return 1
            fresh = cur[self._notes_seen:]
            self._notes_seen = len(cur)
            for m in fresh:
                self.push(prefix + str(m), "INFO")
            return len(fresh)

    return _Bridge(view)


def _scrub(text, fallback: str) -> str:
    """出口脱敏：**用 `obs.policy` 对外的脱敏函数**（不自己写一套）。

    `scrub_log` 里两层：凭据形态（`token=` / `Bearer` / header 转储）**永远脱**，
    可配的 URL 参数与正文隐私按策略开关。默认策略 = 日志类全开。
    """
    from daedalus.obs.policy import SanitizationPolicy
    try:
        return SanitizationPolicy().scrub_log(str(text))
    except Exception:                    # 脱敏自己出错时宁可少显示，也不放原文出去
        return str(fallback)


def _status_colors(tokens) -> dict:
    from daedalus.ui.theme import status_colors
    return status_colors(tokens.light)


def _text_color(tokens) -> str:
    from daedalus.ui.theme import text_color
    return text_color(tokens.light)


def _logs(window, tokens, ctx, t):
    page, lay = PageBase.make(tokens, name="logs", parent=window)
    at = _header(page, lay, tokens, t, "logs")
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(t("logs.hint"), tokens))

    # 工具行：清空 + 自动滚动（绑设置项 `log_autoscroll`）
    from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit
    from qfluentwidgets import PushButton, SwitchButton
    store = getattr(ctx, "settings", None) if ctx is not None else None

    def _autoscroll() -> bool:
        if store is None:
            return True
        return bool(store.get("log_autoscroll"))

    tools = QHBoxLayout()
    clear = PushButton(t("logs.clear"), card)
    tools.addWidget(clear)
    tools.addStretch(1)
    tools.addWidget(_label(t("logs.autoscroll"), tokens))
    sw = SwitchButton(card)
    sw.setChecked(_autoscroll())
    sw.checkedChanged.connect(
        lambda v: _apply(store, ctx, {"log_autoscroll": bool(v)}, t))
    tools.addWidget(sw)
    cl.addLayout(tools)

    view = QPlainTextEdit(card)
    view.setObjectName("logView")
    view.setReadOnly(True)
    view.setMaximumBlockCount(LOG_VIEW_MAX)      # 有界：跑一天也不会把内存吃光
    view.setMinimumHeight(360)
    view.setStyleSheet("background: transparent; border: none;"
                       " font-family: Consolas, 'Cascadia Mono', monospace;"
                       f" font-size: {max(9, int(tokens.font_pt) - 1)}pt;")
    cl.addWidget(view)
    empty = _label(t("logs.empty"), tokens, muted=True)
    empty.setObjectName("logsEmpty")
    cl.addWidget(empty)

    # 桥 + handler：日志（含工作线程的）→ 界面。**先脱敏，后显示**
    bridge = _make_log_bridge(view, tokens, t, autoscroll=_autoscroll)
    bridge._empty = empty                        # noqa: SLF001 - 有内容就收起空态
    handler = _QtLogHandler(bridge, level=logging.INFO)
    # 打上 `_daedalus_handler` 标记：引擎装配日志（obs/logs.py 会清掉「别人挂的」handler）
    # 时不会把面板 handler 摘掉。
    handler._daedalus_handler = "ui"             # noqa: SLF001
    root = logging.getLogger()
    # **同一窗口只留一个面板 handler**：切语言会整页重建（旧页会被删），不在这里摘掉的话，
    # 每切一次语言就多挂一个 handler：日志行重复投递到已经删除的旧页（Python 侧还抱着
    # 已销毁的控件引用）。所以按**窗口**记账，重建时先把上一个摘掉。
    prev = list(getattr(window, "_daedalus_ui_log_handlers", None) or [])
    for old in prev:
        try:
            root.removeHandler(old)
            old.close()
        except Exception:
            pass
    root.addHandler(handler)
    window._daedalus_ui_log_handlers = [handler]     # noqa: SLF001 - 窗口级记账（见上）

    def _detach(*_a) -> None:
        try:
            root.removeHandler(handler)
            handler.close()
        except Exception:
            pass

    page.destroyed.connect(lambda *_a: _detach())   # 页面被删（切语言重建）→ 摘掉，不留野 handler
    clear.clicked.connect(lambda: _clear_logs(view, empty))

    # 界面自己的提示（底图不可用/设置被拒/取色失败…）：**它们的唯一出口**
    def _poll_notes() -> None:
        notes = getattr(ctx, "notes", None) if ctx is not None else None
        if not notes:
            return
        bridge.push_notes(list(notes), prefix=t("logs.ui_prefix"), trimmed=t("logs.ui_trimmed"))

    from PySide6.QtCore import QTimer
    timer = QTimer(page)
    timer.setInterval(NOTES_POLL_MS)
    timer.timeout.connect(_poll_notes)
    timer.start()

    lay.insertWidget(at, card)
    page._view = view               # noqa: SLF001 - 门禁/走查按这个名字找它
    page._handler = handler         # noqa: SLF001 - 便于卸载与测试
    page._bridge = bridge           # noqa: SLF001
    page._empty_label = empty       # noqa: SLF001
    page._notes_timer = timer       # noqa: SLF001
    page._clear_logs = lambda: _clear_logs(view, empty)   # noqa: SLF001
    return page


def _clear_logs(view, empty) -> None:
    """清空面板：空态重新出现。

    ⚠️ **不重置** `_notes_seen` 游标——`ctx.notes` 是累积的，重置会把旧提示整段重放一遍。
    """
    view.clear()
    empty.setVisible(True)


# ── 设置（固定在最下面）───────────────────────────────────────────
def _settings(window, tokens, ctx, t):
    page, lay = PageBase.make(tokens, name="settings", parent=window)
    at = _header(page, lay, tokens, t, "settings")
    lay.insertWidget(at, _label(t("settings.intro"), tokens))
    store = getattr(ctx, "settings", None) if ctx is not None else None
    widgets: dict = {}

    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
                                   QLineEdit, QPushButton, QSlider, QSpinBox)
    from PySide6.QtWidgets import QInputDialog
    from daedalus.ui.i18n import LOCALE_AUTO, LOCALE_NAMES, LOCALES
    from daedalus.ui.theme import ACCENT_PRESETS

    def _add_card(title_key: str):
        card = GlassCard.make(tokens, name="settingsCard")
        cl = GlassCard.body(card, tokens)
        title = _label(t(title_key), tokens)
        title.setStyleSheet(f"color: {tokens.accent}; font-weight: 700;")
        cl.addWidget(title)
        lay.insertWidget(max(0, lay.count() - 1), card)
        return card, cl

    for group_key, fields in _SETTINGS_GROUPS:
        card, cl = _add_card(group_key)
        for key, label_key, kind in fields:
            row = QHBoxLayout()
            row.addWidget(_label(t(label_key), tokens))
            cur = store.get(key) if store is not None else None
            if kind == "bool":
                w = QCheckBox(card)
                w.setChecked(bool(cur) if cur is not None else False)
                w.toggled.connect(lambda v, k=key: _apply(store, ctx, {k: bool(v)}, t))
            elif kind == "int":
                lo, hi = {"panel_alpha": (40, 95), "blur": (0, 30), "dim_manual": (0, 60),
                          "downsample_max": (480, 7680), "fade_ms": (0, 2000),
                          "debounce_ms": (0, 2000), "fps_cap": (5, 120)}.get(key, (0, 10000))
                if key in ("panel_alpha", "blur", "dim_manual"):
                    from PySide6.QtCore import Qt as _Qt
                    w = QSlider(_Qt.Orientation.Horizontal, card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}, t))
                else:
                    w = QSpinBox(card)
                    w.setRange(lo, hi)
                    w.setValue(int(cur) if cur is not None else lo)
                    w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: int(v)}, t))
            elif kind == "float":
                w = QDoubleSpinBox(card)
                w.setRange(10.0, 24.0)
                w.setSingleStep(0.5)
                w.setValue(float(cur) if cur is not None else 10.0)
                w.valueChanged.connect(lambda v, k=key: _apply(store, ctx, {k: float(v)}, t))
            elif kind == "accent":
                w = QComboBox(card)
                for nm, hx in ACCENT_PRESETS:
                    w.addItem(f"{nm} {hx}", hx)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            elif kind == "choice":
                w = QComboBox(card)
                for val in ("compact", "standard", "relaxed"):
                    w.addItem(t(f"density.{val}"), val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            elif kind == "focus":
                w = QComboBox(card)
                for val in ("center", "top", "bottom", "left", "right"):
                    w.addItem(t(f"focus.{val}"), val)
                if cur:
                    idx = w.findData(cur)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(
                    lambda _i, k=key, cb=w: _apply(store, ctx, {k: cb.currentData()}, t))
            else:                                   # file：底图路径 + "选择…"
                box = QHBoxLayout()
                w = QLineEdit(card)
                w.setText(str(cur or ""))
                w.setPlaceholderText(t("settings.wallpaper_placeholder"))
                pick = QPushButton(t("settings.pick"), card)
                pick.clicked.connect(lambda _c=False, e=w: _pick_file(e, t))
                w.editingFinished.connect(lambda k=key, e=w: _apply(store, ctx, {k: e.text()}, t))
                box.addWidget(w)
                box.addWidget(pick)
                row.addLayout(box)
                cl.addLayout(row)
                widgets[key] = w
                continue
            row.addWidget(w)
            cl.addLayout(row)
            widgets[key] = w

    # 语言（**放在外观里**：机主要「装完就是本机语言」，这里也能手动改，即改即生效）
    card, cl = _add_card("settings.group.other")
    row = QHBoxLayout()
    row.addWidget(_label(t("settings.language"), tokens))
    lang = QComboBox(card)
    lang.addItem(t("settings.language_auto"), LOCALE_AUTO)
    for loc in LOCALES:
        lang.addItem(LOCALE_NAMES.get(loc, loc), loc)
    cur_loc = str(store.get("locale") or "") if store is not None else ""
    idx = lang.findData(cur_loc)
    lang.setCurrentIndex(idx if idx >= 0 else 0)
    lang.currentIndexChanged.connect(
        lambda _i, cb=lang: _apply(store, ctx, {"locale": cb.currentData()}, t))
    row.addWidget(lang)
    cl.addLayout(row)
    widgets["locale"] = lang

    # 预设：保存 / 加载 / 导入导出
    pre = GlassCard.make(tokens, name="settingsCard")
    pl = GlassCard.body(pre, tokens)
    pl.addWidget(_label(t("settings.presets"), tokens))
    from daedalus.ui.settings import FACTORY_PRESETS
    for name in FACTORY_PRESETS:
        btn = QPushButton(t("settings.load_preset", name=name), pre)
        btn.clicked.connect(lambda _c=False, n=name: _load_preset(store, ctx, n, t))
        pl.addWidget(btn)

    def _save_current():
        if store is None:
            return
        name, okk = QInputDialog.getText(pre, t("settings.save_preset_title"),
                                        t("settings.preset_name"))
        if okk and name.strip():
            store.save_preset(name.strip())
            _toast(ctx, t("toast.preset_saved", name=name.strip()))

    b1 = QPushButton(t("settings.save_preset"), pre)
    b1.clicked.connect(_save_current)
    b2 = QPushButton(t("settings.export"), pre)
    b2.clicked.connect(lambda: _export_presets(store, ctx, t))
    b3 = QPushButton(t("settings.import"), pre)
    b3.clicked.connect(lambda: _import_presets(store, ctx, t))
    for b in (b1, b2, b3):
        pl.addWidget(b)
    lay.insertWidget(max(0, lay.count() - 1), pre)
    page._widgets = widgets         # noqa: SLF001
    return page


def _apply(store, ctx, patch: dict, t) -> None:
    """设置变更的统一入口：校验 → 保存 → 让窗口重刷（**即改即存**）。"""
    from daedalus.ui.theme import ThemeError
    try:
        if store is not None:
            store.update(patch)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(patch)
    except ThemeError as e:
        _toast(ctx, t("toast.setting_rejected", why=e), error=True)


def _load_preset(store, ctx, name: str, t) -> None:
    from daedalus.ui.theme import ThemeError
    try:
        body = store.load_preset(name)
        if ctx is not None and hasattr(ctx, "on_settings_changed"):
            ctx.on_settings_changed(body)
        _toast(ctx, t("toast.preset_loaded", name=name))
    except ThemeError as e:
        _toast(ctx, str(e), error=True)


def _export_presets(store, ctx, t) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getSaveFileName(None, t("dialog.export_presets"),
                                      "daedalus-presets.json", f"{t('dialog.json')} (*.json)")
    if p:
        _toast(ctx, t("toast.exported", n=store.export_presets(p)["count"]))


def _import_presets(store, ctx, t) -> None:
    if store is None:
        return
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, t("dialog.import_presets"), "",
                                      f"{t('dialog.json')} (*.json)")
    if p:
        try:
            r = store.import_presets(p)
            _toast(ctx, t("toast.imported", n=r["imported"], skip=r["skipped"]))
        except Exception as e:
            _toast(ctx, t("toast.import_failed", why=e), error=True)


def _pick_file(edit, t) -> None:
    """背景图选文件：选完**立刻生效**（走 `editingFinished` → `_apply` → 重刷）。"""
    from PySide6.QtWidgets import QFileDialog
    p, _ = QFileDialog.getOpenFileName(None, t("dialog.pick_wallpaper"), "",
                                       f"{t('dialog.images')} (*.png *.jpg *.jpeg *.webp *.bmp)")
    if p:
        edit.setText(p)
        edit.editingFinished.emit()


def _toast(ctx, msg: str, *, error: bool = False) -> None:
    """提示出口：有 ctx 就交给它；没有就记日志（**不静默**）。"""
    if ctx is not None and hasattr(ctx, "notify"):
        try:
            ctx.notify(str(msg), error=error)
            return
        except Exception:
            pass
    logger.warning("界面提示：%s", msg)


# ── 关于 ─────────────────────────────────────────────────────────
def _about(window, tokens, t):
    page, lay = PageBase.make(tokens, name="about", parent=window)
    at = _header(page, lay, tokens, t, "about")
    from daedalus import about
    info = about()
    card = GlassCard.make(tokens, name="card")
    cl = GlassCard.body(card, tokens)
    cl.addWidget(_label(f"{info['name']}（{info['name_zh']} / {info['name_ja']}）v{info['version']}",
                        tokens))
    cl.addWidget(_label(info["tagline_zh"] if t.locale == "zh-CN" else
                        (info["tagline_ja"] if t.locale == "ja-JP" else info["tagline_en"]),
                        tokens, muted=True))
    cl.addWidget(_label(t("about.body1"), tokens))
    cl.addWidget(_label(t("about.body2"), tokens, muted=True))
    lay.insertWidget(at, card)
    return page

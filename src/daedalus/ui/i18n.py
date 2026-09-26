# -*- coding: utf-8 -*-
"""界面多语言：**语言解析顺序 + 三语文案表**

机主的硬要求（原话）：**「确保你正常安装之后它会给你本机系统上的语言」** ——
安装时选了简体中文，装完就绝不能蹦出日文。所以语言不能写死在代码里，必须按固定顺序解析：

    ① 用户在设置里显式选过 → 用它（`zh-CN` / `ja-JP` / `en-US` / 空=跟随系统）
    ② 安装器当时选的（安装目录 `install.marker` 里的 `lang=<LCID>`）→ 用它
    ③ 本机系统 UI 语言（Windows `GetUserDefaultUILanguage`）→ 用它
    ④ 兜底 `en-US`（**不是** zh-CN：对非中文用户，英文比中文更可能被看懂）

三条纪律：
  * **缺翻译要说出来**：`Translator.missing` 记录没命中的键，界面自检能读到——
    不允许「某句话悄悄变成另一种语言」（那正是机主说的「愚蠢的问题」）；
  * **不猜系统语言**：`GetUserDefaultUILanguage` 拿不到就往下走，不靠 `LANG` 之类的偶然值拍板；
  * 语言**即改即生效**（和外观设置一样，没有「应用」按钮）。
"""

from __future__ import annotations

import ctypes
import logging
import os
import pathlib

logger = logging.getLogger(__name__)

__all__ = ["LOCALES", "LOCALE_NAMES", "detect_system_locale", "read_installer_locale",
           "resolve_locale", "Translator", "translator", "LOCALE_AUTO"]

LOCALES = ("zh-CN", "ja-JP", "en-US")
LOCALE_AUTO = ""                      # 设置里的"跟随系统"
LOCALE_NAMES = {"zh-CN": "简体中文", "ja-JP": "日本語", "en-US": "English"}

# Windows LCID（主语言 ID）→ 我们的语言标签。只登记我们能显示的三语；
# 其它语言一律落到 en-US（比落到中文更合适：非中文用户看英文更可能看懂）。
_LCID_MAP = {
    0x0004: "zh-CN",       # zh-Hans
    0x7804: "zh-CN",       # zh-Hans (legacy)
    0x1004: "zh-CN",       # zh-SG（用简体）
    0x0404: "zh-CN",       # zh-Hant → 暂时也用简体（没有繁体表；宁可显示可读的中文）
    0x0C04: "zh-CN",       # zh-HK
    0x1404: "zh-CN",       # zh-MO
    0x7C04: "zh-CN",       # zh-Hant legacy
    0x0411: "ja-JP",       # ja
    0x0409: "en-US",       # en-US
    0x0809: "en-US",       # en-GB
    0x0C09: "en-US",       # en-AU
    0x1009: "en-US",       # en-CA
}
_CJK_PREFIX = ("zh", "ja")


def detect_system_locale() -> str:
    """本机系统 UI 语言（拿不到就返回 en-US，**不猜**）。"""
    if os.name == "nt":
        try:
            lcid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if lcid in _LCID_MAP:
                return _LCID_MAP[lcid]
            # 未知语言：按主语言号判断是不是 CJK（是我们没登记的变体 → 仍给中文/日文表）
            primary = lcid & 0x3FF
            if primary == 0x04:
                return "zh-CN"
            if primary == 0x11:
                return "ja-JP"
            logger.info("系统语言 LCID 0x%04X 未登记 → 用 en-US", lcid)
            return "en-US"
        except Exception as e:
            logger.debug("读系统 UI 语言失败：%s", e)
    try:
        import locale as _locale
        tag = (_locale.getlocale()[0] or "")
        if tag:
            low = tag.lower()
            if low.startswith("zh"):
                return "zh-CN"
            if low.startswith("ja"):
                return "ja-JP"
            if low.startswith("en"):
                return "en-US"
    except Exception:
        pass
    return "en-US"


def marker_path(exe_dir=None) -> pathlib.Path:
    """安装标记文件（安装器写的）。未打包时看当前目录（源码态/便携态都适用）。"""
    import sys
    base = pathlib.Path(str(exe_dir)) if exe_dir else (
        pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False)
        else pathlib.Path.cwd())
    return base / "install.marker"


def read_installer_locale(exe_dir=None) -> str:
    """读安装器当时选的语言（`install.marker` 的 `lang=<LCID>`）。读不到返回空串。"""
    p = marker_path(exe_dir)
    try:
        if not p.exists():
            return ""
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip().lower() == "lang":
                try:
                    lcid = int(str(v).strip())
                except Exception:
                    continue
                if lcid in _LCID_MAP:
                    return _LCID_MAP[lcid]
                primary = lcid & 0x3FF
                if primary == 0x04:
                    return "zh-CN"
                if primary == 0x11:
                    return "ja-JP"
                return "en-US"
    except Exception as e:
        logger.debug("读安装器语言失败：%s", e)
    return ""


def resolve_locale(settings_value: str = "", *, exe_dir=None) -> str:
    """按 ①设置 ②安装器 ③系统 ④en-US 的顺序定语言（见文件头）。"""
    v = str(settings_value or "").strip()
    if v in LOCALES:
        return v
    v = read_installer_locale(exe_dir)
    if v in LOCALES:
        return v
    return detect_system_locale()


# ══════════════════════════════════════════════════════════════════
# 文案表（键 → 三语）。**界面里不许再出现硬编码的可视文案**：
# 新加一句界面文字 = 在这里加一个键（门禁会扫 UI 文件里是否还有裸中文）。
# ══════════════════════════════════════════════════════════════════
_TABLE: dict[str, dict[str, str]] = {
    # 应用
    "app.title": {"zh-CN": "统一采集与感知引擎", "ja-JP": "統合収集・観測エンジン",
                  "en-US": "Unified Acquisition & Observation Engine"},
    "app.signature": {"zh-CN": "{name} v{version}", "ja-JP": "{name} v{version}",
                      "en-US": "{name} v{version}"},
    # 导航
    "nav.overview": {"zh-CN": "概览", "ja-JP": "概要", "en-US": "Overview"},
    "nav.tasks": {"zh-CN": "任务", "ja-JP": "タスク", "en-US": "Tasks"},
    "nav.logs": {"zh-CN": "日志", "ja-JP": "ログ", "en-US": "Logs"},
    "nav.about": {"zh-CN": "关于", "ja-JP": "情報", "en-US": "About"},
    "nav.settings": {"zh-CN": "设置", "ja-JP": "設定", "en-US": "Settings"},
    # 页面副标题（标题下那行 12px 灰字：**破掉「一上来就是卡」的拥挤感**，
    # 顺便说清这一页能干什么。库的 FluentWindow 不提供页面标题区，所以标题+副标题都归页面自己）
    "page.overview.sub": {"zh-CN": "指标每秒刷新一次；数字会滚动到新值，不是硬切",
                          "ja-JP": "指標は毎秒更新。数値は切り替わらず、新しい値まで流れるように動く",
                          "en-US": "Metrics refresh once a second; numbers roll to the new value "
                                   "instead of snapping"},
    "page.tasks.sub": {"zh-CN": "双击任意一行看这个任务的完整判断链",
                       "ja-JP": "任意の行をダブルクリックすると、そのタスクの判断の連鎖を最後まで見られる",
                       "en-US": "Double-click any row to see that task's full decision chain"},
    "page.logs.sub": {"zh-CN": "界面日志与引擎日志同源，先脱敏后显示；上限 5000 行",
                      "ja-JP": "画面のログはエンジンのログと同源。マスクしてから表示し、上限は 5000 行",
                      "en-US": "UI and engine logs share one source, masked before display, "
                               "capped at 5000 lines"},
    "page.settings.sub": {"zh-CN": "改哪一项都立刻生效，没有「应用」按钮；所有卡片由同一个生成器出样式",
                          "ja-JP": "どの項目も即時反映（適用ボタンなし）。カードはすべて同一の生成器から",
                          "en-US": "Every change takes effect immediately (no Apply button); "
                                   "all cards come from one style generator"},
    "page.about.sub": {"zh-CN": "这是什么、边界在哪里、当前版本",
                       "ja-JP": "これは何か、境界はどこか、現在のバージョン",
                       "en-US": "What this is, where its boundaries are, and the current version"},
    # 概览
    "stat.pages_per_sec": {"zh-CN": "页/秒", "ja-JP": "ページ/秒", "en-US": "pages/s"},
    "stat.mb_per_sec": {"zh-CN": "MB/s", "ja-JP": "MB/s", "en-US": "MB/s"},
    "stat.tasks_ok_fail": {"zh-CN": "任务 成功/失败", "ja-JP": "タスク 成功/失敗",
                           "en-US": "tasks ok/fail"},
    "stat.request_p95": {"zh-CN": "请求 p95", "ja-JP": "リクエスト p95", "en-US": "request p95"},
    "overview.plan": {"zh-CN": "资源计划：{text}", "ja-JP": "リソース計画：{text}",
                      "en-US": "Resource plan: {text}"},
    "overview.plan_live": {"zh-CN": "（运行中的计划）", "ja-JP": "（実行中の計画）",
                           "en-US": "(live plan from the running engine)"},
    "overview.plan_default": {"zh-CN": "（出厂默认计划——引擎未启动）",
                              "ja-JP": "（出荷時の既定計画——エンジン未起動）",
                              "en-US": "(factory-default plan — engine not running)"},
    "overview.plan_note": {"zh-CN": "队列全有界｜写盘单线程 + 批提交｜缺省即拒绝",
                           "ja-JP": "全キューに上限｜書き込みは単一スレッド＋バッチ｜既定は拒否",
                           "en-US": "All queues bounded | single writer + batch commit | deny by default"},
    # 快速采集（概览页）——界面上的**唯一一条能干活的路径**，所以每条状态都要说人话
    "collect.title": {"zh-CN": "快速采集：一行一个目标，开始就行",
                      "ja-JP": "クイック収集：1 行に 1 つ、開始するだけ",
                      "en-US": "Quick collect: one target per line, then press Start"},
    "collect.placeholder": {"zh-CN": "https://example.com/\nhttps://example.org/docs",
                            "ja-JP": "https://example.com/\nhttps://example.org/docs",
                            "en-US": "https://example.com/\nhttps://example.org/docs"},
    "collect.start": {"zh-CN": "开始采集", "ja-JP": "収集開始", "en-US": "Start"},
    "collect.stop": {"zh-CN": "停止", "ja-JP": "停止", "en-US": "Stop"},
    "collect.idle": {"zh-CN": "空闲。开始后这里的指标会每秒钟刷新。",
                     "ja-JP": "待機中。開始すると指標が毎秒更新されます。",
                     "en-US": "Idle. Metrics refresh once per second while running."},
    "collect.running": {"zh-CN": "运行中 {n} 秒……（停止是「下一轮不再领活」，已领的活会做完或超时回队列）",
                        "ja-JP": "実行中 {n} 秒……（停止は「次回以降は取らない」；取得済みは完了か期限切れでキューへ戻る）",
                        "en-US": "Running {n}s… (stop means no new claims; in-flight work finishes or returns via lease timeout)"},
    "collect.stopping": {"zh-CN": "正在停止……", "ja-JP": "停止しています……", "en-US": "Stopping…"},
    "collect.done": {"zh-CN": "采集结束：成功 {n}｜失败 {f} {why}",
                     "ja-JP": "収集終了：成功 {n}｜失敗 {f} {why}",
                     "en-US": "Done: {n} ok | {f} failed {why}"},
    "collect.stopped": {"zh-CN": "（被手动停止）", "ja-JP": "（手動停止）", "en-US": "(stopped by user)"},
    "collect.failed": {"zh-CN": "采集失败：{err}", "ja-JP": "収集失敗：{err}", "en-US": "Collect failed: {err}"},
    "collect.need_target": {"zh-CN": "先贴至少一个目标（一行一个）",
                            "ja-JP": "ターゲットを 1 つ以上入れてください（1 行に 1 つ）",
                            "en-US": "Add at least one target (one per line)"},
    "collect.no_engine": {"zh-CN": "引擎未启动：只能看，不能采。请从命令行启动界面（daedalus ui）。",
                          "ja-JP": "エンジン未起動：閲覧のみ。CLI から起動してください（daedalus ui）。",
                          "en-US": "Engine not running: view-only. Start the UI from the CLI (daedalus ui)."},
    # 快速采集的「导入 TXT」——机主原话：「搞一个直接提取 txt 里面的链接…一行一个…
    # 断掉的链接排除掉…小分类过滤…只适配 txt 就行」。计数口径：N=导入、M=无效排除、
    # D=去重、K=域名数；三个数字都必须**如实**出现（宁可报 0 也不含糊）。
    "collect.import_txt": {"zh-CN": "导入 TXT", "ja-JP": "TXT を読み込む", "en-US": "Import TXT"},
    "collect.import_done": {"zh-CN": "导入 {n} 条（排除 {m} 条无效，去重 {d} 条），{k} 个域名",
                            "ja-JP": "{n} 件を取り込み（無効 {m} 件を除外、重複 {d} 件を削除）、{k} ドメイン",
                            "en-US": "Imported {n} ({m} invalid excluded, {d} duplicates), {k} domains"},
    "collect.import_failed": {"zh-CN": "导入失败：{err}", "ja-JP": "読み込み失敗：{err}",
                              "en-US": "Import failed: {err}"},
    "collect.import_none": {"zh-CN": "「{name}」里没有找到可用链接",
                            "ja-JP": "「{name}」に利用できるリンクが見つかりません",
                            "en-US": "No usable links found in “{name}”"},
    "dialog.pick_txt": {"zh-CN": "选择链接清单（TXT）", "ja-JP": "リンクリスト（TXT）を選ぶ",
                        "en-US": "Choose link list (TXT)"},
    "dialog.txt_filter": {"zh-CN": "文本文件", "ja-JP": "テキストファイル", "en-US": "Text files"},
    # 任务
    "tasks.hint": {"zh-CN": "任务下钻：每一行都能说清「看到了什么 → 决定了什么 → 为什么」",
                   "ja-JP": "タスク詳細：各行が「何を見て → 何を決め → なぜか」を説明できる",
                   "en-US": "Drill-down: every row explains what was seen, decided, and why"},
    # 任务页的工具行（三个真操作）
    "tasks.tools": {"zh-CN": "选中一行或多行，然后：",
                    "ja-JP": "行を選んでから：",
                    "en-US": "Select one or more rows, then:"},
    "tasks.retry": {"zh-CN": "重试失败的任务", "ja-JP": "失敗したタスクを再試行",
                    "en-US": "Retry failed"},
    "tasks.export": {"zh-CN": "导出选中结果", "ja-JP": "選択をエクスポート", "en-US": "Export selected"},
    "tasks.forget": {"zh-CN": "删除任务记录", "ja-JP": "タスク記録を削除", "en-US": "Delete records"},
    "tasks.tools_hint": {"zh-CN": "重试只是重新排队（不超过 5 次）；删除只删任务/证据行，**原始捕获一个字节都不动**。",
                         "ja-JP": "再試行は再キューイングのみ（最大 5 回）。削除はタスク/証拠行だけで、生データには触れません。",
                         "en-US": "Retry only re-queues (max 5). Delete removes task/evidence rows only — raw captures are never touched."},
    "tasks.pick_row": {"zh-CN": "先选中至少一行（点行首或按住 Ctrl 多选）",
                       "ja-JP": "まず 1 行以上選んでください（Ctrl で複数選択）",
                       "en-US": "Select at least one row first (Ctrl for multiple)"},
    "tasks.no_engine": {"zh-CN": "引擎未启动：这三个操作需要引擎（从 `daedalus ui` 启动）",
                        "ja-JP": "エンジン未起動：この 3 つはエンジンが必要です（`daedalus ui` から起動）",
                        "en-US": "Engine not running: these three need the engine (start from `daedalus ui`)"},
    "tasks.retry_done": {"zh-CN": "已重新入队 {n}/{total} 条{why}",
                         "ja-JP": "{n}/{total} 件を再キューイングしました{why}",
                         "en-US": "Re-queued {n}/{total}{why}"},
    "tasks.retry_failed": {"zh-CN": "重试失败：{err}", "ja-JP": "再試行に失敗：{err}",
                           "en-US": "Retry failed: {err}"},
    "tasks.export_done": {"zh-CN": "已导出 {n} 条任务的记录到 {path}（与 CLI export 同一实现，已过脱敏）",
                          "ja-JP": "{n} 件を {path} にエクスポートしました（CLI export と同一実装・マスク済み）",
                          "en-US": "Exported {n} task(s) to {path} (same path as CLI export, sanitized)"},
    "tasks.export_failed": {"zh-CN": "导出失败：{err}", "ja-JP": "エクスポート失敗：{err}",
                            "en-US": "Export failed: {err}"},
    "tasks.forget_title": {"zh-CN": "删除任务记录", "ja-JP": "タスク記録の削除", "en-US": "Delete task records"},
    "tasks.forget_ask": {"zh-CN": "要删除选中的 {n} 条任务记录吗？\n\n"
                                  "会被删掉：任务行、证据链、错误行。\n"
                                  "**不会被删**：原始捕获（raw_artifacts）、派生层、已下载的文件。\n\n"
                                  "删除会记进结构化日志（可追溯）。",
                         "ja-JP": "選択した {n} 件のタスク記録を削除しますか？\n\n"
                                  "削除される：タスク行・証拠チェーン・エラー行。\n"
                                  "**削除されない**：生データ（raw_artifacts）・派生層・ダウンロード済みファイル。\n\n"
                                  "削除は構造化ログに記録されます（追跡可能）。",
                         "en-US": "Delete {n} selected task record(s)?\n\n"
                                  "Removed: task rows, evidence chain, error rows.\n"
                                  "**Never removed**: raw captures (raw_artifacts), derived layer, downloaded files.\n\n"
                                  "Deletions are recorded in the structured log (traceable)."},
    "tasks.forget_done": {"zh-CN": "已删除任务记录 {n} 条（证据 {ev} 行；原始层未动。"
                                  "预检显示会删 {dry} 条）",
                          "ja-JP": "タスク記録 {n} 件を削除（証拠 {ev} 行；生データは未変更。事前確認：{dry} 件）",
                          "en-US": "Deleted {n} task record(s) (evidence {ev}); raw untouched. Dry-run said {dry}"},
    "tasks.forget_failed": {"zh-CN": "删除失败：{err}", "ja-JP": "削除失敗：{err}",
                            "en-US": "Delete failed: {err}"},
    "tasks.col.state": {"zh-CN": "状态", "ja-JP": "状態", "en-US": "state"},
    "tasks.col.target": {"zh-CN": "目标", "ja-JP": "対象", "en-US": "target"},
    "tasks.col.counters": {"zh-CN": "尝试/限流/转移", "ja-JP": "試行/制限/遷移",
                           "en-US": "try/throttle/move"},
    "tasks.col.evidence": {"zh-CN": "证据", "ja-JP": "証拠", "en-US": "evidence"},
    "tasks.col.fingerprint": {"zh-CN": "指纹", "ja-JP": "指紋", "en-US": "fingerprint"},
    "tasks.col.bytes": {"zh-CN": "字节", "ja-JP": "バイト", "en-US": "bytes"},
    "tasks.empty": {"zh-CN": "还没有任务。用 `daedalus-cli collect <目标>` 跑一次，"
                             "这里就会出现每一行的状态与指纹。",
                    "ja-JP": "タスクはまだありません。`daedalus-cli collect <対象>` を一度"
                             "実行すると、各行の状態と指紋がここに並びます。",
                    "en-US": "No tasks yet. Run `daedalus-cli collect <target>` once and every "
                             "row's state and fingerprint will show up here."},
    "tasks.no_engine": {"zh-CN": "引擎未启动，无法下钻", "ja-JP": "エンジン未起動のため詳細を開けません",
                        "en-US": "Engine is not running — cannot drill down"},
    "tasks.detail_title": {"zh-CN": "任务详情", "ja-JP": "タスク詳細", "en-US": "Task detail"},
    "tasks.detail_id": {"zh-CN": "任务 id：{v}", "ja-JP": "タスク id：{v}", "en-US": "task id: {v}"},
    "tasks.detail_state": {"zh-CN": "状态：{v}", "ja-JP": "状態：{v}", "en-US": "state: {v}"},
    "tasks.detail_target": {"zh-CN": "目标：{v}", "ja-JP": "対象：{v}", "en-US": "target: {v}"},
    "tasks.detail_counters": {"zh-CN": "尝试 {a} · 限流 {b} · 转移 {c}",
                              "ja-JP": "試行 {a} · 制限 {b} · 遷移 {c}",
                              "en-US": "attempts {a} · throttles {b} · transitions {c}"},
    "tasks.detail_counts": {"zh-CN": "原始 {art} 份 · 派生 {pg} 条 · 子任务 {kids}",
                            "ja-JP": "原本 {art} 件 · 派生 {pg} 件 · 子タスク {kids}",
                            "en-US": "artifacts {art} · derived pages {pg} · children {kids}"},
    "tasks.detail_summary": {"zh-CN": "摘要：{v}", "ja-JP": "要約：{v}", "en-US": "summary: {v}"},
    "tasks.detail_evidence": {"zh-CN": "证据链（{n} 条）：看到什么 → 决定了什么 → 为什么",
                              "ja-JP": "証拠チェーン（{n} 件）：何を見て → 何を決め → なぜ",
                              "en-US": "Evidence ({n}): what was seen → what was decided → why"},
    "tasks.detail_evidence_line": {"zh-CN": "{stage} · {decision} · {reason}",
                                   "ja-JP": "{stage} · {decision} · {reason}",
                                   "en-US": "{stage} · {decision} · {reason}"},
    "tasks.detail_no_evidence": {"zh-CN": "（这一条还没有证据记录）",
                                 "ja-JP": "（このタスクにはまだ証拠の記録がありません）",
                                 "en-US": "(no evidence recorded for this task yet)"},
    "tasks.detail_gone": {"zh-CN": "没有这个任务：{id}（可能还没入队，或已被清理）",
                          "ja-JP": "そのタスクはありません：{id}（未登録か、既に整理された可能性）",
                          "en-US": "No such task: {id} (not enqueued yet, or already cleaned up)"},
    "tasks.detail_failed": {"zh-CN": "下钻失败：{why}", "ja-JP": "詳細の取得に失敗：{why}",
                            "en-US": "Drill-down failed: {why}"},
    "tasks.detail_close": {"zh-CN": "关闭", "ja-JP": "閉じる", "en-US": "Close"},
    # 日志
    "logs.hint": {"zh-CN": "结构化日志：落盘是 JSON 行（字段稳定、可直接喂 jq），"
                           "面板是紧凑单行；两者都先脱敏再显示",
                  "ja-JP": "構造化ログ：保存は JSON 行（フィールド安定・jq 可）、"
                           "パネルは 1 行の簡潔表示。どちらもマスク後に表示",
                  "en-US": "Structured logs: JSON lines on disk (stable fields, jq-friendly), "
                           "compact one-liners in this panel; both masked before display"},
    "logs.clear": {"zh-CN": "清空", "ja-JP": "消去", "en-US": "Clear"},
    "logs.autoscroll": {"zh-CN": "自动滚动", "ja-JP": "自動スクロール", "en-US": "Auto-scroll"},
    "logs.empty": {"zh-CN": "还没有日志行。跑一次采集，或切到别的页面再回来，"
                            "这里会出现引擎与界面自己的日志。",
                   "ja-JP": "ログはまだありません。収集を一度走らせるか、別のページに"
                            "切り替えて戻ると、エンジンと画面自身のログがここに流れます。",
                   "en-US": "No log lines yet. Run a collection, or switch to another page and "
                            "come back — engine and UI logs will stream in here."},
    "logs.ui_prefix": {"zh-CN": "[界面] ", "ja-JP": "[UI] ", "en-US": "[ui] "},
    "logs.ui_trimmed": {"zh-CN": "（界面提示超过上限，较早的已丢弃）",
                        "ja-JP": "（UI 通知が上限を超えたため、古いものは破棄されました）",
                        "en-US": "(UI notes exceeded their cap — older entries were dropped)"},
    "logs.scrub_failed": {"zh-CN": "[脱敏失败，这一行已隐藏]", "ja-JP": "[マスクに失敗、この行は非表示]",
                          "en-US": "[masking failed — this line is hidden]"},
    # 设置
    "settings.intro": {"zh-CN": "外观设置即改即存（没有「应用」按钮）；所有卡片由同一个生成器出样式，保证统一感",
                       "ja-JP": "外観は即時保存（適用ボタンなし）。全カードは同一ジェネレータで統一感を保つ",
                       "en-US": "Appearance saves as you change it (no Apply button); every card is styled by one generator"},
    "settings.group.theme": {"zh-CN": "主题", "ja-JP": "テーマ", "en-US": "Theme"},
    "settings.group.accent": {"zh-CN": "强调色", "ja-JP": "アクセント色", "en-US": "Accent"},
    "settings.group.glass": {"zh-CN": "玻璃", "ja-JP": "ガラス", "en-US": "Glass"},
    "settings.group.wallpaper": {"zh-CN": "底图", "ja-JP": "背景画像", "en-US": "Wallpaper"},
    "settings.group.type": {"zh-CN": "排版与密度", "ja-JP": "文字と密度", "en-US": "Type & density"},
    "settings.group.motion": {"zh-CN": "动效", "ja-JP": "モーション", "en-US": "Motion"},
    "settings.group.logs": {"zh-CN": "日志", "ja-JP": "ログ", "en-US": "Logs"},
    "settings.group.other": {"zh-CN": "其它", "ja-JP": "その他", "en-US": "Other"},
    "settings.light": {"zh-CN": "浅色主题", "ja-JP": "ライトテーマ", "en-US": "Light theme"},
    "settings.accent": {"zh-CN": "强调色", "ja-JP": "アクセント色", "en-US": "Accent color"},
    "settings.accent_locked": {"zh-CN": "锁定（关掉自动取色）", "ja-JP": "固定（自動抽出を停止）",
                              "en-US": "Lock (stop auto-extract)"},
    "settings.alpha": {"zh-CN": "透明度（40–95）", "ja-JP": "不透明度（40–95）",
                       "en-US": "Opacity (40–95)"},
    "settings.wallpaper": {"zh-CN": "底图路径", "ja-JP": "背景画像のパス", "en-US": "Wallpaper path"},
    "settings.blur": {"zh-CN": "模糊（0–30）", "ja-JP": "ぼかし（0–30）", "en-US": "Blur (0–30)"},
    "settings.dim": {"zh-CN": "蒙层（0–60，只能加暗）", "ja-JP": "暗幕（0–60・暗くするのみ）",
                     "en-US": "Dim (0–60, darken only)"},
    "settings.focus": {"zh-CN": "九宫格焦点", "ja-JP": "焦点（9分割）", "en-US": "Focus (3×3)"},
    "settings.downsample": {"zh-CN": "下采样长边上限", "ja-JP": "縮小の長辺上限",
                            "en-US": "Downsample long edge"},
    "settings.font": {"zh-CN": "界面字号（≥10pt）", "ja-JP": "UI 文字サイズ（≥10pt）",
                      "en-US": "UI font size (≥10pt)"},
    "settings.density": {"zh-CN": "密度（紧凑/标准/宽松）", "ja-JP": "密度（密/標準/広）",
                         "en-US": "Density (compact/standard/relaxed)"},
    "settings.animations": {"zh-CN": "动效总开关", "ja-JP": "モーション全体", "en-US": "Animations"},
    "settings.fade": {"zh-CN": "交叉溶解（ms）", "ja-JP": "クロスフェード（ms）",
                      "en-US": "Crossfade (ms)"},
    "settings.debounce": {"zh-CN": "渲染防抖（ms）", "ja-JP": "描画デバウンス（ms）",
                          "en-US": "Render debounce (ms)"},
    "settings.fps": {"zh-CN": "限帧（fps）", "ja-JP": "フレーム上限（fps）", "en-US": "FPS cap"},
    "settings.log_autoscroll": {"zh-CN": "新日志自动滚到底", "ja-JP": "新しいログで自動的に最下部へ",
                                "en-US": "Auto-scroll to the newest line"},
    "settings.signature": {"zh-CN": "显示右下角签名", "ja-JP": "右下の署名を表示",
                           "en-US": "Show signature"},
    "settings.expert": {"zh-CN": "专家模式（允许单卡覆写玻璃参数）",
                        "ja-JP": "エキスパート（カード単位で上書き）",
                        "en-US": "Expert mode (per-card glass override)"},
    "settings.wallpaper_placeholder": {"zh-CN": "留空 = 关闭底图（回退主题纯色）",
                                       "ja-JP": "空欄 = 背景なし（テーマ単色）",
                                       "en-US": "Empty = no wallpaper (solid theme color)"},
    "settings.pick": {"zh-CN": "选择…", "ja-JP": "選択…", "en-US": "Browse…"},
    "settings.presets": {"zh-CN": "预设", "ja-JP": "プリセット", "en-US": "Presets"},
    "settings.load_preset": {"zh-CN": "加载「{name}」", "ja-JP": "「{name}」を読み込む",
                             "en-US": "Load “{name}”"},
    "settings.save_preset": {"zh-CN": "保存当前为预设…", "ja-JP": "現在の設定を保存…",
                             "en-US": "Save current as preset…"},
    "settings.save_preset_title": {"zh-CN": "保存预设", "ja-JP": "プリセット保存",
                                   "en-US": "Save preset"},
    "settings.preset_name": {"zh-CN": "预设名：", "ja-JP": "プリセット名：", "en-US": "Preset name:"},
    "settings.export": {"zh-CN": "导出预设 JSON…", "ja-JP": "プリセットを書き出す…",
                        "en-US": "Export presets…"},
    "settings.import": {"zh-CN": "导入预设 JSON…", "ja-JP": "プリセットを読み込む…",
                        "en-US": "Import presets…"},
    "settings.language": {"zh-CN": "语言（界面）", "ja-JP": "言語（UI）", "en-US": "Language (UI)"},
    "settings.language_auto": {"zh-CN": "跟随系统", "ja-JP": "システムに従う", "en-US": "Follow system"},
    # 下拉项（复合键：`density.<值>` / `focus.<值>`，由页面按值取）
    "density.compact": {"zh-CN": "紧凑", "ja-JP": "密", "en-US": "Compact"},
    "density.standard": {"zh-CN": "标准", "ja-JP": "標準", "en-US": "Standard"},
    "density.relaxed": {"zh-CN": "宽松", "ja-JP": "広め", "en-US": "Relaxed"},
    "focus.center": {"zh-CN": "居中", "ja-JP": "中央", "en-US": "Center"},
    "focus.top": {"zh-CN": "上", "ja-JP": "上", "en-US": "Top"},
    "focus.bottom": {"zh-CN": "下", "ja-JP": "下", "en-US": "Bottom"},
    "focus.left": {"zh-CN": "左", "ja-JP": "左", "en-US": "Left"},
    "focus.right": {"zh-CN": "右", "ja-JP": "右", "en-US": "Right"},
    "dialog.export_presets": {"zh-CN": "导出预设", "ja-JP": "プリセットの書き出し",
                              "en-US": "Export presets"},
    "dialog.import_presets": {"zh-CN": "导入预设", "ja-JP": "プリセットの読み込み",
                              "en-US": "Import presets"},
    "dialog.pick_wallpaper": {"zh-CN": "选择底图", "ja-JP": "背景画像を選ぶ",
                              "en-US": "Choose wallpaper"},
    "dialog.images": {"zh-CN": "图片", "ja-JP": "画像", "en-US": "Images"},
    "dialog.json": {"zh-CN": "JSON", "ja-JP": "JSON", "en-US": "JSON"},
    # 提示
    "toast.preset_saved": {"zh-CN": "预设「{name}」已保存", "ja-JP": "プリセット「{name}」を保存",
                           "en-US": "Preset “{name}” saved"},
    "toast.preset_loaded": {"zh-CN": "已加载预设「{name}」", "ja-JP": "「{name}」を読み込みました",
                            "en-US": "Loaded preset “{name}”"},
    "toast.setting_rejected": {"zh-CN": "设置未生效：{why}", "ja-JP": "設定が反映されません：{why}",
                               "en-US": "Setting not applied: {why}"},
    "toast.exported": {"zh-CN": "已导出 {n} 个预设", "ja-JP": "{n} 件を書き出しました",
                       "en-US": "Exported {n} presets"},
    "toast.imported": {"zh-CN": "导入 {n} 个（跳过 {skip}）", "ja-JP": "{n} 件読み込み（{skip} 件スキップ）",
                       "en-US": "Imported {n} (skipped {skip})"},
    "toast.import_failed": {"zh-CN": "导入失败：{why}", "ja-JP": "読み込み失敗：{why}",
                            "en-US": "Import failed: {why}"},
    "toast.metrics_failed": {"zh-CN": "指标读取失败：{why}", "ja-JP": "指標の取得に失敗：{why}",
                             "en-US": "Failed to read metrics: {why}"},
    "common.error_prefix": {"zh-CN": "[错误] ", "ja-JP": "[エラー] ", "en-US": "[error] "},
    # 关于
    "about.body1": {"zh-CN": "不是爬虫：三种采集环境（直连网络 / 浏览器运行时 / 制品与媒体）由证据驱动选择；先捕获后理解；事实永不丢失。",
                    "ja-JP": "クローラーではない：3 つの取得環境（直接ネット／ブラウザ／成果物・メディア）を証拠で選び、まず捕獲し後で解釈する。事実は失われない。",
                    "en-US": "Not a crawler: three acquisition environments (direct network / browser runtime / artifact & media) chosen by evidence; capture first, understand later; facts are never lost."},
    "about.body2": {"zh-CN": "边界：只在有权访问且能合法观察的范围内采集；不绕过登录/验证码/风控/签名；被拦截时降速 → 停止 → 报告。",
                    "ja-JP": "境界：正当にアクセスできる範囲のみ。ログイン・CAPTCHA・防御・署名は回避しない。遮断されたら減速→停止→報告。",
                    "en-US": "Boundary: only within what you may lawfully access; no bypassing logins, CAPTCHAs, protections or signatures; on being blocked: slow down → stop → report."},
}


class Translator:
    """一个语言的取词器。`t("nav.tasks")`；缺键 → 记进 `missing` 并回落 en-US。"""

    def __init__(self, locale: str = "en-US"):
        self.locale = locale if locale in LOCALES else "en-US"
        self.missing: set[str] = set()

    def __call__(self, key: str, **kw) -> str:
        row = _TABLE.get(key)
        if row is None:
            self.missing.add(key)
            logger.warning("缺少界面文案键：%s", key)
            return key
        text = row.get(self.locale) or row.get("en-US") or key
        if not row.get(self.locale):
            self.missing.add(key)
        try:
            return text.format(**kw) if kw else text
        except Exception:
            return text

    def has(self, key: str) -> bool:
        row = _TABLE.get(key)
        return bool(row and row.get(self.locale))

    def stats(self) -> dict:
        return {"locale": self.locale, "keys": len(_TABLE), "missing": sorted(self.missing),
                "coverage": {loc: sum(1 for r in _TABLE.values() if r.get(loc))
                             for loc in LOCALES}}


def translator(locale: str = "en-US") -> Translator:
    return Translator(locale)


def coverage() -> dict:
    """三语覆盖率（门禁用：**每种语言都必须覆盖全部键**，不许有半截翻译）。"""
    return {loc: sum(1 for r in _TABLE.values() if r.get(loc)) for loc in LOCALES}, len(_TABLE)

# -*- coding: utf-8 -*-
"""界面多语言：**语言解析顺序 + 三语文案表**

机主的硬要求（原话）：**"确保你正常安装之后它会给你本机系统上的语言"** ——
安装时选了简体中文，装完就绝不能蹦出日文。所以语言不能写死在代码里，必须按固定顺序解析：

    ① 用户在设置里显式选过 → 用它（`zh-CN` / `ja-JP` / `en-US` / 空=跟随系统）
    ② 安装器当时选的（安装目录 `install.marker` 里的 `lang=<LCID>`）→ 用它
    ③ 本机系统 UI 语言（Windows `GetUserDefaultUILanguage`）→ 用它
    ④ 兜底 `en-US`（**不是** zh-CN：对非中文用户，英文比中文更可能被看懂）

三条纪律：
  * **缺翻译要说出来**：`Translator.missing` 记录没命中的键，界面自检能读到——
    不允许"某句话悄悄变成另一种语言"（那正是机主说的"愚蠢的问题"）；
  * **不猜系统语言**：`GetUserDefaultUILanguage` 拿不到就往下走，不靠 `LANG` 之类的偶然值拍板；
  * 语言**即改即生效**（和外观设置一样，没有"应用"按钮）。
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
    # 概览
    "stat.pages_per_sec": {"zh-CN": "页/秒", "ja-JP": "ページ/秒", "en-US": "pages/s"},
    "stat.mb_per_sec": {"zh-CN": "MB/s", "ja-JP": "MB/s", "en-US": "MB/s"},
    "stat.tasks_ok_fail": {"zh-CN": "任务 成功/失败", "ja-JP": "タスク 成功/失敗",
                           "en-US": "tasks ok/fail"},
    "stat.request_p95": {"zh-CN": "请求 p95", "ja-JP": "リクエスト p95", "en-US": "request p95"},
    "overview.plan": {"zh-CN": "资源计划：{text}", "ja-JP": "リソース計画：{text}",
                      "en-US": "Resource plan: {text}"},
    "overview.plan_note": {"zh-CN": "队列全有界｜写盘单线程 + 批提交｜缺省即拒绝",
                           "ja-JP": "全キューに上限｜書き込みは単一スレッド＋バッチ｜既定は拒否",
                           "en-US": "All queues bounded | single writer + batch commit | deny by default"},
    # 任务
    "tasks.hint": {"zh-CN": "任务下钻：每一行都能说清「看到了什么 → 决定了什么 → 为什么」",
                   "ja-JP": "タスク詳細：各行が「何を見て → 何を決め → なぜか」を説明できる",
                   "en-US": "Drill-down: every row explains what was seen, decided, and why"},
    "tasks.col.state": {"zh-CN": "状态", "ja-JP": "状態", "en-US": "state"},
    "tasks.col.target": {"zh-CN": "目标", "ja-JP": "対象", "en-US": "target"},
    "tasks.col.counters": {"zh-CN": "尝试/限流/转移", "ja-JP": "試行/制限/遷移",
                           "en-US": "try/throttle/move"},
    "tasks.col.evidence": {"zh-CN": "证据", "ja-JP": "証拠", "en-US": "evidence"},
    "tasks.col.fingerprint": {"zh-CN": "指纹", "ja-JP": "指紋", "en-US": "fingerprint"},
    "tasks.col.bytes": {"zh-CN": "字节", "ja-JP": "バイト", "en-US": "bytes"},
    # 日志
    "logs.hint": {"zh-CN": "结构化日志（JSON 行）：字段稳定，可直接喂 jq；脱敏在落盘之前完成",
                  "ja-JP": "構造化ログ（JSON 行）：フィールド安定・jq 可・保存前にマスク",
                  "en-US": "Structured logs (JSON lines): stable fields, jq-friendly, masked before write"},
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

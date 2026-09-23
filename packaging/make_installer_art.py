# -*- coding: utf-8 -*-
"""生成安装器美术（与 UI 同一套设计语言：深空渐变 + 双光晕 + 点阵 + Logo 芯片 + 版本徽章）
以及**三语言许可页文本**。

    python packaging/make_installer_art.py            # 生成到 packaging/art/
    python packaging/make_installer_art.py --out DIR

产出（NSIS 要的格式是 **BMP**，尺寸也是 NSIS 规定的）：
    welcome.bmp   164×314  欢迎/完成页左侧横幅
    header.bmp    150×57   内页顶部小图
    license_zh.txt / license_ja.txt / license_en.txt   许可页文本（**UTF-8 with BOM**）
另出 PNG 预览（给人在文件管理器里看一眼，不参与打包）。

为什么许可页要三份、而且**必须带 BOM**：NSIS 的 `LicenseData` 对**无 BOM** 的文件按
**本机 ANSI 代码页**（简体中文机器上是 936/GBK）解码 —— 一份 UTF-8 无 BOM 的中文许可，
到了安装向导里就是满屏乱码生僻字（真实事故）。UTF-8 with BOM 是 NSIS 3 认的形式。

为什么要自己画而不是随便找图：安装器是用户看到的第一眼，它得和界面是一套语言；
而且**版本徽章必须从版本单一来源读**（写死在图里 = 又一个版本漂移点）。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

__all__ = ["make_welcome", "make_header", "license_text", "license_files",
           "write_licenses", "main"]


def _np_cv():
    import cv2
    import numpy as np
    return cv2, np


def _base_gradient(w: int, h: int, cv2, np):
    """深空渐变 `#0B0F14 → #131B26`（左上到右下），再叠两道高斯光晕（screen 混合）。"""
    top = np.array([0x14, 0x0F, 0x0B], dtype="float32")      # BGR of #0B0F14
    bottom = np.array([0x26, 0x1B, 0x13], dtype="float32")   # BGR of #131B26
    ramp = np.linspace(0.0, 1.0, h, dtype="float32")[:, None, None]
    img = top[None, None, :] * (1 - ramp) + bottom[None, None, :] * ramp
    img = np.repeat(img, w, axis=1)
    # 双重光晕：强调色的两个同心光斑 → 模糊后 screen 叠加
    glow = np.zeros((h, w), dtype="float32")
    cv2.circle(glow, (int(w * 0.25), int(h * 0.18)), max(8, int(min(w, h) * 0.28)), 1.0, -1)
    cv2.circle(glow, (int(w * 0.80), int(h * 0.86)), max(8, int(min(w, h) * 0.22)), 0.7, -1)
    glow = cv2.GaussianBlur(glow, (0, 0), sigmaX=max(3.0, min(w, h) * 0.06))
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")   # BGR of #4FA3E8
    torch = glow[:, :, None] * accent[None, None, :] * 0.55
    # screen 混合（1-(1-a)(1-b)）
    img = 255.0 - (255.0 - img) * (255.0 - torch) / 255.0
    return np.clip(img, 0, 255).astype("uint8")


def _dot_pattern(img, step: int, alpha: float, cv2, np):
    """14px 点阵（alpha ≈ 10 → 几乎看不见的一层纹理，作用是把纯渐变的"塑料感"打散）。"""
    if step <= 0:
        return img
    h, w = img.shape[:2]
    dots = np.zeros((h, w), dtype="float32")
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dots[y, x] = 1.0
    dots = cv2.GaussianBlur(dots, (0, 0), sigmaX=0.6)
    out = img.astype("float32") + dots[:, :, None] * 255.0 * alpha
    return np.clip(out, 0, 255).astype("uint8")


def _logo_chip(img, icon_path, *, x: int, y: int, size: int, cv2, np):
    """Logo 芯片：圆角方块 + 外发光（放大 6px + Blur(10) 后叠加）+ 图标等比贴入。"""
    h, w = img.shape[:2]
    if not pathlib.Path(icon_path).exists():
        return img, (x, y, size, size)
    icon = cv2.imdecode(np.fromfile(str(icon_path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if icon is None:
        return img, (x, y, size, size)
    if icon.ndim == 2:
        icon = cv2.cvtColor(icon, cv2.COLOR_GRAY2BGRA)
    if icon.shape[2] == 3:
        icon = cv2.cvtColor(icon, cv2.COLOR_BGR2BGRA)
    inner = int(size * 0.78)
    scale = inner / max(icon.shape[:2])
    icon = cv2.resize(icon, (max(1, int(icon.shape[1] * scale)),
                            max(1, int(icon.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    # 外发光
    halo = np.zeros((h, w), dtype="float32")
    cv2.rectangle(halo, (x - 3, y - 3), (x + size + 3, y + size + 3), 1.0, 6)
    halo = cv2.GaussianBlur(halo, (0, 0), sigmaX=10)
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")
    img = 255.0 - (255.0 - img.astype("float32")) * (255.0 - halo[:, :, None] * accent * 0.5) / 255.0
    img = np.clip(img, 0, 255).astype("uint8")
    # 芯片底：圆角 12
    chip = img.copy()
    cv2.rectangle(chip, (x, y), (x + size, y + size), (40, 30, 22), -1, cv2.LINE_AA)
    mask = np.zeros((h, w), dtype="uint8")
    cv2.rectangle(mask, (x, y), (x + size, y + size), 255, -1, cv2.LINE_AA)
    img = np.where(mask[:, :, None] > 0, chip, img)
    # 贴图标（含 alpha）
    oy, ox = y + (size - icon.shape[0]) // 2, x + (size - icon.shape[1]) // 2
    roi = img[oy:oy + icon.shape[0], ox:ox + icon.shape[1]].astype("float32")
    a = (icon[:, :, 3:4].astype("float32") / 255.0)
    bgr = icon[:, :, :3].astype("float32")
    img[oy:oy + icon.shape[0], ox:ox + icon.shape[1]] = (roi * (1 - a) + bgr * a).astype("uint8")
    return img, (x, y, size, size)


def _text(img, text: str, *, org, scale: float, color, thickness: int, cv2):
    """写字：优先用能显中文的字体文件，找不到就退回 Hershey（此时只写 ASCII）。"""
    fonts = [
        r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhl.ttc",
        r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\meiryo.ttc",
    ]
    for f in fonts:
        if pathlib.Path(f).exists():
            try:
                from PIL import Image, ImageDraw, ImageFont
                pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(pil)
                font = ImageFont.truetype(f, size=int(max(9, scale * 34)))
                draw.text((org[0], org[1] - int(scale * 34)), text, font=font,
                          fill=(color[2], color[1], color[0]))
                return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
            except Exception:
                break
    cv2.putText(img, text.encode("ascii", "ignore").decode() or "Daedalus", org,
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)
    return img


def make_welcome(icon_path, version: str, *, w: int = 164, h: int = 314):
    """欢迎/完成页横幅：渐变 + 点阵 + Logo 芯片 + 名称 + **版本徽章**。"""
    cv2, np = _np_cv()
    img = _base_gradient(w, h, cv2, np)
    img = _dot_pattern(img, 14, 0.04, cv2, np)
    img, (cx, cy, cs, _) = _logo_chip(img, icon_path, x=(w - 56) // 2, y=42, size=56,
                                      cv2=cv2, np=np)
    img = _text(img, "代达罗斯", org=((w - 4 * 14) // 2, cy + cs + 30), scale=0.55,
                color=(0xF0, 0xE6, 0xDA), thickness=1, cv2=cv2)
    # 版本徽章（**从版本单一来源读**，不写死在图里）
    badge = f"v{version}"
    (tw, th), _b = cv2.getTextSize(badge, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
    bx, by = (w - tw - 20) // 2, h - 26
    cv2.rectangle(img, (bx - 8, by - th - 6), (bx + tw + 8, by + 6), (46, 36, 26), -1, cv2.LINE_AA)
    img = _text(img, badge, org=(bx, by), scale=0.38, color=(0xD0, 0xB0, 0x90), thickness=1,
                cv2=cv2)
    # 底部 2px 渐隐强调线
    line = np.zeros((2, w), dtype="float32")
    for x in range(w):
        line[:, x] = (1.0 - abs(x - w / 2) / (w / 2)) ** 2
    accent = np.array([0xE8, 0xA3, 0x4F], dtype="float32")
    img[h - 4:h - 2] = np.clip(img[h - 4:h - 2].astype("float32")
                               + line[:, :, None] * accent * 0.7, 0, 255).astype("uint8")
    return img


def make_header(icon_path, version: str, *, w: int = 150, h: int = 57):
    """内页顶部小图：渐变 + 芯片 + 名称（版本放右上角小字）。"""
    cv2, np = _np_cv()
    img = _base_gradient(w, h, cv2, np)
    img, (cx, cy, cs, _) = _logo_chip(img, icon_path, x=10, y=8, size=40, cv2=cv2, np=np)
    img = _text(img, "代达罗斯", org=(cx + cs + 8, cy + cs // 2 + 6), scale=0.42,
                color=(0xF0, 0xE6, 0xDA), thickness=1, cv2=cv2)
    img = _text(img, f"v{version}", org=(w - 52, h - 8), scale=0.30,
                color=(0xB0, 0x9A, 0x80), thickness=1, cv2=cv2)
    return img


def license_text() -> str:
    """许可页文本：由 `docs/07-能力边界.md` **生成**（单一来源，不手抄两份）。"""
    src = ROOT / "docs" / "07-能力边界.md"
    if not src.exists():
        return ("Daedalus · 使用边界\n\n"
                "只在有权访问且能合法观察的范围内采集；\n"
                "不绕过登录、验证码、风控与签名；\n"
                "被拦截时降速 → 停止 → 报告，不做对抗升级。\n")
    body = src.read_text(encoding="utf-8")
    out = []
    for line in body.splitlines():
        s = line.rstrip()
        if s.startswith("#"):
            out.append(s.lstrip("# ").strip().upper() if s.startswith("## ") else
                       s.lstrip("# ").strip())
            out.append("")
        elif s.startswith(("- ", "* ", "> ")):
            out.append("  " + s[2:].strip())
        else:
            out.append(s)
    return "\n".join(out).strip() + "\n"


# ── 许可页文本：三语言 ────────────────────────────────────────────────
# 单一来源纪律（与版本号同一条规矩）：
#   **中文是权威源**，由 `license_text()` 从 `docs/07-能力边界.md` **生成**（不手抄）；
#   **日文/英文是它的忠实翻译** —— 技术事实直说、不删条目、不加戏、保留原有分节结构。
#   ⚠️ 改中文（= 改 docs/07）时**必须同步改下面两份译文**，否则三种语言的说法会分叉：
#   用户在日语/英语系统上看到的边界声明就会与文档不一致。
# 译文写成**逐行列表**（而不是一个大三引号串）的原因：有 4 行按原文必须点名
# `navigator.webdriver` 与 CAPTCHA —— 那是**不做清单本身**（不是实现），需要在
# 那一行挂 `# noqa: lint -- 理由` 才能通过 S10b 的 F3 边界扫描（该扫描跳过带豁免的行）。
LICENSE_JA = "\n".join([
    "07 · 能力の境界（やること / やらないこと）",
    "",
    "",
    "  この文書は**インストーラのライセンスページの本文**でもあります（`tools/build.py` が本ファイルから",
    "  `packaging/art/license_zh.txt` を生成。単一の出所、手写しはしません）。したがって、ここを変更すると",
    "  インストール時にユーザーが見る境界宣言が直接変わります。",
    "",
    "ひとことで",
    "",
    "",
    "**合法的に観察できる**手段だけで収集します。「何が見えるか」は最強まで突き詰め、「何を突破するか」は",
    "一切やりません。",
    "",
    "やること（コンプライアンス上の頑健性を、最強まで）",
    "",
    "",
    "  **正直な身元**：用途を明記した UA を既定で送ります（`Daedalus/<バージョン> (personal data collector; ...)`）。",
    "  ブラウザ指紋の偽装も Referer の偽造もしません。",
    "  **礼儀優先**：ドメイン単位のレート制限 + 並列上限 + 原子的に予約する礼儀間隔；`Retry-After` を優先し、",
    "  かつ**上限を付けます**（既定 300s）；ドメイン冷却は**再起動をまたいで保持**されます（再起動しても",
    "  「そのサイトは休憩中」を忘れません）。",
    "  **robots 規則**：RFC 9309 の意味論どおりに実行します（404/4xx → 存在しない扱い；5xx/到達不能 →",
    "  **完全禁止**扱い；最長一致優先、同点なら Allow の勝ち；キャッシュ ≤24h）。",
    "  **セッション再利用**：cookies（Netscape / JSON / ブラウザ書き出し）→ セッション jar、DPAPI 暗号文で",
    "  ディスクに保存。**あなた自身がアクセス権を持つ**セッションに使います。",
    "  **改造していないブラウザ**：実ブラウザで公開ページにアクセスし、**ネットワーク観察**を行います",
    "  （プロセス/コンテキスト/ページの三層スロット、サブリソース単位の判定）。用途は「中身が空の殻」の",
    "  ページに限ります —— それ以上はしません。",
    "  **遮断の識別と処置**：「どこで、なぜ遮られたか」を説明できます（ステータスコード、`Retry-After`、",
    "  遮られた URL と理由はすべて事実層に残ります）。そして**段階的バックオフ → 減速 → 停止 → 報告 →",
    "  そのサイトを理由付きで放棄**します。",
    "  **許可された対象は上限まで**：**自分のサイト、提携先の許可範囲、公式 API** に対しては、並列数と強度を",
    "  リソース計画が許す上限まで上げられます。",
    "",
    "やらないこと（相手の防御を突破すること；本プロジェクトはアーキテクチャ上、場所を用意していません）",
    "",
    "",
    "  ステルス注入 / 指紋偽装 / `navigator.webdriver` の書き換え；",  # noqa: lint -- 不做清单原文点名该属性
    "  CAPTCHA の自動認識・代行サービスとの接続；",  # noqa: lint -- 不做清单原文点名验证码
    "  リクエスト署名の偽造（各サイトのリスク管理署名アルゴリズム）；",
    "  出口 IP のローテーション、プロキシプール、アカウントプール；",
    "  挙動の擬人化（人間のリズム/マウス軌跡を模してリスク管理を回避すること）；",
    "  ログイン、ペイウォール、アクセス制御、レート制限を回避する技術的手段。",
    "",
    "**なぜやらないか**（三つの理由、どれか一つだけでも成立します）：",
    "",
    "1. **本プロジェクト自身が書いたレッドラインを越えるから**：三つの設計文書、《02》の C4 条、そして",
    "   オーナーが明確に確認した境界が、いずれも「相手の防御を突破しない」を交渉不可の項目としています。",
    "   アーキテクチャ上、最上位には三つの専門家（直結ネットワーク/ブラウザランタイム/成果物とメディア）",
    "   しか置かず、「対抗」の位置はありません。",
    "2. **この種の能力は有効性を自己証明できないから**：Kiana の失敗記録自身が「定量的証拠なし、",
    "   オフラインでは検証不能」と述べています。実装すれば有効だと主張することになり、しかも検収可能な",
    "   判定基準は出せません —— それは「未検証」を「実装済み」と包装することです。",
    "3. **コストと便益が非対称だから**：一つのサイトのリスク管理を回避するために、対象が変われば失効する",
    "   実装一式を保守し続ける必要があり、本プロジェクトの正当な用途（自社/許諾済み/公開データ）には",
    "   まったく不要です。",
    "",
    "遮られたらどうするか（これが本プロジェクトの正解）",
    "",
    "",
    "| 現象 | 処置 | 何が見えるか |",
    "|---|---|---|",
    "| 429 / 503 / `Retry-After` | **再試行しない、IP も替えない**：独立カウントで冷却 + バックオフ、"
    "連続 N 回でデッドレターへ | `net.throttled`、`throttles` カウント、台帳の項目、アラート |",
    "| robots が不許可 | エンキュー前に拒否（`policy_denied`）、出網ゼロ | タスク状態 `policy_denied` + 読める理由 |",
    "| SSRF ゲートの遮断（私網/予約アドレス） | 再試行不可、即停止 | 証拠内の `policy_denied` + 具体的なアドレス |",
    "| 中身が常に空殻/検証ページ | `browser` 候補として記録、または品質で拒否。"
    "**対抗手段へは昇格しない** | 品質スコアと理由、ブラウザ候補の証拠 |",
    "| サイト全体から拒否された | そのサイトを停止し、理由を書き、次の対象へ | 台帳 `policy_denied`、監査用に書き出し可能 |",
    "",
    "環境変数とスイッチ（「変通」の合法的な形）",
    "",
    "",
    "  **プロキシは環境変数からのみ読みます**（`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`）："
    "「適当なプロキシを入れる」",
    "  インターフェースは提供せず、ローテーションもしません。",
    "  **形状を変えず、差し替え可能に**：**合法な**新しい収集手段（公式 API、許諾データソース、自前プロキシ、",
    "  外部ツール）はどれもアダプタ/実行リソースとして差し込めます —— 出所を一つ足す = ファイルを一つ足す。",
    "  コアは対抗手段のために形を変えません。",
    "  `respect_robots` は切れます（設定項目は存在します）が、**切る前に一度よく考えてください**：",
    "  それはあなた自身のコンプライアンス上のレッドラインで、エンジンは代わりに判断しません。",
    "  CLI に対応スイッチが無いのは、これを気軽にしたくないからです。",
    "",
    "ユーザー自身が責任を負う部分（曖昧にせず明記）",
    "",
    "",
    "  収集対象はあなたが**アクセス権を持つ**ものでなければなりません。本プロジェクトは代理で権限判断をせず、",
    "  権限を迂回する能力も提供しません。",
    "  収集したデータの扱いはあなたの責任です（本機のマスキングスイッチは**表示と書き出し**にのみ影響します。",
    "  **生の層は決してマスキングしません** —— マスキングすると再生できなくなり、事実を壊すことと同じです）。",
    "  アンインストーラは**あなたのデータを削除しません**。削除は別途チェックする明示的な操作です。",
]) + "\n"

LICENSE_EN = "\n".join([
    "07 · Capability Boundaries (what we do / what we do not do)",
    "",
    "",
    "  This document doubles as **the license text in the installer** (generated from this file by",
    "  `tools/build.py` into `packaging/art/license_zh.txt` — one single source, never hand-copied).",
    "  So editing it here directly changes the boundary statement a user sees while installing.",
    "",
    "In one sentence",
    "",
    "",
    "Collect only by **lawful, observable** means: make \"what I can see\" as strong as it can go,",
    "and never do \"what I must break through\".",
    "",
    "Do (compliance robustness, pushed as far as it goes)",
    "",
    "",
    "  **Honest identity**: a UA that states its purpose is sent by default",
    "  (`Daedalus/<version> (personal data collector; ...)`); no browser-fingerprint spoofing, no",
    "  forged Referer.",
    "  **Politeness first**: per-domain rate limiting + a concurrency cap + an atomically reserved",
    "  politeness interval; `Retry-After` wins and is **capped** (300s by default); domain cooldowns",
    "  **survive restarts** (a restart does not make us forget that a site is resting).",
    "  **robots rules**: enforced with RFC 9309 semantics (404/4xx → treated as absent;",
    "  5xx/unreachable → treated as **fully disallowed**; longest match wins, Allow wins ties;",
    "  cache ≤24h).",
    "  **Session reuse**: cookies (Netscape / JSON / browser export) → a session jar, kept on disk as",
    "  DPAPI ciphertext, for sessions **you are entitled to access**.",
    "  **Unmodified browser**: a real browser visits public pages and does **network observation**",
    "  (three layers of slots — process / context / page — with per-subresource decisions), used for",
    "  pages whose content is an empty shell — and for nothing beyond that.",
    "  **Block detection and handling**: it can state **where** we were blocked and **why** (status",
    "  code, `Retry-After`, the blocked URL and the reason all land in the fact layer), then",
    "  **graded backoff → slow down → stop → report → abandon that site with a recorded reason**.",
    "  **Authorized targets get full strength**: for **your own sites, a partner's authorized scope,",
    "  official APIs**, concurrency and intensity can go up to whatever the resource plan allows.",
    "",
    "Do not (defeating the other side's protections; the architecture leaves no room for it)",
    "",
    "",
    "  injection to hide automation / fingerprint spoofing / rewriting `navigator.webdriver`;",  # noqa: lint -- 不做清单原文点名该属性
    "  solving verification challenges or integrating with solving services;",  # noqa: lint -- 不做清单原文点名验证码
    "  forging request signatures (the risk-control signature algorithms of various sites);",
    "  egress IP rotation, proxy pools, account pools;",
    "  imitating human behaviour (mimicking human pacing / mouse trajectories to evade risk control);",
    "  any technical means of bypassing logins, paywalls, access control or rate limits.",
    "",
    "**Why we do not do these** (three reasons, any one of which stands on its own):",
    "",
    "1. **It crosses a red line this project wrote itself**: three design documents, clause C4 of《02》,",
    "   and a boundary the owner explicitly confirmed all list \"do not defeat the other side's",
    "   protections\" as non-negotiable. Architecturally there are exactly three experts at the top",
    "   level (direct network / browser runtime / artifacts and media) and no place at all for",
    "   \"adversarial\" behaviour.",
    "2. **Such capabilities cannot prove themselves effective**: Kiana's own failure log says \"no",
    "   quantitative evidence, cannot be tested offline\". Shipping them would mean claiming they",
    "   work, while the project can produce no acceptance criterion — that is dressing \"unverified\"",
    "   up as \"implemented\".",
    "3. **Cost and benefit are asymmetric**: to slip past one site's risk control you must maintain a",
    "   whole implementation that goes stale as the target changes, and this project's legitimate",
    "   uses (owned / authorized / public data) do not need it at all.",
    "",
    "What to do when blocked (this is the real answer in this project)",
    "",
    "",
    "| Symptom | Handling | What you can see |",
    "|---|---|---|",
    "| 429 / 503 / `Retry-After` | **No retry, no IP change**: an independent counter plus cooldown"
    " and backoff; after N consecutive hits the item goes to the dead-letter queue | `net.throttled`,"
    " the `throttles` counter, a ledger entry, an alert |",
    "| robots disallows | rejected before enqueueing (`policy_denied`), zero egress |"
    " task state `policy_denied` + a readable reason |",
    "| SSRF gate blocks (private/reserved address) | not retryable, stop immediately |"
    " `policy_denied` in the evidence + the exact address |",
    "| Content is always an empty shell / verification page | record a `browser` candidate or reject"
    " on quality, **never escalate to adversarial means** | quality score and reason,"
    " browser-candidate evidence |",
    "| A site rejects us entirely | stop for that site, write down the reason, move to the next target"
    " | ledger `policy_denied`, exportable for audit |",
    "",
    "Environment variables and switches (the lawful form of working around things)",
    "",
    "",
    "  **Proxies are read from environment variables only** (`HTTP_PROXY` / `HTTPS_PROXY` /",
    "  `ALL_PROXY`): there is no interface for \"just type in some proxy\", and no rotation either.",
    "  **Pluggable rather than reshaped**: any **lawful** new acquisition method (official API,",
    "  licensed data source, self-hosted proxy, external tool) can be plugged in as an adapter or an",
    "  execution resource — adding a source = adding a file; the core is never reshaped for",
    "  adversarial means.",
    "  `respect_robots` can be turned off (the config key exists), but **think it through before",
    "  turning it off**: that is your own compliance red line and the engine will not judge it for",
    "  you. There is no CLI switch for it — precisely so that this does not become convenient.",
    "",
    "What you are responsible for (stated plainly, no hedging)",
    "",
    "",
    "  The targets you collect must be ones **you are entitled to access**; this project does not",
    "  judge authorization for you, and provides no means of bypassing it.",
    "  The data you collect is yours to handle (the on-machine redaction switch only affects",
    "  **display and export**; **the raw layer is never redacted** — redact it and it can no longer",
    "  be replayed, which is the same as destroying the facts).",
    "  The uninstaller **does not delete your data**; deleting it is a separate, explicitly ticked",
    "  action.",
]) + "\n"

LICENSE_NAMES = ("license_zh.txt", "license_ja.txt", "license_en.txt")
# 旧版只有一个 `license.txt`（**无 BOM** → 许可页乱码），已废弃；生成时顺手删掉，
# 免得留下一个「看起来在用、其实没人读」的孤儿文件（installer.nsi 不再引用它）。
LEGACY_LICENSE = "license.txt"


def license_files() -> list[tuple[str, str]]:
    """三份许可文本：`[(文件名, 文本)]`。中文从 docs/07 生成，日/英取上面的译文常量。"""
    return [("license_zh.txt", license_text()),
            ("license_ja.txt", LICENSE_JA),
            ("license_en.txt", LICENSE_EN)]


def write_licenses(out: pathlib.Path) -> list[str]:
    """把三份许可写进 `out`，**每份都带 UTF-8 BOM**（不满足就当场拒绝，不留给安装器去乱码）。"""
    made: list[str] = []
    for name, text in license_files():
        p = out / name
        p.write_text(text, encoding="utf-8-sig")          # utf-8-sig = 写入时带 BOM
        head = p.read_bytes()[:3]
        if head != b"\xef\xbb\xbf":
            raise SystemExit(f"{name} 写入后前 3 字节是 {head.hex(' ')}，不是 UTF-8 BOM")
        made.append(f"{name}（{p.stat().st_size} 字节，UTF-8 BOM，{len(text)} 字符）")
    old = out / LEGACY_LICENSE
    if old.exists():
        old.unlink()
        made.append(f"{LEGACY_LICENSE}（旧的无 BOM 单一许可文件，已删除）")
    return made


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成安装器美术（BMP + 预览 PNG + 三语言许可文本）")
    ap.add_argument("--out", default=str(ROOT / "packaging" / "art"))
    args = ap.parse_args(argv)
    from daedalus import VERSION
    cv2, np = _np_cv()
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    icon = ROOT / "assets" / "icon_256.png"
    welcome = make_welcome(str(icon), VERSION)
    header = make_header(str(icon), VERSION)
    made = []
    for name, img in (("welcome", welcome), ("header", header)):
        bmp = out / f"{name}.bmp"
        okk, buf = cv2.imencode(".bmp", img)
        if not okk:
            raise SystemExit(f"编码 {name}.bmp 失败")
        bmp.write_bytes(buf.tobytes())
        png = out / f"{name}.png"                     # 预览用（不参与打包）
        (png).write_bytes(cv2.imencode(".png", img)[1].tobytes())
        made.append(f"{bmp.name} {img.shape[1]}×{img.shape[0]}")
    made.extend(write_licenses(out))          # 三语言许可（中文由 docs/07-能力边界.md 生成）
    print("安装器美术已生成：")
    for m in made:
        print(f"  {m}")
    print(f"输出目录：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

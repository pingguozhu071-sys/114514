# -*- coding: utf-8 -*-
"""环境②浏览器运行时：**进程 / 上下文 / 页 三层槽位 + 观察 + 自己的闸**

它解决什么问题（为什么"直连"不够）：
    有的页面在直连视角下就是"内容空壳"——正文由脚本渲染、数据藏在后续 XHR 里。
    要看见这些，必须真的执行页面（浏览器运行时）。

────────────────────────────────────────────────────────────────
四条不变量（每条都对应一个真实事故类型）

  1) **浏览器有自己的闸**（S1 的纪律："每环境各自建闸"）。
     浏览器发起的是**它自己的网络请求**，根本不经过 `net/fetch.py` 那个唯一咽喉——
     所以子资源（CDN/接口/XHR）必须由**本环境的独立闸**逐个判定：协议、私网地址、
     DNS 解析（含 rebinding 的 TTL 缓存复用）、以及站点的 robots。
     被拦的请求走 `route.abort()`，且**记下原因**（不是静默丢弃）。
  2) **观察即捕获**（Capture First）。观察到的每个请求/响应都留下事实：
     URL、方法、状态、类型、大小、耗时、是否被拦。**文档正文**（以及 XHR/fetch 的载荷）
     按预算写入**原始层**（内容寻址，与直连捕获同一套存储）。

     ⚠️ **一条实测出来的坑（必须记住）**：在 route 拦截生效时，Playwright 一旦读了某个
     响应的 `body()`，Chromium 会**自己再取一遍**那份资源——而且这一遍**不带我们设的
     context 头**（UA 会退回默认的 HeadlessChrome）。实测：读所有响应体 → 每个图片
     被取两次、第二轮必然带错 UA（S8 门禁 C1 抓到）。所以这里的策略是**按类型分流**：
       * `document` / `xhr` / `fetch`：读 body 并落原始层（这是真正有价值的数据，且通常很小）；
       * 其余（image/script/css/font/media…）：**只记元数据**（大小取 `content-length`，
         没给就记 0）——不为了一串数字把页面所有资源都下载两遍。
     另外启动时也会把 UA 作为**启动参数**再传一遍，让浏览器进程自己发起的请求也带上诚实 UA。
  3) **槽位显式、缺省即拒绝**。进程 1 个起始、上下文与页各自有上限（来自资源计划，
     默认 **0 = 不启用**）；拿不到槽位就**明确失败**，不排队等、不偷偷多开。
  4) **不做对抗**（本工程边界）：不注入隐身脚本、不伪装指纹、不绕过验证码识别、
     不改 `navigator.webdriver`、不轮换出口。这里是"未改装的浏览器 + 诚实 UA"。
     被拦就如实报告（"被拦在哪、为什么"是可查询事实），不升级为规避手段。

就绪探测（P24）：`probe_browser()` 只查包与二进制路径，**不启动浏览器**；
启动失败时 `capability()` 会说清"缺什么、哪些能力因此不可用"。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import threading
import time
from dataclasses import dataclass, field

from daedalus.obs.metrics import METRICS

logger = logging.getLogger(__name__)

__all__ = ["BrowserEnvironment", "BrowserVerdict", "ObservedRequest", "probe_browser",
           "BrowserCapability", "DEFAULT_MAX_CAPTURE", "shutdown_playwright",
           "playwright_state"]

DEFAULT_MAX_CAPTURE = 8 << 20        # 单页最多往原始层写 8MB（正文 + 高价值载荷）
DEFAULT_MAX_OBSERVED = 500           # 单页最多记多少条观察（有界，防页面刷请求把内存刷爆）
DEFAULT_TIMEOUT = 30.0
# 读 body 的**高价值**类型（见文件头那条坑：读 body 会让 Chromium 再取一遍）
BODY_TYPES = frozenset({"document", "xhr", "fetch"})

# ── 进程级 playwright 单例 ───────────────────────────────────────
# 为什么必须是单例：playwright 的同步 API 在同一个进程里**反复 start/stop 会撞上**
#       Error: It looks like you are using Playwright Sync API inside the asyncio loop.
# 第一次 stop 之后残留的循环会让第二次 start 直接失败（S8 门禁 C2–C4 全红就是这个）。
# 正确形状：驱动进程**一个进程一个**，浏览器（进程）按需要开关，退出时统一收尾。
_PW_LOCK = threading.RLock()
_PW: dict = {"mgr": None, "pw": None, "factory": None, "starts": 0, "stops": 0, "why": ""}


def playwright_state() -> dict:
    """当前 playwright 单例状态（观测/自检用）。"""
    with _PW_LOCK:
        return {"started": _PW["pw"] is not None, "starts": _PW["starts"],
                "stops": _PW["stops"], "why": _PW["why"]}


def _acquire_playwright(factory=None):
    """取（必要时启动）进程级 playwright 实例。线程安全；重复调用复用同一个。"""
    with _PW_LOCK:
        if _PW["pw"] is not None:
            return _PW["pw"]
        if factory is not None:
            _PW["factory"] = factory
            _PW["pw"] = factory()
            _PW["mgr"] = None
        else:
            from playwright.sync_api import sync_playwright
            _PW["mgr"] = sync_playwright()
            _PW["pw"] = _PW["mgr"].start()
        _PW["starts"] += 1
        _PW["why"] = ""
        return _PW["pw"]


def shutdown_playwright() -> dict:
    """收尾进程级 playwright（关闭链调用；幂等）。**不抛异常**。"""
    with _PW_LOCK:
        out = {"stopped": False, "why": ""}
        mgr = _PW["mgr"]
        try:
            if mgr is not None:
                mgr.stop()
                out["stopped"] = True
        except Exception as e:
            out["why"] = f"{type(e).__name__}: {e}"
            _PW["why"] = out["why"]
        finally:
            _PW["mgr"] = None
            _PW["pw"] = None
            if out["stopped"]:
                _PW["stops"] += 1
        return out


@dataclass(frozen=True)
class BrowserCapability:
    """浏览器能力的**如实报告**（不启动浏览器，只查包与二进制）。"""

    playwright: bool = False
    engine: str = "chromium"
    executable: str = ""
    executable_exists: bool = False
    version: str = ""
    reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.playwright and self.executable_exists)

    def to_dict(self) -> dict:
        return {"available": self.available, "playwright": self.playwright,
                "engine": self.engine, "executable": self.executable,
                "executable_exists": self.executable_exists, "version": self.version,
                "reason": self.reason,
                "note": "" if self.available else "浏览器环境不可用：相关能力被禁用（不会静默降级）"}


def _browsers_root(client=None) -> pathlib.Path:
    """浏览器二进制根目录：环境变量优先，否则用户级 `ms-playwright`。

    `client` 可注入（测试用）；不给就按 playwright 的约定目录找。
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        return pathlib.Path(env)
    if client:
        try:
            return pathlib.Path(client)
        except Exception:
            pass
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or str(pathlib.Path.home() / ".cache")
    return pathlib.Path(base) / "ms-playwright"


def _chromium_exe(root: pathlib.Path, revision: str) -> pathlib.Path:
    """按平台拼出 chromium 可执行文件路径（win/linux/mac 三种约定）。"""
    d = root / f"chromium-{revision}"
    if sys.platform.startswith("win"):
        return d / "chrome-win64" / "chrome.exe"
    if sys.platform == "darwin":
        return d / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
    return d / "chrome-linux" / "chrome"


def _driver_version(pkg_dir: pathlib.Path) -> str:
    """读 driver 的 `package.json` 版本（读不到返回空串，**不猜**）。"""
    try:
        import json
        meta = pkg_dir / "driver" / "package" / "package.json"
        if meta.exists():
            return str(json.loads(meta.read_text(encoding="utf-8")).get("version") or "")
    except Exception:
        pass
    return ""


def _playwright_is_genuine() -> tuple[bool, str]:
    """自证"我们用的浏览器自动化是**未改装**的正版 playwright"。

    为什么要有这一条：本工程的边界是"未改装的浏览器"（`docs/07`）。而生态里存在
    **反检测改装的 playwright 分支**（patchright/rebrowser/undetected 等同 API fork），
    一旦被换进来，浏览器就不再"未改装"——而这个变化**完全静默**（代码一行没改）。

    三重核对（后两重是**重新打包时发现的混装漏洞**补的）：
      ① 模块路径里出现分支名 → 不合规；
      ② 发行包名不是 `playwright` → 不合规；
      ③ **driver 版本必须与 Python 包版本一致** —— 抓"正版 Python + 改装 driver"的
         **混装**：patchright 会装一个名叫 `hook-playwright.sync_api.py` 的 PyInstaller 钩子
         （内容却是 `collect_data_files("patchright")`），于是打包时给 `playwright.sync_api`
         收的是**改装分支的 node 驱动**（实测：Python 侧 1.62.0、驱动侧 1.61.1）。
         模块名仍然是 `playwright`，前两重核对都看不出来——只有版本对不上会露出来。
    """
    try:
        import playwright as _pw
        mod_dir = pathlib.Path(getattr(_pw, "__file__", "")).parent
        path = str(mod_dir).lower()
    except Exception as e:
        return False, f"取不到 playwright 路径（{type(e).__name__}: {e}）"
    known_forks = ("patchright", "rebrowser", "undetected", "playwright_stealth")  # noqa: lint -- 检测词表
    for fork in known_forks:
        if fork in path:
            return False, (f"检测到改装分支 `{fork}`（路径 {path}）——本工程只允许**未改装**的 "
                           f"playwright；相关能力已禁用")
    py_ver = ""
    try:
        import importlib.metadata as _md
        dist = _md.distribution("playwright")
        name = str(getattr(dist, "metadata", {}).get("Name", "") or "").lower()
        if name and name != "playwright":
            return False, f"playwright 这个发行包实际叫 `{name}`（疑似分支）——按未改装要求禁用"
        py_ver = str(dist.version or "")
    except Exception:
        pass
    drv_ver = _driver_version(mod_dir)
    if py_ver and drv_ver and py_ver != drv_ver:
        return False, (f"**驱动与 Python 包版本不一致**（Python {py_ver} / driver {drv_ver}）——"
                       f"这是装配混装的典型症状（正版 Python 配了改装分支配的驱动），按未改装要求禁用")
    return True, ""


def _browsers_root_scan(engine: str) -> tuple[str, str]:
    """退路：不看 `browsers.json`，直接扫浏览器根目录找可执行文件。

    为什么要有这条：**打包后** `browsers.json` 可能不在预期位置（它属于驱动数据）。
    此时若直接判"浏览器不可用"，就会出现"本机明明装了 chromium 却说没有"的假缺件。
    宁可如实说"没找到版本清单，用了已装的 chromium-<rev>"，也不要谎报缺件。
    """
    root = _browsers_root(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    if not root.exists():
        return "", f"浏览器根目录不存在：{root}"
    for c in sorted(root.glob(f"{engine}-*"), reverse=True):
        cand = c / ("chrome-win64/chrome.exe" if sys.platform.startswith("win")
                    else ("chrome-linux/chrome" if sys.platform != "darwin"
                          else "chrome-mac/Chromium.app/Contents/MacOS/Chromium"))
        if cand.exists():
            return str(cand), f"未找到 browsers.json，改用已装的 {c.name}"
    return "", f"{root} 下没有可用的 {engine} 目录"


def probe_browser(engine: str = "chromium") -> BrowserCapability:
    """探测浏览器可用性（**不启动任何进程**：只读 playwright 的 `browsers.json` 与二进制路径）。

    为什么不用 `sync_playwright()` 去问：那会真的起一个驱动进程——探测本身变成了重活，
    而且在"刚关掉一个浏览器"之后容易瞬时失败（S8 门禁第一次就踩到：C1 过了、C2/C3/C4 全被
    误判成"未就绪"）。就绪探测要**廉价、无副作用、可重复**。
    """
    try:
        import playwright as _pw_pkg
    except Exception as e:
        return BrowserCapability(reason=f"未安装 playwright（{type(e).__name__}: {e}）")

    genuine, why = _playwright_is_genuine()
    if not genuine:
        # 不合规（改装分支）→ 如实报告并禁用；**不尝试修正、不静默继续**
        return BrowserCapability(playwright=True, engine=engine, reason=why)

    base = pathlib.Path(getattr(_pw_pkg, "__file__", "")).parent
    meta = base / "driver" / "package" / "browsers.json"
    revision, ver = "", str(getattr(_pw_pkg, "__version__", "") or "")
    why = ""
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            for b in data.get("browsers", []):
                if str(b.get("name")) == engine:
                    revision = str(b.get("revision") or "")
                    break
            if not revision:
                why = f"playwright 不认识引擎 {engine}（browsers.json 里没有）"
        except Exception as e:
            why = f"读 browsers.json 失败（{type(e).__name__}: {e}）"
    else:
        why = f"找不到 playwright 的 browsers.json（{meta}）"

    root = _browsers_root(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    exe = ""
    if revision:
        p = _chromium_exe(root, revision)
        exe = str(p)
        if p.exists():
            return BrowserCapability(playwright=True, engine=engine, executable=exe,
                                     executable_exists=True, version=f"{engine}-{revision}",
                                     reason="")
        # 退一步：目录里可能有别的修订版（多版本共存是常态）
        cands = sorted(root.glob(f"{engine}-*")) if root.exists() else []
        for c in reversed(cands):
            alt = c / ("chrome-win64/chrome.exe" if sys.platform.startswith("win")
                       else ("chrome-linux/chrome" if not sys.platform == "darwin"
                             else "chrome-mac/Chromium.app/Contents/MacOS/Chromium"))
            if alt.exists():
                return BrowserCapability(playwright=True, engine=engine, executable=str(alt),
                                         executable_exists=True,
                                         version=f"{engine}-{c.name.split('-')[-1]}",
                                         reason=f"未找到 {engine}-{revision}，改用已装的 {c.name}")
        why = (why + "；" if why else "") + \
            f"浏览器二进制不存在于 {root}（用 `python -m playwright install {engine}` 安装；" \
            f"本工程**不静默安装**）"
    else:
        # **没有 browsers.json**（打包态的常见情形：它属于驱动数据）→
        # 退一步扫已装的浏览器，而不是谎报"缺件"（实测：打包后本机装了 chromium 却报不可用）
        exe, note = _browsers_root_scan(engine)
        if exe:
            return BrowserCapability(playwright=True, engine=engine, executable=exe,
                                     executable_exists=True, version=engine,
                                     reason=note)
        why = (why + "；" if why else "") + note
    return BrowserCapability(playwright=True, engine=engine, executable=exe,
                             executable_exists=False, version=ver, reason=why)


@dataclass
class ObservedRequest:
    """观察到的一次网络活动（**事实**，不是推测）。"""

    url: str
    method: str = "GET"
    resource: str = ""            # document / script / xhr / image / stylesheet / …
    status: int = 0
    mime: str = ""
    size: int = 0
    ok: bool = True
    denied: bool = False
    reason: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"url": self.url[:500], "method": self.method, "resource": self.resource,
                "status": int(self.status), "mime": self.mime[:120], "size": int(self.size),
                "ok": bool(self.ok), "denied": bool(self.denied),
                "reason": self.reason[:200], "at": round(self.at, 3)}


@dataclass
class BrowserVerdict:
    """一次浏览器观察的结果。"""

    ok: bool
    reason: str = ""
    final_url: str = ""
    title: str = ""
    status: int = 0
    html_sha256: str = ""
    html_size: int = 0
    observed: list[dict] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)
    captured_bytes: int = 0
    seconds: float = 0.0
    blocked: bool = False              # 被自己的闸挡在门外（**没起浏览器**）

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason[:300], "final_url": self.final_url[:300],
                "title": self.title[:200], "status": int(self.status),
                "html_sha256": self.html_sha256, "html_size": self.html_size,
                "observed_n": len(self.observed), "denied_n": len(self.denied),
                "captured_bytes": self.captured_bytes, "seconds": round(self.seconds, 3),
                "blocked": self.blocked}

    def summary(self) -> str:
        if self.blocked:
            return f"被拦未启动：{self.reason}"
        return (f"{'成功' if self.ok else '失败'}（{self.status}）；观察到 {len(self.observed)} 条，"
                f"拦截 {len(self.denied)} 条；捕获 {self.captured_bytes} 字节；"
                f"{self.seconds:.2f}s{('；' + self.reason) if self.reason else ''}")


class BrowserEnvironment:
    """浏览器运行时（进程 1 个、上下文/页有界、观察可落原始层）。

    `gate` / `robots` 是**本环境自己的**判定口（默认用 `net/ssrf_gate` + `net/robots`）——
    注入点留给测试（离线端到端要对着本地夹具服务器跑，就必须允许替换闸；
    产品默认永远是严格的那一套）。
    """

    def __init__(self, *, store=None, gate=None, robots=None, writer=None,
                 max_contexts: int = 0, max_pages: int = 0, engine: str = "chromium",
                 headless: bool = True, user_agent: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT, capture_bodies: bool = True,
                 max_capture_bytes: int = DEFAULT_MAX_CAPTURE,
                 max_observed: int = DEFAULT_MAX_OBSERVED,
                 accept_language: str = "zh-CN,zh;q=0.9,en;q=0.8",
                 playwright_factory=None):
        self.store = store
        self.gate = gate
        self.robots = robots
        self.writer = writer
        self.max_contexts = max(0, int(max_contexts))
        self.max_pages = max(0, int(max_pages))
        self.engine = engine
        self.headless = bool(headless)
        self.timeout = float(timeout)
        self.capture_bodies = bool(capture_bodies)
        self.max_capture_bytes = int(max_capture_bytes)
        self.max_observed = int(max_observed)
        self.accept_language = accept_language
        self._ua = user_agent
        self._factory = playwright_factory            # 测试可注入（离线夹具）
        self._lock = threading.Lock()
        self._ctx_used = 0
        self._page_used = 0
        # **槽位强制**（安全自审发现的真问题：原来只有计数、没有约束——并发 observe 能无限
        # 开上下文，等于"声明了槽位但没人管"）。语义是**缺省即拒绝**：拿不到槽位**不排队**，
        # 直接明确拒绝——排队会把"容量不足"藏起来，让上层以为一切正常而实际在堆积。
        self._ctx_slots = (threading.BoundedSemaphore(self.max_contexts)
                           if self.max_contexts > 0 else None)
        self._page_slots = (threading.BoundedSemaphore(self.max_pages)
                            if self.max_pages > 0 else None)
        self._calls = 0
        self._opened = 0
        self._closed = 0
        self._last_error = ""
        self._mgr = None          # playwright 上下文管理器
        self._pw = None
        self._browser = None

    # ── 就绪与能力 ───────────────────────────────────────────────
    def capability(self) -> dict:
        cap = probe_browser(self.engine)
        d = cap.to_dict()
        d.update({"max_contexts": self.max_contexts, "max_pages": self.max_pages,
                  "enabled": bool(self.max_contexts > 0 and self.max_pages > 0),
                  "calls": self._calls, "opened_pages": self._opened})
        if not d["enabled"]:
            d["reason"] = (d["reason"] + "；" if d["reason"] else "") + \
                "槽位为 0（资源计划里浏览器默认 0 = 不启用，缺省即拒绝）"
        return d

    def stats(self) -> dict:
        return {"calls": self._calls, "opened_pages": self._opened, "closed": self._closed,
                "contexts_in_use": self._ctx_used, "pages_in_use": self._page_used,
                "capability": self.capability(), "last_error": self._last_error}

    # ── 独立闸：判定一个 URL 能不能进浏览器面 ──────────────────────
    def decide(self, url: str) -> tuple[bool, str]:
        """本环境的**独立判定**：协议 + SSRF（含 DNS）+ robots。

        返回值 `(允许, 原因)`。这个函数是纯判定，**不发起任何网络活动**——
        所以它既能给入口用，也能给每个子资源用（route 处理器里逐条调用）。
        """
        u = str(url or "").strip()
        if not u:
            return False, "空 URL"
        if u.startswith(("data:", "blob:", "about:", "javascript:")):
            return False, f"协议不允许进入浏览器面（{u.split(':', 1)[0]}:）"
        if self.gate is not None:
            try:
                if self.gate(u):                 # 注入口：真实默认见 `default_gate()`
                    return False, "SSRF 闸拦截（私网/保留地址/非 http(s)）"
            except Exception as e:
                # 闸自己出错时**默认拒绝**（fail-closed）：宁可漏采，不可误入内网
                return False, f"闸判定异常，按拒绝处理（{type(e).__name__}: {e}）"
        if self.robots is not None:
            try:
                if not self.robots.allowed(u):
                    return False, "robots.txt 不允许"
            except Exception as e:
                return False, f"robots 判定异常，按拒绝处理（{type(e).__name__}: {e}）"
        return True, "ok"

    def default_gate(self):
        """默认闸：`u -> True 表示危险`（与 `net/fetch.py` 用的是同一个 `is_private_url`）。"""
        from daedalus.net.ssrf_gate import is_private_url
        return is_private_url

    # ── 主入口：观察一个页面 ─────────────────────────────────────
    def observe(self, url: str, *, wait_until: str = "load",
                settle_seconds: float = 0.5, capture_bodies: bool | None = None,
                max_capture_bytes: int | None = None) -> BrowserVerdict:
        """打开一个 URL，观察它的网络活动，把文档正文写进原始层。

        `settle_seconds`：load 之后再等一小会儿，让后续 XHR/渲染跑出来
        （"内容空壳"页面的正文常常这一小会儿才出现）。
        """
        t0 = time.monotonic()
        cap = probe_browser(self.engine)
        if not cap.available:
            self._last_error = cap.reason
            METRICS.inc("browser.refused", why="unavailable")
            return BrowserVerdict(False, f"浏览器不可用：{cap.reason}", seconds=time.monotonic() - t0)
        if self.max_contexts <= 0 or self.max_pages <= 0:
            self._last_error = "槽位为 0（缺省即拒绝）"
            METRICS.inc("browser.refused", why="no_slots")
            return BrowserVerdict(False, "浏览器槽位为 0（资源计划里默认不启用；"
                                         "要用请显式给 browser_contexts/browser_pages）",
                                  seconds=time.monotonic() - t0)
        allowed, why = self.decide(url)
        if not allowed:
            # **被拦的 URL 进不了浏览器面**：连浏览器都不启动（可验证的事实）
            self._last_error = why
            METRICS.inc("browser.blocked")
            return BrowserVerdict(False, why, blocked=True, seconds=time.monotonic() - t0)

        gate_fn = self.gate or self.default_gate()
        capture = self.capture_bodies if capture_bodies is None else bool(capture_bodies)
        cap_bytes = int(max_capture_bytes if max_capture_bytes is not None
                        else self.max_capture_bytes)
        observed: list[ObservedRequest] = []
        denied: list[dict] = []
        captured = {"bytes": 0, "sha256": "", "size": 0}

        # **先要槽位**（非阻塞）：拿不到就明确拒绝，**不排队**（见 __init__ 的说明）。
        # 顺序：上下文槽 → 页槽；任何一个失败都要把已拿到的还回去。
        got_ctx = got_page = False
        if self._ctx_slots is not None:
            got_ctx = self._ctx_slots.acquire(blocking=False)
            if not got_ctx:
                METRICS.inc("browser.slot_denied")
                return BrowserVerdict(False, f"浏览器上下文槽位已满"
                                             f"（{self.max_contexts} 个都在用）——缺省即拒绝，不排队",
                                      seconds=time.monotonic() - t0)
        if self._page_slots is not None:
            got_page = self._page_slots.acquire(blocking=False)
            if not got_page:
                if got_ctx and self._ctx_slots is not None:
                    self._ctx_slots.release()
                METRICS.inc("browser.slot_denied")
                return BrowserVerdict(False, f"浏览器页槽位已满"
                                             f"（{self.max_pages} 个都在用）——缺省即拒绝，不排队",
                                      seconds=time.monotonic() - t0)

        with self._lock:
            self._ctx_used += 1
            self._page_used += 1
            self._calls += 1
            self._opened += 1
        METRICS.inc("browser.calls")
        METRICS.set("browser.pages_open", self._page_used)
        try:
            pw, browser = self._ensure_browser()
            ctx = browser.new_context(user_agent=self._ua or self._honest_ua(),
                                      accept_downloads=False,
                                      locale="zh-CN",
                                      extra_http_headers={"Accept-Language": self.accept_language})
            page = ctx.new_page()
            page.set_default_timeout(self.timeout * 1000)

            def _on_route(route, request):
                u = request.url
                ok, reason = self._decide_subresource(u, gate_fn)
                if not ok:
                    denied.append({"url": u[:300], "reason": reason})
                    observed.append(ObservedRequest(url=u, method=request.method,
                                                    resource=request.resource_type,
                                                    ok=False, denied=True, reason=reason).to_dict())
                    _trim(observed, self.max_observed)
                    try:
                        route.abort()
                    except Exception:
                        pass
                    return
                try:
                    route.continue_()
                except Exception:
                    pass

            def _on_response(resp):
                try:
                    req = resp.request
                    hdrs = {}
                    try:
                        hdrs = resp.headers or {}
                    except Exception:
                        pass
                    rtype = str(req.resource_type or "")
                    size = 0
                    size_src = "none"
                    if capture and rtype in BODY_TYPES and captured["bytes"] < cap_bytes:
                        # 只读高价值类型的 body（读 body 会触发 Chromium 再取一遍，见文件头）
                        try:
                            body = resp.body()
                            size = len(body or b"")
                            size_src = "body"
                            if size:
                                captured["bytes"] += size
                                sha, sz = self._store_body(req.url, resp.status, hdrs, body,
                                                           note="observed_" + rtype)
                                if rtype == "document" and not captured["sha256"]:
                                    captured["sha256"], captured["size"] = sha, sz
                        except Exception:
                            size = 0
                    if not size:
                        # 不读 body 的类型：大小取 content-length（**记不到就记 0，不猜**）
                        cl = str(hdrs.get("content-length") or "")
                        if cl.isdigit():
                            size = int(cl)
                            size_src = "header"
                    observed.append(ObservedRequest(
                        url=resp.url, method=req.method, resource=rtype,
                        status=int(resp.status or 0), mime=str(hdrs.get("content-type") or ""),
                        size=size, ok=True, reason=f"size={size_src}").to_dict())
                    _trim(observed, self.max_observed)
                except Exception as e:
                    logger.debug("响应观察失败：%s", e)

            page.route("**/*", _on_route)
            page.on("response", _on_response)
            resp = page.goto(url, wait_until=wait_until, timeout=self.timeout * 1000)
            status = int(getattr(resp, "status", 0) or 0) if resp is not None else 0
            if settle_seconds > 0:
                try:
                    page.wait_for_timeout(int(settle_seconds * 1000))
                except Exception:
                    pass
            title = ""
            try:
                title = str(page.title() or "")
            except Exception:
                pass
            # 闸通过之后才允许的额外捕获：把渲染后的 HTML 也留一份（正文常常只在 DOM 里）
            html = ""
            try:
                html = str(page.content() or "")
            except Exception:
                pass
            final_url = ""
            try:
                final_url = str(page.url or "")
            except Exception:
                pass
            if capture and html and captured["bytes"] < cap_bytes:
                # 渲染后的 DOM 也留一份：正文常常只在 DOM 里（脚本注入），原始 HTML 里没有。
                # **必须受 `capture_bodies` 管**（曾经这里漏了判断，于是设 False 也照样写入——
                # S8 门禁 C3 抓到："我只想要元数据"的调用方被塞了字节）
                b = html.encode("utf-8", "ignore")
                captured["bytes"] += len(b)
                captured["sha256"], captured["size"] = self._store_body(
                    final_url or url, status, {"content-type": "text/html; charset=utf-8"}, b,
                    note="rendered_dom")
            try:
                ctx.close()
            except Exception:
                pass
            ok = bool(status and status < 400) or bool(html)
            reason = "" if ok else f"页面未取到内容（HTTP {status}）"
            METRICS.inc("browser.ok" if ok else "browser.fail")
            METRICS.inc("browser.observed", len(observed))
            METRICS.inc("browser.denied", len(denied))
            return BrowserVerdict(ok=ok, reason=reason, final_url=final_url, title=title,
                                  status=status, html_sha256=captured["sha256"],
                                  html_size=captured["size"], observed=list(observed),
                                  denied=list(denied), captured_bytes=captured["bytes"],
                                  seconds=time.monotonic() - t0)
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            METRICS.inc("browser.errors")
            logger.warning("浏览器观察失败（%s）：%s", url[:120], e)
            return BrowserVerdict(False, f"浏览器观察异常：{type(e).__name__}: {e}"[:300],
                                  observed=list(observed), denied=list(denied),
                                  seconds=time.monotonic() - t0)
        finally:
            # 还槽位（顺序与获取相反）；`BoundedSemaphore.release()` 超量会抛，所以按标志位还
            if got_page and self._page_slots is not None:
                try:
                    self._page_slots.release()
                except Exception:
                    pass
            if got_ctx and self._ctx_slots is not None:
                try:
                    self._ctx_slots.release()
                except Exception:
                    pass
            with self._lock:
                self._ctx_used = max(0, self._ctx_used - 1)
                self._page_used = max(0, self._page_used - 1)
            METRICS.set("browser.pages_open", self._page_used)

    # ── 内部 ────────────────────────────────────────────────────
    @staticmethod
    def _honest_ua() -> str:
        """诚实 UA（与直连咽喉同一套说法）：说明是本工具、用于个人采集。**不伪装浏览器**。"""
        try:
            from daedalus import VERSION
        except Exception:
            VERSION = "0"
        return f"Daedalus/{VERSION} (personal data collector; headless browser)"

    def _decide_subresource(self, url: str, gate_fn) -> tuple[bool, str]:
        u = str(url or "")
        if u.startswith(("data:", "blob:", "about:")):
            return True, "ok"                     # 页面自身的内联资源：不产生外部请求
        if u.startswith(("http://", "https://")):
            try:
                if gate_fn(u):
                    return False, "SSRF 闸拦截（私网/保留地址/非 http(s)）"
            except Exception as e:
                return False, f"闸判定异常，按拒绝（{type(e).__name__}）"
            if self.robots is not None:
                try:
                    if not self.robots.allowed(u):
                        return False, "robots.txt 不允许"
                except Exception:
                    return False, "robots 判定异常，按拒绝"
            return True, "ok"
        # 其它协议（ws/wss 等）也不放行：本环境只观察 http(s)
        return False, f"子资源协议不允许（{u.split(':', 1)[0]}:）"

    def _ensure_browser(self):
        """惰性启动浏览器（复用进程级 playwright；浏览器本身也复用）。启动失败把原因写清，不反复重试。"""
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return _PW["pw"], self._browser
            except Exception:
                pass
            self._browser = None
        pw = _acquire_playwright(self._factory)
        launcher = getattr(pw, self.engine)
        ua = self._ua or self._honest_ua()
        # UA **两处都给**：context 头负责常规请求；启动参数负责浏览器进程自己发起的那些
        # （实测有请求不走 context 头，会退回默认的 HeadlessChrome UA——见文件头那条坑）
        self._browser = launcher.launch(headless=self.headless, args=[f"--user-agent={ua}"])
        self._pw = pw
        METRICS.inc("browser.launched")
        return pw, self._browser

    def _store_body(self, url: str, status: int, headers: dict, body: bytes,
                    note: str = "") -> tuple[str, int]:
        """把观察到的字节写进**原始层**（与直连捕获同一套内容寻址存储）。"""
        if self.store is None:
            return "", 0
        try:
            art = self.store.put(body, url=url, status=int(status or 0), headers=headers or {},
                                 mime=str((headers or {}).get("content-type") or ""),
                                 source="browser/observe", note=note)
            METRICS.inc("rawstore.puts")
            METRICS.inc("rawstore.bytes", int(art.get("size", 0) or 0))
            return str(art.get("sha256", "")), int(art.get("size", 0) or 0)
        except Exception as e:
            logger.warning("浏览器观察写原始层失败：%s", e)
            METRICS.inc("rawstore.failed")
            return "", 0

    def close(self) -> dict:
        """关闭**浏览器进程**（playwright 驱动由进程级单例持有，见 `shutdown_playwright()`）。

        **不抛异常**；可重复调用（第二次是空操作）。
        """
        out = {"browser": False, "playwright": False, "why": ""}
        try:
            if self._browser is not None:
                self._browser.close()
                out["browser"] = True
        except Exception as e:
            out["why"] = f"关浏览器失败：{type(e).__name__}: {e}"
        finally:
            self._browser = None
            self._pw = None
        with self._lock:
            self._closed += 1
            self._ctx_used = self._page_used = 0
        METRICS.set("browser.pages_open", 0)
        return out


def _trim(observed: list, limit: int) -> None:
    """观察列表有界：页面能刷出成千上万条请求，**列表必须封顶**（保留最早的 N 条）。

    注意用的是**实例的** `max_observed`（曾经这里读的是模块常量，于是那个参数形同虚设——
    S8 门禁 C3 抓到：设了 25 却收了 121 条）。
    """
    if limit > 0 and len(observed) > limit:
        del observed[limit:]

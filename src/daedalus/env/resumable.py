# -*- coding: utf-8 -*-
"""带 SSRF 闸的探测 + 断点续传下载（线程模型就绪，同步实现）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/universal_downloader.py`
（`probe_url` / `_stream_one`），经《新工程开工包》去掉异步与平台耦合后重写。
Daedalus 内的改动（都是《03-可复用源码清单》点明的接入项）：
  1) 导入改为包内路径；
  2) **`verify` 失败必须删 `.part`**（原实现只删 min_bytes 分支，verify 失败会带着坏产物反复重试）；
  3) 新增 `extra_headers`（按域补 `Referer`/`Origin`，用于防盗链站点）；
  4) 新增 `expect_mime` / `reject_html`：下载"成功但拿到的是网页"要有出口；
  5) 新增 416 处理：分片已完整时服务端会回 416，此时能验证通过就收尾，而不是白重试。

────────────────────────────────────────────────────────────────
它解决什么
    大文件下到一半断了，不能从头再来；而且**下载通道必须和抓取通道过同一道闸**
    （Kiana 的真实事故：某下载通道漏了逐跳校验，外部 URL 302 指向内网 →
      内网正文被当作图片正常落盘）。

三条来自 Kiana 的教训（都已体现在实现里）
  1) **`.part` + Range 续传 + 字节数自校验 + 原子改名**：
     半截文件绝不能被下游当成完整文件；服务端**不支持 range（返回 200）时必须作废旧 `.part`**，
     否则会拼出损坏文件。
  2) **下载完要验证产物**（大小门槛 + 可选内容校验），别信"命令/请求成功"。
  3) **异常语义要分清**：被闸拦下（`BlockedError`）不可重试；网络失败可重试。

────────────────────────────────────────────────────────────────
用法

    from daedalus.env.resumable import probe, download

    pr = probe(url)                                      # 先握手（HEAD 回退 GET + Range:0-4）
    ok, path, why = download(url, dest_path, min_bytes=1024,
                             extra_headers={"Referer": "https://example.com/"})
    if not ok: 记录 why（可读原因），按可重试/不可重试分流

**为可测性设计**：所有 HTTP 调用都可注入 `opener`（默认是 `daedalus.net.ssrf_gate.safe_open`），
所以单测里不需要联网、也不需要在 localhost 起服务（localhost 会被闸拦下）。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from daedalus.net.ssrf_gate import BlockedError, RedirectLoopError, safe_open

__all__ = ["probe", "download", "ProbeResult"]

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_CHUNK = 256 * 1024


class ProbeResult:
    __slots__ = ("total", "resumable", "status", "reason")

    def __init__(self, total=None, resumable=False, status=0, reason=""):
        self.total = total              # 总字节数（未知为 None）
        self.resumable = resumable      # 服务端是否支持 Range
        self.status = status
        self.reason = reason            # 可读原因（失败时）

    def __repr__(self):
        return (f"ProbeResult(total={self.total}, resumable={self.resumable}, "
                f"status={self.status}, reason={self.reason!r})")


def _looks_like_html(headers: dict) -> bool:
    ct = str((headers or {}).get("content-type") or "").lower()
    return "text/html" in ct or "application/xhtml" in ct


def _ct_matches(headers: dict, expect_mime: tuple[str, ...]) -> bool:
    ct = str((headers or {}).get("content-type") or "").lower()
    return any(ct.startswith(m.lower()) for m in expect_mime)


def probe(url: str, *, opener=safe_open, timeout: float = 15,
          extra_headers: dict | None = None) -> ProbeResult:
    """下载前握手：HEAD（不被支持时回退带 Range 的 GET）→ 总大小 + 是否支持续传。"""
    base_headers = dict(extra_headers or {})
    for method in ("HEAD", "GET"):
        try:
            hdrs_out = dict(base_headers, **{"Range": "bytes=0-4"})
            with opener(url, method=method, headers=hdrs_out, timeout=timeout) as resp:
                if resp.status not in (200, 206):
                    if method == "GET":
                        return ProbeResult(status=resp.status,
                                           reason=f"探测返回 {resp.status}")
                    continue
                hdrs = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
                total, resumable = None, False
                m = _CONTENT_RANGE_RE.search(str(hdrs.get("content-range") or ""))
                if m and m.group(3) != "*":
                    total = int(m.group(3))
                elif str(hdrs.get("content-length") or "").isdigit():
                    total = int(hdrs["content-length"])
                resumable = (resp.status == 206
                             or str(hdrs.get("accept-ranges") or "").lower() == "bytes")
                if not resumable and method == "HEAD":
                    continue        # HEAD 拿不到续传信息 → 用 GET 再试一次
                return ProbeResult(total=total, resumable=resumable, status=resp.status)
        except (BlockedError, RedirectLoopError):
            raise                   # 闸拦下/重定向超限：交给调用方分流（不可重试/可重试）
        except Exception as e:
            if method == "GET":
                return ProbeResult(reason=f"探测异常: {type(e).__name__}: {e}")
    return ProbeResult(reason="探测失败（HEAD 与 GET 均不可用）")


def download(url: str, dest, *, opener=safe_open, timeout: float = 60,
             chunk: int = _CHUNK, max_retries: int = 3, min_bytes: int = 1024,
             verify=None, extra_headers: dict | None = None,
             expect_mime: tuple[str, ...] | None = None,
             reject_html: bool = True) -> tuple[bool, str | None, str]:
    """断点续传下载。

    参数
        dest          : 最终路径（下载过程写到 `dest + ".part"`）
        opener        : HTTP 调用（默认 `ssrf_gate.safe_open`，已带逐跳闸）
        min_bytes     : 产物大小门槛（**下载成功但文件过小 = 失败**）
        verify        : 可选校验函数 `verify(path) -> bool`（例如检查文件头/可播性）
        extra_headers : 追加请求头（按域补 Referer/Origin 用）
        expect_mime   : 期望的 Content-Type 前缀集合（给了就严格比对）
        reject_html   : 未给 expect_mime 时，若响应是 HTML 直接判失败（防盗链/错误页）

    返回 `(ok, path_or_None, reason)`；reason 是**可读原因**，便于记录与告警。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    def _drop_part(note: str) -> None:
        """校验失败/类型不符：**必须删** `.part`（否则下次续传会把坏数据拼进去）。"""
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        _ = note

    def _finalize(size: int) -> tuple[bool, str | None, str]:
        try:
            os.replace(part, dest)                # 原子改名：下游永远看不到半截文件
        except Exception as e:
            return False, None, f"原子改名失败: {e}"
        return True, str(dest), f"ok（{size} 字节）"

    last_reason = "未开始"
    for attempt in range(1, max_retries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = dict(extra_headers or {})
        mode = "wb"
        if have > 0:
            headers["Range"] = f"bytes={have}-"
            mode = "ab"          # 续写
        try:
            with opener(url, method="GET", headers=headers, timeout=timeout) as resp:
                rheaders = {str(k).lower(): v for k, v in (resp.headers or {}).items()}
                # 分片已完整时服务端回 416：能通过校验就收尾，不白重试
                if resp.status == 416 and have > 0:
                    if have >= int(min_bytes) and (verify is None or verify(part)):
                        return _finalize(have)
                    _drop_part("416 且校验不过")
                    last_reason = "416：既非完整产物，已作废旧 .part 重来"
                    continue
                # 服务端不支持 range 却回了 200：必须作废旧 .part，从头来
                if have > 0 and resp.status == 200:
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    have, mode, last_reason = 0, "wb", "服务端不支持续传，已从头开始"
                    continue
                if resp.status not in (200, 206):
                    last_reason = f"HTTP {resp.status}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                # "下载成功但是网页"的出口：类型不符直接删 .part 判失败
                if expect_mime is not None and not _ct_matches(rheaders, expect_mime):
                    _drop_part("mime")
                    return False, None, f"Content-Type 不符（期望 {expect_mime}，实际 {rheaders.get('content-type')!r}）"
                if expect_mime is None and reject_html and _looks_like_html(rheaders):
                    _drop_part("html")
                    return False, None, "拿到的是 HTML（疑似防盗链/错误页），已丢弃"
                with open(part, mode) as f:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        f.write(buf)
        except (BlockedError, RedirectLoopError) as e:
            return False, None, f"{type(e).__name__}: {e}"      # 闸/重定向：不再重试
        except Exception as e:
            last_reason = f"{type(e).__name__}: {e}"
            time.sleep(min(2 ** attempt, 8))
            continue

        # ── 完成后的自校验 ──────────────────────────────────────
        size = part.stat().st_size if part.exists() else 0
        if size < int(min_bytes):
            last_reason = f"产物过小（{size} < {min_bytes} 字节），疑似错误页/空文件"
            _drop_part("too-small")          # 垃圾不保留，避免下次续传出坏文件
            continue
        if verify is not None:
            try:
                ok_v = bool(verify(part))
            except Exception as e:
                last_reason = f"校验异常: {e}"
                _drop_part("verify-exc")
                continue
            if not ok_v:
                last_reason = "内容校验未通过（verify 返回 False）"
                _drop_part("verify")         # 移植改动 2：这里原来**没有**删，是个真 bug
                continue
        return _finalize(size)

    return False, None, f"重试 {max_retries} 次后失败：{last_reason}"

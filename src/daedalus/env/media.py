# -*- coding: utf-8 -*-
"""环境③制品与媒体：大对象与分片**不是"更大的 HTTP 请求"**

三条与其他环境不同的地方（《写作.txt》§五）：
  1) **资源模型不同**：Manifest → 分片计划 → 并行/分段取 → 完整性校验 → 装配 → 制品。
     所以它有自己的预算（时间更长、体积更大）与自己的执行槽位（子进程/外部工具）。
  2) **产物必须过契约**（`capture/artifacts.py`）：大小 + 格式/MIME + 内容哈希（+ 可播探测）。
     "命令返回 0"从来不算成功。
  3) **出网仍然只有一个咽喉**：分片、密钥、直链一律经 `Fetcher.open`（远端给的 URL 全部是不可信输入）。

缺件（没有 ffmpeg/ffprobe）不静默：`capability()` 明确列出"哪些能力因此不可用"。
"""

from __future__ import annotations

import logging
import pathlib

from daedalus.capture.artifacts import ArtifactContract, Verdict, check_artifact, for_media
from daedalus.exec.subprocess import readiness, run_tool, which
from daedalus.net.fetch import BlockedError

logger = logging.getLogger(__name__)

__all__ = ["MediaEnvironment"]


class MediaEnvironment:
    """制品与媒体环境（大文件 + 流媒体 + 文档制品）。"""

    def __init__(self, fetcher, *, workdir=None, concurrency: int = 4, tools=None):
        self.fetcher = fetcher
        self.workdir = pathlib.Path(workdir) if workdir else None
        self.concurrency = int(concurrency)
        self._tools = readiness(tools or ("ffmpeg", "ffprobe"))
        self._calls = 0

    # ── 能力探测（缺件要说出来）───────────────────────────────────
    def capability(self) -> dict:
        t = self._tools["tools"]
        return {
            "ffmpeg": t.get("ffmpeg", False),
            "ffprobe": t.get("ffprobe", False),
            "can_mux": bool(t.get("ffmpeg")),
            "can_probe_media": bool(t.get("ffprobe")),
            "missing": list(self._tools["missing"]),
            "note": ("缺少 ffmpeg → **不能合成媒体**（HLS 分片仍可下载，但不会自动拼装）；"
                     "缺少 ffprobe → 不能做「可播」验证。缺件不会让引擎崩，"
                     "但相关能力会被禁用并如实告知"),
        }

    # ── 大对象：探测 + 断点续传 + 契约 ───────────────────────────
    def probe(self, url: str, timeout: float = 15.0):
        from daedalus.env.resumable import probe
        self._calls += 1
        return probe(url, opener=self.fetcher.open, timeout=timeout)

    def download_large(self, url: str, dest, *, contract: ArtifactContract | None = None,
                       timeout: float = 300.0, min_bytes: int = 256,
                       extra_headers: dict | None = None,
                       expect_mime: tuple[str, ...] | None = None) -> Verdict:
        """分段/断点续传下载大对象，并用契约验证产物。

        `min_bytes` 只是**传输层的兜底**（防 0 字节/明显残缺）；"合格产物"的判断在**契约**里
        （它给更具体的原因：格式不符、是网页、哈希对不上……）。
        """
        from daedalus.env.resumable import download
        dest = pathlib.Path(dest)
        okk, path, why = download(url, dest, opener=self.fetcher.open, timeout=timeout,
                                 min_bytes=min_bytes, extra_headers=extra_headers,
                                 expect_mime=expect_mime)
        self._calls += 1
        if not okk:
            return Verdict(False, f"下载失败：{why}", {"url": url})
        return check_artifact(path, contract or for_media("large_file"))

    # ── 流媒体：HLS（分片并发 + 按序拼装 + 契约）───────────────────
    def download_hls(self, playlist_url: str, out_path, *,
                     contract: ArtifactContract | None = None,
                     timeout: float = 30.0, keep_segments: bool = False) -> Verdict:
        from daedalus.adapters.hls import download_hls
        if not self._tools["tools"].get("ffmpeg"):
            # 缺件**明确告知**（不静默降级成「下完分片但没拼装、看起来像成功」）
            return Verdict(False, "缺少 ffmpeg：HLS 分片可下载但无法拼装；"
                                  "请安装 ffmpeg 或改用 `keep_segments=True` 自行处理",
                           {"missing": "ffmpeg"})
        okk, path, why = download_hls(playlist_url, out_path, opener=self.fetcher.open,
                                     timeout=timeout, workdir=self.workdir,
                                     ffmpeg=which("ffmpeg"), ffprobe=which("ffprobe"),
                                     keep_segments=keep_segments,
                                     concurrency=self.concurrency, runner=self._runner)
        self._calls += 1
        if not okk:
            return Verdict(False, f"HLS 失败：{why}", {"playlist": playlist_url})
        return check_artifact(path, contract or for_media("hls"))

    @staticmethod
    def _runner(cmd, timeout: float = 1800.0, workdir: str | None = None):
        """把命令交给统一子进程执行面（`run_tool` 需要"工具名 + 参数"）。
        这里命令的第一个元素是显式路径，所以直接传 tool 并让 run_tool 走 FileNotFoundError→ToolMissing。"""
        tool, *args = [str(c) for c in cmd]
        return run_tool(tool, args, timeout=timeout, workdir=workdir, check_exists=False)

    # ── 文档制品（探测 + 契约）────────────────────────────────────
    def fetch_document(self, url: str, dest, *, contract: ArtifactContract | None = None) -> Verdict:
        from daedalus.capture.artifacts import for_document
        return self.download_large(url, dest, contract=contract or for_document("document"))

    def stats(self) -> dict:
        return {"calls": self._calls, "capability": self.capability(),
                "workdir": str(self.workdir) if self.workdir else None,
                "concurrency": self.concurrency}


def _unused_blocked_guard() -> tuple:
    """（保留）与本模块相关的异常类型清单，便于调用方 except 时对齐语义：
    `BlockedError` = 被闸拦下（不可重试）；`Throttled` = 被限流（独立计数）；
    `ToolMissing` = 缺件（硬报错）。"""
    return (BlockedError,)

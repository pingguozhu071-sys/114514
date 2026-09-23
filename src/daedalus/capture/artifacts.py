# -*- coding: utf-8 -*-
"""产物契约：**凡"看起来成功"都要再验证一次**

为什么单独立一个模块：Kiana 上"命令返回 0 但产物是坏的"出现过多次（ffmpeg 合并、yt-dlp 下载），
根因都是"把退出码当成了成功"。契约把"什么算合格的产物"变成**可核对的事实**：

    大小门槛 → MIME/文件头（魔数）→ 内容哈希（流式，不把大文件读进内存）→ 可选可读/可播探测

用法：
    contract = ArtifactContract(min_bytes=1024, expect_magic=("视频容器魔数",), label="hls")
    verdict = check_artifact(path, contract)     # → Verdict(ok, reason, facts)
`Verdict.facts` 带 `size/sha256/magic/head_hex`，直接进证据链与台账。
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field

from daedalus.frontier.dedup import stream_hash
from daedalus.understand.detect import detect

logger = logging.getLogger(__name__)

__all__ = ["ArtifactContract", "Verdict", "check_artifact", "mime_of_file", "for_media",
           "for_document", "for_image"]

# 常见"这是网页/错误页而不是我们要的东西"的魔数/开头
_HTML_MARKS = (b"<!doctype html", b"<html", b"<?xml", b"{", b"[")


@dataclass(frozen=True)
class ArtifactContract:
    """产物该满足什么。`label` 只用于日志与证据里说清"这是哪种产物"。"""

    label: str = "artifact"
    min_bytes: int = 1024
    max_bytes: int | None = None
    expect_mime_prefixes: tuple[str, ...] = ()      # 例如 ("video/", "audio/mpeg")
    expect_format: tuple[str, ...] = ()             # 探测链的格式名，例如 ("mp4","matroska")
    reject_html: bool = True                        # 服务器错误页/防盗链页
    require_readable: bool = False                  # 需要真去读一遍（例如能解码）

    def describe(self) -> str:
        bits = [f"≥{self.min_bytes}B", f"label={self.label}"]
        if self.max_bytes:
            bits.append(f"≤{self.max_bytes}B")
        if self.expect_format:
            bits.append("格式∈" + "/".join(self.expect_format))
        if self.expect_mime_prefixes:
            bits.append("MIME∈" + "/".join(self.expect_mime_prefixes))
        if self.reject_html:
            bits.append("拒收网页")
        return "、".join(bits)


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    facts: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.ok)


def mime_of_file(path) -> str:
    """尽量从文件头认出 MIME（只读头部若干字节，不解析整文件）。"""
    p = pathlib.Path(path)
    try:
        with p.open("rb") as fp:
            head = fp.read(4096)
    except Exception:
        return ""
    return detect(head).mime


def check_artifact(path, contract: ArtifactContract, *, want_sha256: bool = True) -> Verdict:
    """按契约验证产物。**绝不抛异常**（失败即 Verdict.ok=False + 可读原因）。"""
    p = pathlib.Path(path)
    facts: dict = {"path": str(p), "label": contract.label}
    if not p.exists():
        return Verdict(False, f"产物不存在：{p}", facts)
    try:
        size = p.stat().st_size
    except Exception as e:
        return Verdict(False, f"取不到大小：{type(e).__name__}: {e}", facts)
    facts["size"] = size
    if size < int(contract.min_bytes):
        return Verdict(False, f"产物过小（{size} < {contract.min_bytes} 字节），疑似错误页/空文件",
                       facts)
    if contract.max_bytes is not None and size > int(contract.max_bytes):
        return Verdict(False, f"产物过大（{size} > {contract.max_bytes} 字节）", facts)

    try:
        with p.open("rb") as fp:
            head = fp.read(4096)
    except Exception as e:
        return Verdict(False, f"读不到头部：{type(e).__name__}: {e}", facts)
    facts["head_hex"] = head[:8].hex()
    guess = detect(head, url=str(p))
    facts["format"] = guess.name
    facts["mime"] = guess.mime
    facts["format_how"] = guess.how

    if contract.reject_html and (guess.name in ("html", "json", "xml", "text")
                                 or head.lstrip()[:1].lower() in (b"<", b"{")):
        low = head.lstrip().lower()
        if any(low.startswith(m) for m in _HTML_MARKS):
            return Verdict(False, f"产物是网页/错误页（格式判定 {guess.name}，how={guess.how}）", facts)
    if contract.expect_format and guess.name not in contract.expect_format:
        return Verdict(False, f"格式不符：判定为 {guess.name}，期望 {'/'.join(contract.expect_format)}",
                       facts)
    if contract.expect_mime_prefixes and not any(
            guess.mime.startswith(pfx) for pfx in contract.expect_mime_prefixes):
        return Verdict(False, f"MIME 不符：{guess.mime}，期望 {'/'.join(contract.expect_mime_prefixes)}",
                       facts)
    if want_sha256:
        try:
            facts["sha256"] = stream_hash(p)          # **流式**：大文件不进内存
        except Exception as e:
            return Verdict(False, f"算哈希失败：{type(e).__name__}: {e}", facts)
    if contract.require_readable:
        try:
            with p.open("rb") as fp:
                fp.read(1)
        except Exception as e:
            return Verdict(False, f"产物不可读：{type(e).__name__}: {e}", facts)
    return Verdict(True, f"合格（{size} 字节，格式 {guess.name}）", facts)


# ── 常用契约（在调用点显式选，别到处拍脑袋写数字）─────────────────
def for_media(label: str = "media") -> ArtifactContract:
    """媒体制品：至少 100KB、不许是网页、需要能被探测出媒体格式。"""
    return ArtifactContract(label=label, min_bytes=100 * 1024, reject_html=True,
                            expect_format=("mp4", "matroska", "mp3", "m4a", "flac", "ogg",
                                           "riff", "mp4", "png", "jpeg", "gif", "webm"))


def for_document(label: str = "document") -> ArtifactContract:
    """文档制品：至少 1KB、允许 zip 类容器（docx/xlsx 就是 zip）。"""
    return ArtifactContract(label=label, min_bytes=1024, reject_html=True,
                            expect_format=("pdf", "zip", "text", "unknown"))


def for_image(label: str = "image") -> ArtifactContract:
    return ArtifactContract(label=label, min_bytes=512, reject_html=True,
                            expect_format=("png", "jpeg", "gif", "unknown"))

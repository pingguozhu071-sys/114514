# -*- coding: utf-8 -*-
"""探测链：**不靠猜**地判断"我拿到的是什么"（魔数 → 扩展名 → XML 根 → Content-Type → 文本 → 兜底）

顺序与理由（《可以.txt》§七 + 施工计划的 ch.6）：
  1) **魔数**最可信（`%PDF`、`\x89PNG`、`\x1f\x8b`…），文件字节不会撒谎；
  2) **扩展名**次之（`foo.m3u8`、`bar.pdf`），但可以被骗；
  3) **XML 根元素**（`<rss` / `<feed` / `<urlset`）——RSS/Sitemap 只能这么认；
  4) **声明的 Content-Type**（可能错、可能空、可能被站点写死成 octet-stream）——所以排在后面；
  5) **文本启发式**（可打印比例 + HTML 标记）；
  6) **兜底** `application/octet-stream`：认不出来就**如实说认不出来**——它会走"只捕获、延期解释"。

**绝不因为"认不出"而丢弃数据**：认不出也是结论（`unknown`），原始层照样保存。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["FormatGuess", "detect", "MAGIC_TABLE"]

# 魔数表（前缀 → (格式名, MIME)）。顺序有意义：更长的前缀放前面。
MAGIC_TABLE: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpeg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"%PDF-", "pdf", "application/pdf"),
    (b"PK\x03\x04", "zip", "application/zip"),          # docx/xlsx/pptx 也是 zip
    (b"\x1f\x8b", "gzip", "application/gzip"),
    (b"BZh", "bzip2", "application/x-bzip2"),
    (b"7z\xbc\xaf\x27\x1c", "7z", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "rar", "application/vnd.rar"),
    (b"ID3", "mp3", "audio/mpeg"),
    (b"OggS", "ogg", "application/ogg"),
    (b"fLaC", "flac", "audio/flac"),
    (b"RIFF", "riff", "application/octet-stream"),      # 具体是 wav/avi 要看第 8-12 字节
    (b"\x1aE\xdf\xa3", "matroska", "video/x-matroska"),  # mkv/webm
    (b"\x00\x00\x00\x18ftyp", "mp4", "video/mp4"),
    (b"\x00\x00\x00\x1cftyp", "mp4", "video/mp4"),
    (b"\x00\x00\x00\x20ftyp", "mp4", "video/mp4"),
    (b"\x7fELF", "elf", "application/x-elf"),
    (b"MZ", "pe", "application/vnd.microsoft.portable-executable"),
    (b"SQLite format 3\x00", "sqlite", "application/vnd.sqlite3"),
)

_XML_ROOTS = ((r"<\s*rss", "rss", "application/rss+xml"),
              (r"<\s*feed", "atom", "application/atom+xml"),
              (r"<\s*urlset", "sitemap", "application/xml"),
              (r"<\s*sitemapindex", "sitemap_index", "application/xml"),
              (r"<\s*html", "html", "text/html"),
              (r"<!DOCTYPE\s+html", "html", "text/html"))
_M3U8_MARK = b"#EXTM3U"
_JSON_TOP = (b"{", b"[")

_EXT_MAP = {".pdf": ("pdf", "application/pdf"), ".png": ("png", "image/png"),
            ".jpg": ("jpeg", "image/jpeg"), ".jpeg": ("jpeg", "image/jpeg"),
            ".gif": ("gif", "image/gif"), ".zip": ("zip", "application/zip"),
            ".gz": ("gzip", "application/gzip"), ".m3u8": ("hls", "application/vnd.apple.mpegurl"),
            ".mpd": ("dash", "application/dash+xml"), ".mp4": ("mp4", "video/mp4"),
            ".mkv": ("matroska", "video/x-matroska"), ".mp3": ("mp3", "audio/mpeg"),
            ".m4a": ("m4a", "audio/mp4"), ".flac": ("flac", "audio/flac"),
            ".html": ("html", "text/html"), ".htm": ("html", "text/html"),
            ".json": ("json", "application/json"), ".xml": ("xml", "application/xml"),
            ".rss": ("rss", "application/rss+xml"), ".txt": ("text", "text/plain"),
            ".md": ("text", "text/markdown"), ".csv": ("csv", "text/csv")}

# 通用容器后缀：只当弱提示，不阻断后续（根元素/内容）判断；全链都没结论时才用它兜底
_GENERIC_EXTS = frozenset({".xml", ".txt", ".bin", ".dat", ".rss", ".atom"})


@dataclass(frozen=True)
class FormatGuess:
    """探测结论。`how` 说明"凭什么这么判"（**必须能说出来**），`confidence` 0–1。"""

    name: str                    # pdf/png/html/json/rss/atom/sitemap/hls/text/unknown…
    mime: str
    how: str                     # magic|extension|xml-root|content-type|json|text|fallback
    confidence: float
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.name != "unknown"


def _printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    sample = data[:4096]
    ok = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b < 127 or b >= 0x80)
    return ok / len(sample)


def detect(data: bytes | None, meta: dict | None = None,
           url: str | None = None) -> FormatGuess:
    """按探测链给出结论（顺序见文件头）。`meta` 可含 `content_type`；`url` 用于看扩展名。"""
    meta = dict(meta or {})
    blob = bytes(data) if data else b""
    head = blob[:4096]

    # ① 魔数
    for magic, name, mime in MAGIC_TABLE:
        if head.startswith(magic):
            return FormatGuess(name, mime, "magic", 0.99, detail=magic[:8].hex())
    if _M3U8_MARK in head[:512]:
        return FormatGuess("hls", "application/vnd.apple.mpegurl", "magic", 0.95,
                           detail="#EXTM3U")

    # ② 扩展名（从 URL 或 meta.filename 里取）。
    #    **通用容器后缀**（.xml/.txt/.bin/.dat）只当弱提示：它只说"是个 XML/文本"，
    #    不说"哪种 XML"——所以要继续往下走，让根元素/内容来定（否则 `feed.xml` 会被判成
    #    泛泛的 "xml"，丢掉 rss/atom/sitemap 的区分）。
    path = str(meta.get("filename") or "")
    if not path and url:
        path = str(url).split("?", 1)[0]
    suffix = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    generic_hint = None
    if suffix in _EXT_MAP:
        if suffix in _GENERIC_EXTS:
            generic_hint = _EXT_MAP[suffix]
        else:
            name, mime = _EXT_MAP[suffix]
            return FormatGuess(name, mime, "extension", 0.7, detail=suffix)

    # ③ XML 根元素（RSS/Atom/Sitemap 只能这么认）
    text_head = head[:2048].decode("utf-8", "replace").lstrip("\ufeff \t\r\n")
    if text_head.startswith("<?xml") or text_head.startswith("<"):
        for pattern, name, mime in _XML_ROOTS:
            if re.search(pattern, text_head[:1024], re.IGNORECASE):
                return FormatGuess(name, mime, "xml-root", 0.85, detail=pattern)

    # ④ 声明的 Content-Type（可能错/可能空，所以排在后面）
    ctype = str(meta.get("content_type") or "").split(";", 1)[0].strip().lower()
    if ctype and ctype != "application/octet-stream":
        base = ctype.split("/", 1)[0]
        if "html" in ctype:
            return FormatGuess("html", "text/html", "content-type", 0.6, detail=ctype)
        if "json" in ctype:
            return FormatGuess("json", "application/json", "content-type", 0.6, detail=ctype)
        if "xml" in ctype:
            return FormatGuess("xml", "application/xml", "content-type", 0.6, detail=ctype)
        if ctype.startswith("text/"):
            return FormatGuess("text", ctype, "content-type", 0.6, detail=ctype)
        if base in ("image", "audio", "video", "font"):
            return FormatGuess(ctype.split("/", 1)[1], ctype, "content-type", 0.55, detail=ctype)

    # ⑤ JSON（先看首字符再真解析，避免把 "{不是JSON" 误判）
    stripped = head.lstrip()[:1]
    if stripped in _JSON_TOP and blob:
        try:
            json.loads(blob[:65536].decode("utf-8", "replace"))
            return FormatGuess("json", "application/json", "json", 0.75)
        except Exception:
            pass

    # ⑥ 文本启发式
    ratio = _printable_ratio(head)
    if blob and ratio > 0.92:
        low = head[:1024].lower()
        if b"<html" in low or b"<!doctype html" in low:
            return FormatGuess("html", "text/html", "text", 0.65)
        return FormatGuess("text", "text/plain", "text", 0.55, detail=f"printable={ratio:.2f}")

    # ⑦ 兜底：认不出来就如实说（会走"只捕获、延期解释"）
    if generic_hint is not None:
        name, mime = generic_hint
        return FormatGuess(name, mime, "extension", 0.4,
                           detail=f"{suffix}（通用后缀，未能进一步分辨）")
    return FormatGuess("unknown", "application/octet-stream", "fallback", 0.2,
                       detail=f"printable={ratio:.2f} head={head[:8].hex()}")

# -*- coding: utf-8 -*-
"""原始层（Raw Artifact Store）：**先捕获，后理解** 的那个"捕获"

四条设计（对应《写作.txt》§十一"Capture First 才是地基"）：
  1) **内容寻址**：文件名 = 内容 sha256，按前两位分片目录（`raw/ab/abcd….bin`）。
     同一内容重复落盘 → 直接复用（去重）；不同 URL 同内容也共用一份字节。
  2) **逐记录压缩**：文本类（text/*、json、xml、html）大于阈值就用 gzip，**由后缀自描述**
     （`.bin.gz`），读取端不需要额外元数据就知道要不要解压。
  3) **血缘齐全**：URL、状态码、响应头、MIME、大小、hash、来源、会话、父任务、发现路径。
     —— 这些字段是"以后能重新解释"的前提。
  4) **永不脱敏、永不裁剪**：原始层是事实层（`SanitizationPolicy.for_raw_layer` 恒等）。
     响应头落库前**在写库那一步**才过 `sanitize_headers`（受策略开关控制），
     而磁盘上的原始字节与元数据文件始终是原样。

写盘是**原子**的（临时文件 + `os.replace`），下游永远不会看到半截文件。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import pathlib
import time

logger = logging.getLogger(__name__)

__all__ = ["RawStore", "content_sha256"]

# 超过这个大小才压缩（小文件压了反而更慢）
COMPRESS_MIN_BYTES = 1024
_TEXTY_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/rss",
                        "application/atom", "application/xhtml", "application/javascript",
                        "application/vnd.apple.mpegurl")


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RawStore:
    """内容寻址的原始层。`put()` 落盘并登记元数据；`read()` 取回字节。"""

    def __init__(self, root, db, writer, compress_text: bool = True):
        self.root = pathlib.Path(root)
        self.db = db
        self.writer = writer
        self.compress_text = bool(compress_text)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)

    # ── 写入 ─────────────────────────────────────────────────────
    def put(self, data: bytes, *, url: str, status: int = 200, headers: dict | None = None,
            mime: str = "", source: str = "", session_id: str = "",
            parent_task: str = "", discovery_path: str = "", note: str = "") -> dict:
        """落盘 + 登记。返回 artifact 字典（可直接喂给 `Frontier.commit_done(artifact=...)`）。"""
        if data is None:
            raise ValueError("raw 层不接受 None（事实层不能有空值）")
        blob = bytes(data)
        sha = content_sha256(blob)
        mime_l = str(mime or "").lower()
        use_gz = (self.compress_text and len(blob) >= COMPRESS_MIN_BYTES
                  and any(mime_l.startswith(p) for p in _TEXTY_MIME_PREFIXES))
        ext = ".bin.gz" if use_gz else ".bin"
        rel = pathlib.Path("raw") / sha[:2] / f"{sha}{ext}"
        dest = self.root / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            payload = gzip.compress(blob) if use_gz else blob
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, dest)                      # 原子：下游看不到半截文件
        art = {
            "sha256": sha, "url": url, "size": len(blob), "mime": mime,
            "status": int(status),
            "headers_json": json.dumps(dict(headers or {}), ensure_ascii=False)[:20_000],
            "fetched_at": time.time(), "source": source, "session_id": session_id,
            "parent_task": parent_task, "discovery_path": discovery_path,
            "path": rel.as_posix(), "note": note,
        }
        self._insert(art)
        return art

    def _insert(self, art: dict) -> None:
        """登记元数据（**去重**：同 (sha256, url) 只留一条）。"""
        def job(conn):
            conn.execute(
                "INSERT OR IGNORE INTO raw_artifacts (sha256, url, size, mime, status, "
                "headers_json, fetched_at, source, session_id, parent_task, discovery_path, "
                "path, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (art["sha256"], art["url"], int(art["size"]), art.get("mime", ""),
                 int(art.get("status", 0)), art.get("headers_json", "{}"),
                 float(art.get("fetched_at", time.time())), art.get("source", ""),
                 art.get("session_id", ""), art.get("parent_task", ""),
                 art.get("discovery_path", ""), art.get("path", ""), art.get("note", "")))
            return True
        self.writer.run_now(job, label="rawstore.put")

    # ── 读取 ─────────────────────────────────────────────────────
    def path_for(self, sha256: str, compressed: bool | None = None) -> pathlib.Path:
        if compressed is None:
            gz = (self.root / "raw" / sha256[:2] / f"{sha256}.bin.gz")
            compressed = gz.exists()
        name = f"{sha256}.bin.gz" if compressed else f"{sha256}.bin"
        return self.root / "raw" / sha256[:2] / name

    def read(self, sha256: str) -> bytes:
        """取回原始字节（自动识别是否 gzip；缺失则抛 FileNotFoundError）。"""
        p = self.path_for(sha256)
        if not p.exists():
            raise FileNotFoundError(f"原始层里没有 {sha256}（{p}）")
        blob = p.read_bytes()
        return gzip.decompress(blob) if p.suffix == ".gz" else blob

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def verify(self, sha256: str, expected_size: int | None = None) -> tuple[bool, str]:
        """校验完整性（**"看起来成功"要再验证一次**）。"""
        try:
            blob = self.read(sha256)
        except Exception as e:
            return False, f"读取失败: {type(e).__name__}: {e}"
        got = content_sha256(blob)
        if got != sha256:
            return False, f"内容寻址不符：期望 {sha256[:12]}…，实得 {got[:12]}…"
        if expected_size is not None and len(blob) != int(expected_size):
            return False, f"大小不符：期望 {expected_size}，实得 {len(blob)}"
        return True, f"ok（{len(blob)} 字节）"

    def stats(self) -> dict:
        def job(conn):
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS total FROM raw_artifacts"
            ).fetchone()
            files = sum(1 for _ in (self.root / "raw").rglob("*.bin*"))
            return {"rows": int(row["n"] if row else 0),
                    "bytes_indexed": int(row["total"] if row else 0),
                    "files_on_disk": files, "root": str(self.root)}
        return self.writer.run_now(job, label="rawstore.stats")

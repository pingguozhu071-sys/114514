# -*- coding: utf-8 -*-
"""制品/文档元数据解析器（**必须进程隔离**）

为什么必须隔离：解析"来路不明的字节"是典型的不可信输入处理——PDF/图片/压缩包里可以塞
死循环、内存炸弹、畸形结构。所以本解析器**声明需要进程隔离**（`REQUIRES_PROCESS_ISOLATION = True`），
由 `run_isolated()` 在**独立进程**里跑；超时就**终止进程**（这正是隔离的好处之一：
线程杀不掉，进程可以）。进程槽位的容量来自资源注册表——**缺省即拒绝**（默认容量 0）。

能提取什么（**老实说，不全**）：
  * 图片：格式/尺寸/模式（Pillow）；
  * 音视频：容器/时长/码率/流数组（ffprobe；缺 ffprobe 就如实说缺件）；
  * 压缩容器（zip/docx/xlsx/pptx）：条目名与大小（stdlib zipfile）；
  * PDF：识别 + 基本结构统计（页对象数）；**不**做文本抽取（那需要额外库，本工程暂不引入）。
提取不到的部分**明说**，不猜。
"""

from __future__ import annotations

import io
import logging
import multiprocessing as mp
import queue as _queue
import zipfile
from dataclasses import dataclass

from daedalus.understand.registry import ParserSpec

logger = logging.getLogger(__name__)

__all__ = ["SPEC", "parse_mediainfo", "run_isolated", "REQUIRES_PROCESS_ISOLATION"]

VERSION = 1
REQUIRES_PROCESS_ISOLATION = True


def _image_meta(data: bytes) -> dict:
    try:
        from PIL import Image
    except Exception as e:
        return {"error": f"缺 Pillow：{type(e).__name__}"}
    try:
        with Image.open(io.BytesIO(data)) as im:
            return {"format": im.format, "width": im.width, "height": im.height,
                    "mode": im.mode, "n_frames": getattr(im, "n_frames", 1)}
    except Exception as e:
        return {"error": f"图片解析失败：{type(e).__name__}: {e}"}


def _zip_meta(data: bytes, limit: int = 200) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()[:limit]
            return {"entries": len(zf.infolist()),
                    "names": [i.filename for i in infos],
                    "uncompressed_bytes": sum(int(i.file_size) for i in infos)}
    except Exception as e:
        return {"error": f"zip 解析失败：{type(e).__name__}: {e}"}


def _pdf_meta(data: bytes) -> dict:
    # 不做文本抽取（需额外库）；只给"能不能看出是 PDF 结构 + 页对象粗计"
    pages = data.count(b"/Type /Page") + data.count(b"/Type/Page")
    return {"pages_rough": pages, "has_xref": b"xref" in data[:2048] or b"/XRef" in data,
            "note": "仅结构统计；文本抽取需额外库（本工程暂不引入）"}


def _media_meta(path: str) -> dict:
    """用 ffprobe 取媒体元数据（缺件如实说）。"""
    from daedalus.exec.subprocess import ToolMissing, which, run_tool
    if not which("ffprobe"):
        return {"error": "缺 ffprobe：无法读取媒体元数据（不是解析失败，是环境缺件）"}
    try:
        res = run_tool("ffprobe", ["-v", "error", "-print_format", "json",
                                   "-show_format", "-show_streams", path], timeout=60)
    except ToolMissing as e:
        return {"error": str(e)}
    if not res.ok:
        return {"error": f"ffprobe 失败：{res.brief()}"}
    import json
    try:
        obj = json.loads(res.stdout or "{}")
        fmt = obj.get("format") or {}
        streams = obj.get("streams") or []
        return {"container": fmt.get("format_name"), "duration": fmt.get("duration"),
                "bit_rate": fmt.get("bit_rate"),
                "streams": [{"codec_type": s.get("codec_type"), "codec_name": s.get("codec_name"),
                             "width": s.get("width"), "height": s.get("height"),
                             "sample_rate": s.get("sample_rate")} for s in streams]}
    except Exception as e:
        return {"error": f"ffprobe 输出解析失败：{type(e).__name__}"}


def parse_mediainfo(data: bytes, meta: dict) -> dict:
    """在隔离进程里被调用（见 `run_isolated`）。返回同构字典。"""
    fmt = str(meta.get("format") or "")
    out: dict = {"kind": "artifact_meta", "format": fmt}
    if fmt in ("pdf",):
        out.update(_pdf_meta(data))
    elif fmt in ("zip",):
        out.update(_zip_meta(data))
    elif fmt in ("png", "jpeg", "gif"):
        out.update(_image_meta(data))
    else:
        path = str(meta.get("path") or "")
        if path:
            out.update(_media_meta(path))
        else:
            out["note"] = "既不是图片/压缩容器，也没有本地路径可供探测（媒体元数据需要落盘后的路径）"
    if out.get("error") and not any(k not in ("kind", "format", "error") for k in out):
        return {"ok": False, "error": str(out["error"]), **out}
    return {"ok": True, **out}


def _child_entry(payload, out_q) -> None:
    """隔离子进程的入口。**必须是模块级函数**：`spawn` 启动方式要能 pickle 它——
    写成闭包/局部函数会得到 `PicklingError: Can't pickle local object`
    （本工程门禁 D2/D3 抓到过）。"""
    try:
        fn, data, meta = payload
        out_q.put(fn(data, meta))
    except Exception as e:                            # 子进程里崩了也不能带塌父进程
        try:
            out_q.put({"ok": False, "error": f"子进程异常：{type(e).__name__}: {e}"})
        except Exception:
            pass


def run_isolated(fn, arg, *, timeout: float = 20.0, registry=None,
                 capacity_name: str = "process") -> dict:
    """在**独立进程**里跑解析（不可信输入处理）。

    * 进程槽位容量来自注册表（`capacity_name="process"`，**默认 0 → 直接拒绝**）；
    * 超时**终止子进程**（隔离的第二个好处：线程杀不掉，进程可以）；
    * 返回值同样走同构字典：失败给可读原因，不抛异常（除非资源被拒——那是配置问题，要让人看见）。

    ⚠️ `fn` 必须是**模块级函数**（spawn 要 pickle 它）；`arg` 也必须是可 pickle 的。
    """
    if registry is not None:
        from daedalus.core.registry import ResourceDenied
        if registry.capacity(capacity_name) <= 0:
            raise ResourceDenied(
                f"资源 {capacity_name} 未启用（容量 0）——**解析不可信内容需要进程隔离**，"
                f"请显式登记容量（缺省即拒绝）")
    ctx = mp.get_context("spawn")                     # spawn：Windows 上唯一稳妥的启动方式
    q: "mp.Queue" = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_child_entry, args=((fn, arg[0], arg[1]), q), daemon=True)
    proc.start()
    proc.join(timeout=float(timeout))
    if proc.is_alive():
        proc.terminate()                              # ← 隔离下可以终止（线程做不到）
        proc.join(5)
        return {"ok": False, "error": f"解析超时（{timeout}s）→ 已终止隔离进程"}
    try:
        return q.get_nowait()
    except _queue.Empty:
        return {"ok": False, "error": "隔离进程没有返回结果（可能被系统杀掉或崩溃）"}


SPEC = ParserSpec(name="artifact_meta", version=VERSION,
                  accepts=("pdf", "zip", "png", "jpeg", "gif", "mp4", "matroska", "mp3",
                           "m4a", "flac", "ogg", "riff", "unknown"),
                  parse=lambda data, meta: parse_mediainfo(data, meta), order=90,
                  note="制品/文档元数据；**需要进程隔离**（见 run_isolated）")

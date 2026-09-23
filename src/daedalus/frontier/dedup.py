# -*- coding: utf-8 -*-
"""内容指纹与 SimHash 近似去重（纯同步，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/parser.py`，经《新工程开工包》精简重写。
Daedalus 内的改动：
  1) 导入示例改为包内路径；新增 `FINGERPRINT_VERSION`（指纹算法版本号——算法一改，
     新旧值"可比不可混"，见 docs/11）；
  2) 新增 `stream_hash()`：大文件/媒体用**流式**哈希，绝不把整个文件读进内存；
  3) 明确约定：`clamp63()` 是**写库路径的强制入口**（不是"调用方记得用"）。

────────────────────────────────────────────────────────────────
为什么需要两级去重
    精确去重（内容哈希）解决"同一条 URL 抓了两次"；
    近似去重（SimHash + 汉明距离）解决"同一内容在不同 URL / 带不同参数 / 站内转载"。
    去重是"任务模型的一等公民"：命中即"未变"，是**一等结果**，要有记录（不是"没抓到"）。

⚠️ 一个必须记住的坑（Kiana 真实事故）
    SimHash 是 64 位整数，而 SQLite 的 INTEGER 上限是 **2^63−1**。
    超出会**让批量提交整批回滚**——表现为"数据静默丢失"，极难定位。
    **入库前必须钳到 63 位**：`clamp63(v)`。

用法
    from daedalus.frontier.dedup import content_hash, simhash64, clamp63, is_near_dup, stream_hash

    h_text = content_hash(page_text)          # 精确去重键
    h_sim  = clamp63(simhash64(page_text))    # 入库前钳位！存进 SQLite 的 simhash 列
    if is_near_dup(h_sim, db_sim): 跳过        # 与同域已抓页面比汉明距离
    h_file = stream_hash(path)                # 媒体/大文件：流式，不占内存
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "content_hash", "simhash64", "clamp63", "hamming", "is_near_dup", "tokens_of",
    "stream_hash", "FINGERPRINT_VERSION", "NEAR_DUP_THRESHOLD",
]

# 指纹算法版本：改动分词/位宽/哈希时递增；库里保留旧值但**不与新值混比**
FINGERPRINT_VERSION = 1

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# 汉明距离阈值：≤3 视为近似重复（SimHash 的常规经验值；语料越杂可调到 2~4）
NEAR_DUP_THRESHOLD = 3
_SQLITE_MAX_INT = (1 << 63) - 1

# 单文档参与 SimHash 的词元上限（超出按等步长抽样）。
# 为什么要有：分词是一次性把整篇文字切成一堆小字符串，**CPU 与页大小成正比**——
# 一个 10MB 的页面在"解析"这一步就能把工作线程占住好几秒（等于给自己制造挂死）。
# SimHash 本身是概率结构（位数固定 64），抽样不改变量级，却把最坏情况的 CPU 上界钉死。
# 20000 个词元 ≈ 20 万字符正文，正常网页全量参与、不触顶。
MAX_SIMHASH_TOKENS = 20000

# ── 位累加的快速通道 ─────────────────────────────────────────────
# 朴素写法是"每个词元 → 内层 64 次循环逐位累加"，实测占单线程总时间的 **81%**
# （见 DEVLOG S7：24KB 页面 16ms/条）。这里换成**分道大整数**：
#   8 个字节表项，每个表项把一个字节的 8 个比特摊进 8 条 16 位"车道"，
#   于是每个词元只需 8 次查表 + 8 次移位 + 8 次加法（C 层完成），而不是 64 次 Python 循环。
# 车道宽 16 位 → 计数上限 65535，超过就回退到分块累加（见 `_simhash_accumulate`）。
_LANE_BITS = 16
_LANE_MASK = (1 << _LANE_BITS) - 1
_SPREAD: tuple[int, ...] = tuple(
    sum((1 << (_LANE_BITS * bit)) for bit in range(8) if (byte >> bit) & 1)
    for byte in range(256))


def _accumulate_ones(hashes) -> tuple[int, int]:
    """把一批 64 位哈希**逐位求 1 的个数**。返回 `(大整数分道累加值, 词元数)`。

    分块是为了防车道溢出（16 位车道放 65535，取 4096 一块留足余量）。
    """
    acc = 0
    n = 0
    chunk_max = 4096
    chunk: list[int] = []

    def flush() -> None:
        nonlocal acc, n
        if not chunk:
            return
        for h in chunk:
            b = h.to_bytes(8, "little")
            acc += (_SPREAD[b[0]] + (_SPREAD[b[1]] << 128) + (_SPREAD[b[2]] << 256)
                    + (_SPREAD[b[3]] << 384) + (_SPREAD[b[4]] << 512)
                    + (_SPREAD[b[5]] << 640) + (_SPREAD[b[6]] << 768)
                    + (_SPREAD[b[7]] << 896))
        n += len(chunk)
        chunk.clear()

    for h in hashes:
        chunk.append(h & ((1 << 64) - 1))
        if len(chunk) >= chunk_max:
            flush()
    flush()
    return acc, n


def tokens_of(text: str, *, max_tokens: int = MAX_SIMHASH_TOKENS) -> list[str]:
    """分词（零依赖版）：拉丁词 + **中文二元组**。

    中文若按单字切分，短文本的特征太少、区分度差；重叠二元组是性价比最高的做法
    （不引分词库，避免给新工程增加依赖与部署负担）。

    `max_tokens`：超过就**等步长抽样**（不是截断——截断会让"长文档只看开头"，
    抽样至少让整篇都参与）。抽样是**有意的近似**，见 `MAX_SIMHASH_TOKENS` 的说明。
    """
    t = (text or "").lower()
    out = _WORD_RE.findall(t)
    for run in _CJK_RE.findall(t):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    if max_tokens > 0 and len(out) > max_tokens:
        stride = len(out) / float(max_tokens)
        out = [out[int(i * stride)] for i in range(max_tokens)]
    return out


def content_hash(text: str, n: int = 16) -> str:
    """精确内容哈希（取前 n 个 hex 字符；够用且短）。"""
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:n]


def stream_hash(path, chunk: int = 1 << 20, n: int = 64) -> str:
    """大文件/媒体的**流式**哈希（分块读，绝不整文件入内存）。

    为什么不用 content_hash：媒体动辄几百 MB，读进内存会把常驻内存预算打穿
    （预算见施工计划 §2-4：常驻 ≤4GB）。
    """
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            blk = fp.read(chunk)
            if not blk:
                break
            h.update(blk)
    return h.hexdigest()[:n]


def simhash64(text: str, *, max_tokens: int = MAX_SIMHASH_TOKENS) -> int:
    """64 位 SimHash。返回**无符号** 64 位值——入库前记得 clamp63()。

    语义与朴素实现**逐位等价**（`ones > N/2` 等价于 `Σ(+1/-1) > 0`，平票都是 0），
    只是把 64 次 Python 内层循环换成分道大整数加法（见 `_accumulate_ones`）。
    """
    toks = tokens_of(text, max_tokens=max_tokens)
    if not toks:
        return 0
    acc, n = _accumulate_ones(
        # 用 SHA-256 取前 16 位十六进制。此处哈希**不承担任何安全属性**（只做均匀比特打散），
        # 但避免弱哈希可减少安全扫描噪音与评审争议，代价是每 token 多约 1µs。
        int(hashlib.sha256(tok.encode("utf-8", "ignore")).hexdigest()[:16], 16)
        for tok in toks)
    if n <= 0:
        return 0
    # 逐车道读数：某位 1 的个数 > N/2（即 2*ones > N）则该位为 1
    lanes = acc.to_bytes(_LANE_BITS // 8 * 64, "little")
    out = 0
    for i in range(64):
        ones = int.from_bytes(lanes[i * 2:i * 2 + 2], "little") & _LANE_MASK
        if ones * 2 > n:
            out |= (1 << i)
    return out


def clamp63(value: int) -> int:
    """钳到 SQLite INTEGER 安全范围。**任何写库前都要过它**（见文件头的坑）。"""
    try:
        return int(value) & _SQLITE_MAX_INT
    except Exception:
        return 0


def hamming(a: int, b: int) -> int:
    """汉明距离（不同位的个数）。"""
    return bin((int(a) ^ int(b)) & ((1 << 64) - 1)).count("1")


def is_near_dup(a: int, b: int, threshold: int = NEAR_DUP_THRESHOLD) -> bool:
    """两个 SimHash 是否近似重复。"""
    return hamming(a, b) <= int(threshold)

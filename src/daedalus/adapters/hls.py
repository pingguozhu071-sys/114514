# -*- coding: utf-8 -*-
"""HLS / m3u8 下载与 AES-128 解密（线程模型就绪，同步实现）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/m3u8_downloader.py`，经《新工程开工包》重写为同步版。
Daedalus 内的改动：
  1) 导入改为包内路径；
  2) **移除 `locale.setlocale()`**（进程级副作用，多线程宿主里很危险）——改为给子进程传 `env`，
     并对 `subprocess` 显式 `encoding="utf-8", errors="replace"`（原坑：中文输出解码崩溃）；
  3) 新增**防御上限**：playlist 体积上限（原实现无上限读进内存）、分片数上限（防"恶意清单列百万段"的磁盘炸弹）；
  4) 新增**产物可播验证**：ffprobe 存在时确认能读出时长/流（"命令返回 0 但产物是坏的"要有出口）；
  5) 分片**并发下载**仍未做（串行），解密与拼接必须按序 —— 留给 S5，登记在 PENDING。

────────────────────────────────────────────────────────────────
覆盖的流程
    取 playlist → 解析分片与 #EXT-X-KEY → 分片下载（**走 SSRF 闸**）→
    AES-128-CBC 解密（含 PKCS7 去填充）→ 写 UTF-8 的 concat 列表 → ffmpeg 合成单片 → 验证产物

四个真实踩过的坑（都在实现里规避了）
  1) **密钥/分片 URL 来自远端清单 = 不可信输入**：必须和普通抓取一样过逐跳闸
     （Kiana 的教训：清单里给的 URL 直接下，等于绕过入口校验把内网地址交给下载器）。
  2) **ffmpeg 的 concat 列表必须 UTF-8**：Windows 下子进程默认 GBK，含中文路径直接崩。
  3) **合并失败会让临时分片被清理，表现为"下载完啥也没有"** → 合并后必须**验证产物**。
  4) **PKCS7 去填充要校验合法性**（长度 1~16 且尾部字节一致），别把脏数据当合法填充。

────────────────────────────────────────────────────────────────
用法

    from daedalus.adapters.hls import download_hls

    ok, path, why = download_hls("https://host/index.m3u8", out_path="D:/out/video.ts")

**依赖**：AES 用 `cryptography`（优先）或 `pycryptodome`；ffmpeg 仅在**需要合成**时用到。
两者缺失时函数会返回明确原因，不会静默失败。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from daedalus.net.ssrf_gate import BlockedError, safe_open

__all__ = ["parse_playlist", "decrypt_segment", "download_hls", "PlaylistInfo"]

_ATTR_RE = re.compile(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)')

# 防御上限（原实现没有，属"恶意清单"面）
MAX_PLAYLIST_BYTES = 4 << 20      # 4MB：异常大的清单本身就是攻击向量
MAX_SEGMENTS = 5000               # 段数上限：防磁盘炸弹


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CBC 解密（优先 cryptography，回退 pycryptodome）。

    ⚠️ 关于"CBC 是弱算法"的扫描告警——**这里是协议强制，不能改**：
      HLS 规范规定 `#EXT-X-KEY:METHOD=AES-128` 就是 **AES-128-CBC + PKCS7**，
      换成 AES-GCM 会与所有真实流不兼容。CBC 的真实缺陷是**无完整性认证**
      （密文可被篡改而不被发现），本场景下的风险边界是：
        * 密钥与分片都经 `ssrf_gate.safe_open` 的受信通道获取（逐跳校验 + HTTPS）；
        * 拿到明文后的产物**仍会做大小/可播验证**（见 download_hls 第 ⑤ 步）；
        * 我们不把它用作"保密存储"——它只是解出别人本就公开播出的媒体流。

      **但如果你要加密自己的数据（配置/凭据/缓存），不要用 CBC，用 AES-GCM**
      （或直接走 Windows DPAPI，见 `daedalus.privacy.secrets`）。
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return dec.update(data) + dec.finalize()
    except ImportError:
        from Crypto.Cipher import AES      # type: ignore
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def _pkcs7_unpad(data: bytes) -> bytes:
    """PKCS7 去填充（**校验合法性**，不合法就原样返回并让上层判失败）。"""
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and len(data) >= pad and all(b == pad for b in data[-pad:]):
        return data[:-pad]
    return data


class PlaylistInfo:
    __slots__ = ("segments", "key_method", "key_uri", "key_iv", "media_sequence", "is_master")

    def __init__(self):
        self.segments: list[str] = []
        self.key_method = ""            # "" | "AES-128" | "NONE" | 其它
        self.key_uri = ""
        self.key_iv = b""
        self.media_sequence = 0
        self.is_master = False          # 主播放列表（含 #EXT-X-STREAM-INF，需要再取一层）


def parse_playlist(text: str, base_url: str = "") -> PlaylistInfo:
    """解析 m3u8 文本。相对 URL 用 `base_url` 解析为绝对地址。"""
    info = PlaylistInfo()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                info.media_sequence = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line.startswith("#EXT-X-STREAM-INF"):
            info.is_master = True
        elif line.startswith("#EXT-X-KEY"):
            # 注意：先截掉 "#EXT-X-KEY:" 再按逗号分割（Kiana 在这里踩过前缀未截断的坑）
            attrs = dict(_ATTR_RE.findall(line.split(":", 1)[1] if ":" in line else ""))
            method = attrs.get("METHOD", "").strip('"').upper()
            info.key_method = method
            uri = attrs.get("URI", "").strip('"')
            info.key_uri = urllib.parse.urljoin(base_url, uri) if uri else ""
            iv_hex = attrs.get("IV", "").strip('"')
            if iv_hex.upper().startswith("0X"):
                try:
                    info.key_iv = bytes.fromhex(iv_hex[2:].zfill(32))
                except Exception:
                    info.key_iv = b""
        elif not line.startswith("#"):
            info.segments.append(urllib.parse.urljoin(base_url, line))
    return info


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    """解密单个分片（AES-128-CBC + PKCS7 去填充）。"""
    return _pkcs7_unpad(_aes_cbc_decrypt(key, iv, data))


def _iv_for(seq_index: int, explicit: bytes) -> bytes:
    """IV 规则：清单给了显式 IV 就用它；否则按分片序号（大端 16 字节）推导。"""
    if explicit:
        return explicit
    return int(seq_index).to_bytes(16, "big")


def _ffprobe_ok(ffprobe: str | None, path: Path) -> tuple[bool, str]:
    """产物可播验证（ffprobe 缺失时按"跳过"处理，不误杀）。"""
    if not ffprobe:
        return True, "（无 ffprobe，跳过可播验证）"
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
        if r.returncode != 0:
            return False, f"ffprobe rc={r.returncode}: {(r.stderr or '')[:160]}"
        dur = (r.stdout or "").strip()
        if dur and float(dur) <= 0:
            return False, f"ffprobe 时长异常: {dur!r}"
        return True, f"ffprobe ok（duration={dur or 'n/a'}）"
    except Exception as e:
        return False, f"ffprobe 异常: {type(e).__name__}: {e}"


def download_hls(playlist_url: str, out_path, *, opener=safe_open, timeout: float = 30,
                 workdir=None, ffmpeg: str | None = None, ffprobe: str | None = None,
                 keep_segments: bool = False, max_segments: int = MAX_SEGMENTS,
                 concurrency: int = 4, runner=None) -> tuple[bool, str | None, str]:
    """下载一个 HLS 流并合成单片。返回 `(ok, path_or_None, reason)`。

    `concurrency`：分片**并发**下载（每片独立解密，各写各的文件）；**拼接顺序由分片序号决定**
    （并发不改变顺序——`concat_list` 始终按 index 生成）。解密与写出都在各自的分片文件里，
    所以并发是安全的；真正必须按序的只有最后的拼装。
    `runner`：外部工具的**执行面**（`exec.subprocess.run_tool` 风格）；不传则用内置 subprocess。
    """
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    ffprobe = ffprobe if ffprobe is not None else shutil.which("ffprobe")
    tmp = Path(workdir or tempfile.mkdtemp(prefix="hls_"))
    tmp.mkdir(parents=True, exist_ok=True)

    # ① 取 playlist（走闸；**有体积上限**）
    try:
        with opener(playlist_url, timeout=timeout) as resp:
            if resp.status != 200:
                return False, None, f"playlist HTTP {resp.status}"
            raw = resp.read(MAX_PLAYLIST_BYTES + 1)
            if len(raw) > MAX_PLAYLIST_BYTES:
                return False, None, f"playlist 过大（>{MAX_PLAYLIST_BYTES} 字节），拒绝处理"
            text = raw.decode("utf-8", "replace")
    except BlockedError as e:
        return False, None, f"playlist 被闸拦下: {e}"
    except Exception as e:
        return False, None, f"playlist 获取失败: {type(e).__name__}: {e}"

    info = parse_playlist(text, base_url=playlist_url)
    if info.is_master:
        return False, None, "这是主播放列表（#EXT-X-STREAM-INF）：请先解析出子 playlist 再下载"
    if not info.segments:
        return False, None, "playlist 里没有分片"
    if len(info.segments) > int(max_segments):
        return False, None, f"分片数超上限（{len(info.segments)} > {max_segments}），疑似恶意清单"

    # ② 取密钥（同样走闸 —— 它是远端给的 URL）
    key = b""
    if info.key_method == "AES-128":
        if not info.key_uri:
            return False, None, "声明了 AES-128 但没有 URI"
        try:
            with opener(info.key_uri, timeout=timeout) as kr:
                key = kr.read()
        except BlockedError as e:
            return False, None, f"密钥 URL 被闸拦下: {e}"
        except Exception as e:
            return False, None, f"密钥获取失败: {type(e).__name__}: {e}"
        if len(key) != 16:
            return False, None, f"密钥长度异常: {len(key)}"

    # ③ 逐片下载（+ 解密）—— 可并发；**每个分片写自己的文件**，拼接顺序由序号决定
    seg_files: list[Path] = [tmp / f"seg_{i:05d}.ts" for i in range(len(info.segments))]

    def fetch_one(i: int) -> tuple[int, str]:
        seg_url = info.segments[i]
        try:
            with opener(seg_url, timeout=timeout) as sr:
                if sr.status != 200:
                    return i, f"分片 {i} HTTP {sr.status}"
                data = sr.read()
            if key:
                data = decrypt_segment(data, key, _iv_for(info.media_sequence + i, info.key_iv))
            seg_files[i].write_bytes(data)
            return i, ""
        except BlockedError as e:
            return i, f"分片 {i} 被闸拦下: {e}"
        except Exception as e:
            return i, f"分片 {i} 失败: {type(e).__name__}: {e}"

    n_workers = max(1, min(int(concurrency), len(info.segments), 16))
    if n_workers == 1:
        results = [fetch_one(i) for i in range(len(info.segments))]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=n_workers, thread_name_prefix="hls-seg") as ex:
            results = list(ex.map(fetch_one, range(len(info.segments))))
    # **按序号收集错误**（并发不改变顺序；只报第一个失败，但把序号写清楚）
    for idx, err in sorted(results, key=lambda r: r[0]):
        if err:
            return False, None, err
    seg_files = [p for p in seg_files if p.exists()]

    # ④ 合成（有 ffmpeg 就合成；没有则如实说明并保留分片）
    if not ffmpeg:
        return False, None, f"未找到 ffmpeg：分片已下载在 {tmp}（共 {len(seg_files)} 片），无法合成"

    list_file = tmp / "concat_list.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in seg_files),
                         encoding="utf-8")          # ← 必须 UTF-8（见文件头坑 2）
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    child_env.setdefault("LC_ALL", "C.UTF-8")          # 移植改动 2：不再改进程自身的 locale
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)]
    try:
        if runner is not None:
            # 走统一子进程执行面（超时/编码/工作目录都由它管）
            res = runner(cmd, timeout=1800, workdir=str(tmp))
            r_rc, r_err = int(getattr(res, "returncode", -1)), str(getattr(res, "stderr", ""))
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=1800, env=child_env)
            r_rc, r_err = int(r.returncode), (r.stderr or "")
    except Exception as e:
        return False, None, f"ffmpeg 调用异常: {type(e).__name__}: {e}"

    # ⑤ **验证产物**（别信退出码 —— 见文件头坑 3）
    if r_rc != 0 or not out_path.exists() or out_path.stat().st_size < 1024:
        return False, None, f"ffmpeg 失败(rc={r_rc})：{r_err[:200]}"
    playable, why = _ffprobe_ok(ffprobe, out_path)
    if not playable:
        return False, None, f"产物不可播：{why}"

    if not keep_segments:
        try:
            for p in seg_files:
                p.unlink(missing_ok=True)
            list_file.unlink(missing_ok=True)
        except Exception:
            pass
    return True, str(out_path), f"ok（{out_path.stat().st_size} 字节，{len(seg_files)} 片；{why}）"

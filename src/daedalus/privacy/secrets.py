# -*- coding: utf-8 -*-
"""本机密钥的安全落盘（Windows DPAPI）+ 结构化密钥存取（同步，零第三方依赖）

来源：Kiana Vnext Plus v2.19.8 的 `kiana_vnext_plus/privacy_store.py`，经《新工程开工包》重写。
Daedalus 内的改动：
  1) `_data_root()` 改为本工程的数据根规则（环境变量覆盖 / 便携=exe 同级 / 默认用户级目录），
     **绝不使用打包器解包临时目录**（Kiana 事故：升级时删 `_internal` 把用户数据一起删光）；
  2) 新增 `secret_meta()`：给密钥文件记一份**不含密钥本身**的元数据（创建/更新/用途），
     便于轮换与审计；
  3) `ctypes.wintypes` 的导入做了兜底，使非 Windows 环境下**可以导入**（只是能力为 False，
     而不是 ImportError）——便于离线测试与跨平台跑门禁。

────────────────────────────────────────────────────────────────
为什么用 DPAPI 而不是自己写加密
    Windows 的 `CryptProtectData/CryptUnprotectData` 用**当前用户账户**的密钥加密：
    密文拷到别的机器/别的用户下无法解密——"不可移植"本身就是隐私特性。
    而且它是系统级实现，不需要你管理密钥文件本身。

────────────────────────────────────────────────────────────────
三条来自 Kiana 的教训（都很贵）
  1) **绝不设"弱兜底"**：Kiana 曾有一个**公开字面量**兜底密码（源码里写着，等于谁读过
     源码就能解开加密库）。正确做法：**读不到就明确失败**，不降级。
  2) **不要谎报加密**：界面曾提示"密钥已 DPAPI 加密"，实际实现却写的是明文 JSON。
     **提示语必须由加密结果决定**（不可用时如实显示"本机未加密"）。
  3) **明文迁移要顺手擦掉**：老版本明文存的密钥，读到后加密落盘并**删掉原明文**，
     否则"加密"只是多留了一份副本。

────────────────────────────────────────────────────────────────
用法

    from daedalus.privacy.secrets import save_secret_json, load_secret_json, dpapi_available

    ok = save_secret_json("cookies", {"sessionid": "..."})   # 返回 False = 本机不支持
    if ok: 提示用户"已加密保存"
    else:  提示用户"本机无法加密，明文保存在配置文件中"   # ← 如实告知，别谎报

    keys = load_secret_json("cookies") or {}                  # 不存在/解密失败 → None
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import pathlib
import sys
import time

try:                                    # 非 Windows 也能导入（能力为 False，而非 ImportError）
    import ctypes.wintypes
    _WINTYPES_OK = True
except Exception:                       # pragma: no cover - 仅非 Windows
    _WINTYPES_OK = False

logger = logging.getLogger(__name__)

__all__ = ["dpapi_available", "protect", "unprotect", "save_protected", "load_protected",
           "secret_path", "secret_meta", "save_secret_json", "load_secret_json",
           "delete_secret", "data_root", "portable_dir"]

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
APP_DIR_NAME = "Daedalus"
_ENV_DATA_ROOT = "DAEDALUS_DATA_ROOT"       # 测试/便携用：显式指定数据根
_ENV_PORTABLE = "DAEDALUS_PORTABLE"         # "1" = 数据根放 exe 同级


if _WINTYPES_OK:
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]
else:                                   # pragma: no cover
    _DATA_BLOB = None


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))


def _from_blob(blob) -> bytes:
    out = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))
    return out


def dpapi_available() -> bool:
    return os.name == "nt" and _WINTYPES_OK


def protect(data: bytes | str) -> bytes:
    """加密（当前用户可解；其他用户/机器无法解）。非 Windows 抛 RuntimeError。

    接受 `bytes` 或 `str`（str 会按 UTF-8 编码）——让调用方少写一次 encode。
    """
    if not dpapi_available():
        raise RuntimeError("DPAPI 仅 Windows 可用")
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(raw)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    return _from_blob(out)


def unprotect(data: bytes) -> bytes:
    """解密（非本机/本用户会抛 OSError）。"""
    if not dpapi_available():
        raise RuntimeError("DPAPI 仅 Windows 可用")
    out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    return _from_blob(out)


# ══════════════════════════════════════════════════════════════════
# 数据根与文件级封装
# ══════════════════════════════════════════════════════════════════
def portable_dir() -> pathlib.Path | None:
    """便携版的判定（**不靠环境变量也能用**）：exe 同级有 `portable.flag` 或 `DaedalusData/`。

    为什么要有"标记文件"这一层：打包好的便携版是给"拷到 U 盘/别的机器直接双击"用的，
    要求用户先设环境变量是不现实的（双击快捷方式时环境变量根本不生效）。
    所以便携性写在**文件系统**里：
      * `portable.flag`（安装器便携模式会写它，内容随便，存在即便携）；
      * 或者同级已经有 `DaedalusData/`（用户手动建过，说明他就是要便携）。

    ⚠️ **绝不**用打包器解包临时目录（`sys._MEIPASS`）当数据根——Kiana 便携数据就是这样
    在安装器升级时被整个删光的。
    """
    try:
        if getattr(sys, "frozen", False):
            base = pathlib.Path(sys.executable).parent
        else:
            base = pathlib.Path.cwd()
    except Exception:
        return None
    try:
        if (base / "portable.flag").exists() or (base / "DaedalusData").is_dir():
            return base / "DaedalusData"
    except Exception:
        return None
    return None


def data_root() -> pathlib.Path:
    """Daedalus 数据根（密钥、缓存、原始层与元数据库都在这下面）。

    优先级：
      1) 环境变量 `DAEDALUS_DATA_ROOT`（测试注入 / 用户自定）
      2) 便携：`DAEDALUS_PORTABLE=1` **或** exe 同级有 `portable.flag`/`DaedalusData/`
         → **exe 同级** `DaedalusData/`（见 `portable_dir()`）
      3) 默认：用户级目录（`%LOCALAPPDATA%/Daedalus`，回退 `%APPDATA%`，再回退家目录）

    ⚠️ **绝不要**用打包器解包临时目录（`sys._MEIPASS`）——Kiana 的便携数据就是这样
    在安装器升级时被整个删掉的。
    """
    env_root = os.environ.get(_ENV_DATA_ROOT)
    if env_root:
        return pathlib.Path(env_root)
    if os.environ.get(_ENV_PORTABLE) == "1":
        try:
            base = pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) \
                else pathlib.Path.cwd()
            return base / "DaedalusData"
        except Exception:
            pass
    port = portable_dir()
    if port is not None:
        return port
    try:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(pathlib.Path.home())
    except Exception:
        base = str(pathlib.Path.home())
    return pathlib.Path(base) / APP_DIR_NAME


def secret_path(name: str) -> pathlib.Path:
    """密钥文件路径：<data_root>/secrets/<name>.bin（name 会被清洗，防路径穿越）。"""
    safe = "".join(c for c in str(name) if c.isalnum() or c in "_-") or "secret"
    return data_root() / "secrets" / f"{safe}.bin"


def secret_meta(name: str) -> dict:
    """读取密钥文件的**元数据**（不含密钥内容）：created/updated/type/size。

    为什么单独存：轮换与审计需要"这个密钥什么时候建的/多大/用来干什么"，
    但把这些塞进密文里会让调用方拿到的 dict 形状变形（老调用方会炸）。
    """
    try:
        p = secret_path(name)
        meta_p = p.with_suffix(".meta.json")
        meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        if p.exists():
            meta.setdefault("created", meta.get("created") or None)
            meta["size"] = p.stat().st_size
            meta["updated"] = meta.get("updated") or p.stat().st_mtime
        meta.setdefault("name", name)
        return meta
    except Exception as e:
        logger.warning("读取密钥元数据 %s 失败: %s", name, e)
        return {}


def save_protected(path: pathlib.Path, text: str) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(protect(str(text).encode("utf-8")))


def load_protected(path: pathlib.Path) -> str:
    return unprotect(pathlib.Path(path).read_bytes()).decode("utf-8")


def save_secret_json(name: str, obj, kind: str = "") -> bool:
    """dict → JSON → DPAPI 密文落盘。**成功返回 True；DPAPI 不可用返回 False**。

    调用方必须据此如实提示用户（见文件头教训 2），**不要**静默改写成明文。
    """
    try:
        p = secret_path(name)
        now = time.time()
        existed = p.exists()
        save_protected(p, json.dumps(obj, ensure_ascii=False))
        try:                                    # 元数据明文落盘（**不含密钥内容**）
            meta = {}
            meta_p = p.with_suffix(".meta.json")
            if meta_p.exists():
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            meta.update({"name": name, "type": kind or meta.get("type", ""),
                         "updated": now})
            meta.setdefault("created", now if not existed else meta.get("created", now))
            meta_p.parent.mkdir(parents=True, exist_ok=True)
            meta_p.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning("DPAPI 加密保存 %s 失败: %s", name, e)
        return False


def load_secret_json(name: str):
    """DPAPI 密文 → dict。不存在/解密失败/非 JSON → None（不抛异常）。"""
    try:
        p = secret_path(name)
        if not p.exists():
            return None
        val = json.loads(load_protected(p))
        return val if isinstance(val, dict) else None
    except Exception as e:
        logger.warning("DPAPI 解密读取 %s 失败: %s", name, e)
        return None


def delete_secret(name: str) -> bool:
    """删除密钥文件与它的元数据。"""
    ok = True
    try:
        p = secret_path(name)
        p.unlink(missing_ok=True)
        p.with_suffix(".meta.json").unlink(missing_ok=True)
    except Exception as e:
        logger.warning("删除密钥文件 %s 失败: %s", name, e)
        ok = False
    return ok

# -*- coding: utf-8 -*-
"""设置存储与预设：**即改即存**（没有"应用"按钮），预设可导入导出 JSON

两条纪律：
  * **即改即存**：每个 setter 立刻落盘（原子写：临时文件 + `os.replace`）。
    用户改了外观却因为"忘了点应用"而丢设置，是最招人烦的一类 bug。
  * **读坏不崩**：设置文件损坏/被手改坏 → 回退默认值并**记下原因**（`load_note`），
    在设置页如实显示（不静默吞掉，否则用户永远不知道自己那份设置了为什么没生效）。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time

from daedalus.ui.theme import ACCENT_PRESETS, ThemeError, tokens

logger = logging.getLogger(__name__)

__all__ = ["SettingsStore", "FACTORY_PRESETS", "settings_path"]

_FIELDS = ("light", "accent", "accent_locked", "panel_alpha", "radius", "font_pt", "density",
           "animations", "fade_ms", "debounce_ms", "fps_cap", "signature", "expert_mode",
           "wallpaper", "wallpaper_dir", "wallpaper_mode", "blur", "dim_manual", "focus",
           "downsample_max", "theme_name", "log_autoscroll", "page")

# 出厂预设（"命名保存/加载/导入导出"里的那批）
FACTORY_PRESETS: dict = {
    "通透": {"panel_alpha": 40, "blur": 6, "dim_manual": 0, "accent": "#4FA3E8"},
    "标准": {"panel_alpha": 65, "blur": 0, "dim_manual": 0, "accent": "#4FA3E8"},
    "柔和": {"panel_alpha": 78, "blur": 12, "dim_manual": 10, "accent": "#9B7EDE"},
    "实心": {"panel_alpha": 95, "blur": 0, "dim_manual": 0, "accent": "#2FC6C6"},
}


def settings_path(data_root) -> pathlib.Path:
    return pathlib.Path(str(data_root)) / "ui_settings.json"


class SettingsStore:
    """外观与界面设置（字段名与 `theme.Tokens` 基本一一对应）。"""

    def __init__(self, data_root, *, autosave: bool = True):
        self.path = settings_path(data_root)
        self.autosave = bool(autosave)
        self.load_note = ""
        self.data: dict = self._defaults()
        self.presets: dict = {k: dict(v) for k, v in FACTORY_PRESETS.items()}
        self.load()

    # ── 默认值 / 读写 ───────────────────────────────────────────
    @staticmethod
    def _defaults() -> dict:
        return {"light": False, "accent": ACCENT_PRESETS[0][1], "accent_locked": False,
                "panel_alpha": 65, "radius": 12, "font_pt": 10.0, "density": "standard",
                "animations": True, "fade_ms": 450, "debounce_ms": 250, "fps_cap": 30,
                "signature": True, "expert_mode": False,
                "wallpaper": "", "wallpaper_dir": "", "wallpaper_mode": "single",
                "blur": 0, "dim_manual": 0.0, "focus": "center", "downsample_max": 2560,
                "theme_name": "", "log_autoscroll": True, "page": "overview"}

    def load(self) -> dict:
        if not self.path.exists():
            return self.data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            self.load_note = f"设置文件读不出来（{type(e).__name__}: {e}）→ 已回退默认值"
            logger.warning(self.load_note)
            return self.data
        if not isinstance(raw, dict):
            self.load_note = "设置文件不是一个对象 → 已回退默认值"
            return self.data
        ui = raw.get("ui") if isinstance(raw.get("ui"), dict) else raw
        for k in _FIELDS:
            if k in ui:
                self.data[k] = ui[k]
        pres = raw.get("presets")
        if isinstance(pres, dict):
            for name, body in pres.items():
                if isinstance(body, dict):
                    self.presets[str(name)] = {k: v for k, v in body.items() if k in _FIELDS}
        return self.data

    def save(self) -> None:
        if not self.autosave:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = {"ui": {k: self.data.get(k) for k in _FIELDS},
                "presets": self.presets, "saved_at": time.time()}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)                 # 原子写：断电也不会留半截文件
        except Exception as e:
            logger.warning("设置保存失败：%s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value, *, quiet: bool = False) -> None:
        """即改即存。校验不过就抛 `ThemeError`（**不静默纠正**）。"""
        self._validate(key, value)
        self.data[key] = value
        if not quiet:
            self.save()

    def update(self, patch: dict, *, quiet: bool = False) -> dict:
        for k, v in (patch or {}).items():
            if k in _FIELDS:
                self._validate(k, v)
        self.data.update({k: v for k, v in (patch or {}).items() if k in _FIELDS})
        if not quiet:
            self.save()
        return dict(self.data)

    @staticmethod
    def _validate(key: str, value) -> None:
        if key == "panel_alpha" and not (40 <= int(value) <= 95):
            raise ThemeError(f"透明度越界：{value}（合法 40–95）")
        if key == "font_pt" and float(value) < 10.0:
            raise ThemeError(f"字号 {value}pt < 10pt（中文会发糊）")
        if key == "blur" and not (0 <= int(value) <= 30):
            raise ThemeError(f"模糊越界：{value}（合法 0–30）")
        if key == "dim_manual" and not (0 <= float(value) <= 60):
            raise ThemeError(f"蒙层越界：{value}（合法 0–60）")
        if key == "density" and value not in ("compact", "standard", "relaxed"):
            raise ThemeError(f"未知密度档：{value}")
        if key == "focus" and value not in ("center", "top", "bottom", "left", "right",
                                           "top_left", "top_right", "bottom_left",
                                           "bottom_right"):
            raise ThemeError(f"未知焦点：{value}")

    # ── 预设 ────────────────────────────────────────────────────
    def save_preset(self, name: str, *, patch: dict | None = None) -> dict:
        n = str(name or "").strip()
        if not n:
            raise ThemeError("预设名不能为空")
        body = {k: (patch or {}).get(k, self.data.get(k))
                for k in _FIELDS if k in (patch or {}) or k in
                ("accent", "panel_alpha", "blur", "dim_manual", "light", "density", "font_pt",
                 "radius", "focus", "animations")}
        self.presets[n] = body
        self.save()
        return dict(body)

    def load_preset(self, name: str) -> dict:
        body = self.presets.get(str(name))
        if body is None:
            raise ThemeError(f"没有这个预设：{name}")
        return self.update(dict(body))

    def delete_preset(self, name: str) -> bool:
        okk = self.presets.pop(str(name), None) is not None
        if okk:
            self.save()
        return okk

    def list_presets(self) -> list[dict]:
        return [{"name": n, "builtin": n in FACTORY_PRESETS, "fields": len(b)} for n, b in
                sorted(self.presets.items())]

    def export_presets(self, path) -> dict:
        p = pathlib.Path(str(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"presets": self.presets}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return {"written": str(p), "count": len(self.presets)}

    def import_presets(self, path, *, overwrite: bool = True) -> dict:
        p = pathlib.Path(str(path))
        if not p.exists():
            raise ThemeError(f"预设文件不存在：{p}")
        raw = json.loads(p.read_text(encoding="utf-8"))
        body = raw.get("presets") if isinstance(raw, dict) else None
        if not isinstance(body, dict):
            raise ThemeError("预设文件格式不对（要 {\"presets\": {...}}）")
        added = skipped = 0
        for name, fields in body.items():
            if not isinstance(fields, dict):
                skipped += 1
                continue
            if name in self.presets and name not in FACTORY_PRESETS and not overwrite:
                skipped += 1
                continue
            self.presets[str(name)] = {k: v for k, v in fields.items() if k in _FIELDS}
            added += 1
        self.save()
        return {"imported": added, "skipped": skipped, "total": len(self.presets)}

    # ── 给主题用 ────────────────────────────────────────────────
    def to_tokens(self):
        """把设置变成一组**已校验**的主题令牌（参数错在这里就抛）。"""
        return tokens(light=bool(self.data["light"]), accent=str(self.data["accent"]),
                      panel_alpha=int(self.data["panel_alpha"]), radius=int(self.data["radius"]),
                      font_pt=float(self.data["font_pt"]), density=str(self.data["density"]),
                      animations=bool(self.data["animations"]), fade_ms=int(self.data["fade_ms"]),
                      debounce_ms=int(self.data["debounce_ms"]), fps_cap=int(self.data["fps_cap"]),
                      signature=bool(self.data["signature"]),
                      expert_mode=bool(self.data["expert_mode"]),
                      wallpaper=str(self.data["wallpaper"]), blur=int(self.data["blur"]),
                      dim_manual=float(self.data["dim_manual"]), focus=str(self.data["focus"]),
                      downsample_max=int(self.data["downsample_max"]))

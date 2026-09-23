# -*- coding: utf-8 -*-
"""指标面：**有界、可导出、可下钻**的观测数据

为什么要自己写而不是引 Prometheus 客户端：
  1) 这是**单机单用户**工具，没必要为了几个百分位引一个服务端生态；
  2) 更重要的：**指标本身不能成为 OOM 源**。常见事故是"给每个 URL 打标签" →
     序列数无限膨胀 → 内存一点点涨到炸（而且极难归因）。
     所以这里**硬性限制序列数**（超出就丢弃并记 `metrics.series_overflow`），
     直方图用**固定容量环形缓冲**（样本数不随时间增长），百分位只在取快照时算。

三个原语：`inc`（计数）/ `set`（瞬时值）/ `observe`（分布）。外加 `timer` 上下文管理器。

百分位口径（写清楚，免得以后争议）：
  * 取快照时对**已有样本**排序后做**线性插值**（与 numpy 的 linear 一致）；
  * 样本不足时如实返回已有值（不编造）——`histogram()["count"]` 永远是真实样本数；
  * 环形缓冲满了以后是**最近 N 个**样本的分位数（滑动窗口），并记 `_overflow` 说明丢了多少。

导出：`snapshot()` / `summary()`（给人看的紧凑视图，含 UI 需要的十几项）/ `to_jsonl()`。
"""

from __future__ import annotations

import json
import threading
import time
from array import array
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["Metrics", "METRICS", "Histogram", "Series", "RateWindow", "quantile"]

# 序列基数上限：超过就不再接纳新序列（并计数）。**这是防 OOM 的关键闸**。
MAX_SERIES = 1024
# 直方图样本容量：固定，不随流量增长（2064 个 double ≈ 16KB/序列）。
HIST_CAPACITY = 2048


def quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（`sorted_values` 必须已升序）。空输入返回 0.0。"""
    n = len(sorted_values)
    if n <= 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    q = min(max(float(q), 0.0), 1.0)
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


@dataclass
class Histogram:
    """固定容量样本窗 + 计数/和/极值。百分位在快照时算。"""

    capacity: int = HIST_CAPACITY
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    dropped: int = 0
    _samples: array = field(default_factory=lambda: array("d"))
    _sorted_cache: list[float] | None = None

    def observe(self, value: float) -> None:
        try:
            v = float(value)
        except Exception:
            return
        self.count += 1
        self.total += v
        if self.minimum is None or v < self.minimum:
            self.minimum = v
        if self.maximum is None or v > self.maximum:
            self.maximum = v
        if len(self._samples) < self.capacity:
            self._samples.append(v)
        else:
            # 满了以后丢掉最老的样本（滑动窗），并如实记账丢了多少
            self._samples[self.dropped % self.capacity] = v
            self.dropped += 1
        self._sorted_cache = None

    def percentiles(self) -> dict:
        if self._sorted_cache is None:
            self._sorted_cache = sorted(self._samples)
        s = self._sorted_cache
        return {"count": self.count, "sum": self.total,
                "min": self.minimum if self.minimum is not None else 0.0,
                "max": self.maximum if self.maximum is not None else 0.0,
                "p50": quantile(s, 0.50), "p90": quantile(s, 0.90),
                "p95": quantile(s, 0.95), "p99": quantile(s, 0.99),
                "samples": len(s), "dropped": self.dropped}

    def to_dict(self) -> dict:
        return self.percentiles()

    def reset(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = self.maximum = None
        self.dropped = 0
        self._samples = array("d")
        self._sorted_cache = None


class Series:
    """一条时间序列（名字 + 标签）。`kind` 是 counter/gauge/histogram。"""

    __slots__ = ("kind", "key", "value", "hist", "updated_at")

    def __init__(self, kind: str, key: str):
        self.kind = kind
        self.key = key
        self.value = 0.0
        self.hist = Histogram()
        self.updated_at = time.time()


class RateWindow:
    """滑动窗口速率：`pages/s` 与 `MB/s` 这类"实时"指标靠它算，不靠外部定时器。

    记录 (时刻, 累计字节, 累计页数) 的稀疏点，取快照时用"窗口头尾差值 / 时间差"。
    点数有上限（`max_points`），超了丢最老的——**还是那句话：观测不能自己长成 OOM**。
    """

    def __init__(self, max_points: int = 256):
        self.max_points = int(max_points)
        self._lock = threading.Lock()
        self._points: list[tuple[float, int, int]] = []

    def touch(self, bytes_total: int, pages_total: int, now: float | None = None) -> None:
        t = time.time() if now is None else float(now)
        with self._lock:
            self._points.append((t, int(bytes_total), int(pages_total)))
            if len(self._points) > self.max_points:
                self._points = self._points[-self.max_points:]

    def rates(self, window: float = 30.0, now: float | None = None) -> dict:
        t1 = time.time() if now is None else float(now)
        with self._lock:
            pts = list(self._points)
        if len(pts) < 2:
            return {"window": 0.0, "pages_per_sec": 0.0, "mb_per_sec": 0.0}
        # 取窗口内最早的点（没有就取最早的点，如实标出真实窗口长度）
        ref = pts[0]
        for p in pts:
            if t1 - p[0] <= window:
                ref = p
                break
        dt = max(1e-6, t1 - ref[0])
        d_bytes = max(0, pts[-1][1] - ref[1])
        d_pages = max(0, pts[-1][2] - ref[2])
        return {"window": round(dt, 3),
                "pages_per_sec": round(d_pages / dt, 4),
                "mb_per_sec": round(d_bytes / dt / (1 << 20), 4)}


class Metrics:
    """线程安全的指标注册表（进程内单例由 `METRICS` 提供）。"""

    def __init__(self, max_series: int = MAX_SERIES, hist_capacity: int = HIST_CAPACITY):
        self.max_series = int(max_series)
        self.hist_capacity = int(hist_capacity)
        self._lock = threading.Lock()
        self._series: dict[str, Series] = {}
        self.series_overflow = 0
        self.started_at = time.time()
        self.rates = RateWindow()
        self._bytes_total = 0
        self._pages_total = 0

    # ── 内部：取序列（带基数闸）──────────────────────────────────
    def _series_for(self, key: str, kind: str, labels: dict | None) -> Series | None:
        full = _key_of(key, labels)
        s = self._series.get(full)
        if s is not None:
            if s.kind != kind:
                # 同名不同类型是**调用方的 bug**：不静默混用，直接不合格
                raise ValueError(f"指标 {key!r} 已以 {s.kind} 注册，不能再当 {kind} 用")
            return s
        if len(self._series) >= self.max_series:
            self.series_overflow += 1            # 丢弃新序列并计数（防标签爆炸）
            return None
        s = Series(kind, full)
        s.hist = Histogram(capacity=self.hist_capacity)
        self._series[full] = s
        return s

    # ── 三个原语（第一个参数叫 `key` 而不是 `name`：`name=` 是常用标签，别撞）──
    def inc(self, key: str, value: float = 1.0, **labels) -> float:
        with self._lock:
            s = self._series_for(key, "counter", labels)
            if s is None:
                return 0.0
            s.value += float(value)
            s.updated_at = time.time()
            return s.value

    def set(self, key: str, value, **labels) -> None:            # noqa: A003 - 语义就是 set
        with self._lock:
            s = self._series_for(key, "gauge", labels)
            if s is None:
                return
            try:
                s.value = float(value)
            except Exception:
                return
            s.updated_at = time.time()

    def observe(self, key: str, value: float, **labels) -> None:
        with self._lock:
            s = self._series_for(key, "histogram", labels)
            if s is None:
                return
            s.hist.observe(float(value))
            s.updated_at = time.time()

    # ── 便捷 ───────────────────────────────────────────────────
    @contextmanager
    def timer(self, key: str, **labels):
        """`with METRICS.timer("net.latency"): ...` —— 异常也会记时间（失败也要算延迟）。"""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.observe(key, time.monotonic() - t0, **labels)

    def count_throughput(self, bytes_added: int = 0, pages_added: int = 0) -> None:
        """喂吞吐窗口（字节与页数）。窗口自己有界，不对每条记录留痕。"""
        with self._lock:
            self._bytes_total += max(0, int(bytes_added))
            self._pages_total += max(0, int(pages_added))
            b, p = self._bytes_total, self._pages_total
        self.rates.touch(b, p)

    # ── 读 ─────────────────────────────────────────────────────
    def _registry_value(self, key: str):
        """注册表自身的指标（不是普通序列）：`metrics.*` 前缀。

        为什么单独处理：序列基数闸满时**不能再往序列表里塞东西**，但"我丢了多少序列"
        这条恰恰是闸本身产生的——所以它必须活在注册表上，并由这里统一暴露。
        """
        if key == "metrics.series_overflow":
            return float(self.series_overflow)
        if key == "metrics.series_total":
            return float(len(self._series))
        if key == "metrics.uptime_seconds":
            return round(time.time() - self.started_at, 3)
        return None

    def counter(self, key: str, **labels) -> float:
        with self._lock:
            reg = self._registry_value(key) if not labels else None
            if reg is not None:
                return reg
            s = self._series.get(_key_of(key, labels))
            return float(s.value) if s is not None else 0.0

    def gauge(self, key: str, **labels):
        with self._lock:
            reg = self._registry_value(key) if not labels else None
            if reg is not None:
                return reg
            s = self._series.get(_key_of(key, labels))
            return None if s is None else float(s.value)

    def histogram(self, key: str, **labels) -> dict:
        with self._lock:
            s = self._series.get(_key_of(key, labels))
            return s.hist.percentiles() if s is not None else {}

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._series)

    def snapshot(self) -> dict:
        """全部序列的快照（名字 + 标签 → 值 / 分布）。"""
        with self._lock:
            out: dict[str, dict] = {}
            for key, s in self._series.items():
                if s.kind == "histogram":
                    out[key] = dict(kind=s.kind, **s.hist.percentiles())
                else:
                    out[key] = {"kind": s.kind, "value": s.value,
                                "updated_at": round(s.updated_at, 3)}
            overflow = self.series_overflow
        out["metrics.series_total"] = {"kind": "gauge", "value": len(out)}
        out["metrics.series_overflow"] = {"kind": "counter", "value": overflow}
        out["metrics.uptime_seconds"] = {"kind": "gauge",
                                        "value": round(time.time() - self.started_at, 3)}
        out.update({f"rate.{k}": {"kind": "gauge", "value": v}
                    for k, v in self.rates.rates().items()})
        return out

    def summary(self) -> dict:
        """紧凑视图（GUI/CLI 直接显示；键是**稳定的**，够 L1 的 ≥15 项）。"""
        s = self.snapshot()

        def v(key: str, default=0.0):
            return s.get(key, {}).get("value", default)

        lat = self.histogram("net.latency")
        task = self.histogram("task.duration")
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "pages_per_sec": v("rate.pages_per_sec"),
            "mb_per_sec": v("rate.mb_per_sec"),
            "net_requests": v("net.requests"),
            "net_bytes": v("net.bytes"),
            "net_latency_p50": round(lat.get("p50", 0.0), 4),
            "net_latency_p95": round(lat.get("p95", 0.0), 4),
            "net_latency_p99": round(lat.get("p99", 0.0), 4),
            "net_throttled": v("net.throttled"),
            "net_blocked": v("net.blocked"),
            "tasks_total": v("task.total"),
            "tasks_done": v("task.done"),
            "tasks_failed": v("task.failed"),
            "tasks_policy_denied": v("task.policy_denied"),
            "task_duration_p50": round(task.get("p50", 0.0), 4),
            "task_duration_p95": round(task.get("p95", 0.0), 4),
            "task_duration_p99": round(task.get("p99", 0.0), 4),
            "parse_rejected": v("parse.rejected"),
            "raw_bytes": v("rawstore.bytes"),
            "db_rows": v("db.rows"),
            "db_flush_p95": round(self.histogram("db.flush").get("p95", 0.0), 4),
            "queue_blocked_puts": v("queue.blocked_puts"),
            "active_threads": v("exec.threads"),
            "rss_mb": v("proc.rss_bytes") / (1 << 20),
            "handles": v("proc.handles"),
            "disk_free_mb": v("disk.free_bytes") / (1 << 20),
            "series_total": v("metrics.series_total"),
            "series_overflow": v("metrics.series_overflow"),
        }

    def gauges(self, prefix: str) -> dict:
        """取某个前缀下的全部瞬时值（例如 `queue.depth`）。"""
        with self._lock:
            return {k: s.value for k, s in self._series.items()
                    if s.kind == "gauge" and k.startswith(prefix)}

    def to_jsonl(self) -> str:
        """一行一条序列（JSONL：可直接喂 jq / 落盘对比回归）。"""
        ts = round(time.time(), 3)
        out = []
        for key, body in self.snapshot().items():
            out.append(json.dumps({"ts": ts, "key": key, **body}, ensure_ascii=False,
                                  sort_keys=True))
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True)

    def reset(self) -> None:
        with self._lock:
            self._series.clear()
            self.series_overflow = 0
            self.started_at = time.time()
            self._bytes_total = self._pages_total = 0
        self.rates = RateWindow()


def _key_of(key: str, labels: dict | None) -> str:
    if not labels:
        return str(key)
    body = ",".join(f"{k}={labels[k]}" for k in sorted(labels) if labels[k] is not None)
    return f"{key}{{{body}}}" if body else str(key)


# 进程内单例：组件直接 `from daedalus.obs.metrics import METRICS` 使用。
METRICS = Metrics()

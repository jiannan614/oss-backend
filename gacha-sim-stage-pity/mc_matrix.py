#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mc_matrix.py · 蒙特卡洛矩阵引擎（NumPy）

> 本文件在 各个抽卡服务的包 各放一份（和 `donut.py` 一样），
> 三个次前端各自独立部署，不走跨目录 import。改一处记得同步另外两份。

## 为什么能「一次算完」

「保底」定义的是一个**更新过程**：抽到一次货之后，概率表从第 1 抽重新开始，
所以「相邻两次出货之间隔了几抽」是**独立同分布**的随机变量，分布只由保底表决定：

    P(间隔 = k) = ∏_{j<k} (1 - h(j)) · h(k)          h(p) = 第 p 抽的出货率

这条性质把「一万轮 × 每轮几百抽」的逐抽循环，换成一次矩阵运算：

1. 一次性抽 `runs × K` 个间隔（K = 一轮最多需要几次出货）；
2. `cumsum` 得到「第 k 次出货落在第几抽」；
3. 布尔索引 + `cumsum` 数「第几个 UP 落在第几抽」。

全程没有逐抽循环、也没有逐轮循环。统计（最欧 / 最非 / 均值 / 中位 / 分位 /
分档计数）也在这里算完，前端只拿一个几百字节的 dict，不再自己遍历一万个抽数。
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

# 「没算出来」的哨兵值：比任何可能的抽数都大，命中它的轮次交给标量循环兜底
UNRESOLVED = np.int64(1 << 30)

Tier = tuple[str, "int | None", "int | None"]   # (标签, 下界(含), 上界(含))


# ---------------------------------------------------------------- 间隔采样
def build_gap_lut(hazard: Sequence[float], n_bits: int = 16) -> np.ndarray:
    """把「第 k 抽出货」的分布做成反查表：均匀数 → 间隔抽数。

    hazard[p-1] 是「距上次出货 p 抽时，这一抽出货」的概率（第 p 抽）。
    返回长度 2**n_bits 的整数表，直接拿 `u * 2**n_bits` 当索引即可反查。
    65536 个桶足够细：每个桶的累积概率误差 < 1.6e-5，对均值的影响远小于 0.01 抽。
    """
    h = np.asarray(hazard, dtype=np.float64)
    if h.ndim != 1 or h.size == 0:
        raise ValueError("hazard 必须是一维非空数组")
    # 生存概率：撑到第 k 抽还没出货
    survive = np.cumprod(np.concatenate(([1.0], 1.0 - h[:-1])))
    pmf = survive * h
    total = pmf.sum()
    if total <= 0:
        raise ValueError("hazard 分布无效（累计概率为 0）")
    cdf = np.cumsum(pmf / total)
    cdf[-1] = 1.0

    n = 1 << n_bits
    edges = (np.arange(n, dtype=np.float64) + 0.5) / n
    idx = np.searchsorted(cdf, edges, side="left")
    np.clip(idx, 0, h.size - 1, out=idx)
    dtype = np.int8 if h.size < 127 else np.int16
    return (idx + 1).astype(dtype)


def sample_gaps(rng: np.random.Generator, shape: tuple[int, int],
                lut: np.ndarray, n_bits: int = 16) -> np.ndarray:
    """从反查表里一次性抽 `shape` 个间隔（int32，方便后面 cumsum）。"""
    n = 1 << n_bits
    u = rng.random(shape, dtype=np.float32)
    idx = (u * n).astype(np.int32)
    np.minimum(idx, n - 1, out=idx)
    return lut[idx].astype(np.int32)


def first_true(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按行找第一个 True：返回 (是否有 True, 该列下标)。

    比 `mask.argmax(1)` 多返回一个「到底有没有」，避免把「全 False」当成第 0 列。
    """
    has = mask.any(axis=1)
    idx = mask.argmax(axis=1)
    return has, idx


def pick(matrix: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """按行取值：`matrix[row, idx[row]]`，1 维 / 2 维下标都支持，越界钳到末列。"""
    idx = np.asarray(idx)
    rows = np.arange(matrix.shape[0]).reshape((-1,) + (1,) * (idx.ndim - 1))
    return matrix[rows, np.clip(idx, 0, matrix.shape[1] - 1)]


def geometric_count(rng: np.random.Generator, shape: tuple[int, ...],
                    p: float) -> np.ndarray:
    """一列「第几次才成功」的几何分布（值域 1,2,3,...，P(k)=(1-p)^(k-1)·p）。

    用逆变换采样：u∈(0,1)，k = 1 + floor(log(u) / log(1-p))。
    """
    u = rng.random(shape, dtype=np.float64)
    np.clip(u, np.finfo(np.float64).tiny, 1.0, out=u)
    k = 1 + np.floor(np.log(u) / np.log1p(-p))
    return k.astype(np.int32)


# ---------------------------------------------------------------- 分批
# 大轮次时分批算，把峰值内存摁在「一批」的量级（20 万轮也只占几十 MB）。
# 这是批数级的循环（≤ 9 次），不是逐轮循环，也不影响分布。
CHUNK_RUNS = 25_000


def in_chunks(total: int, chunk: int = CHUNK_RUNS):
    """把 `total` 轮切成若干批，产出 (起, 止)。"""
    for start in range(0, max(int(total), 0), chunk):
        yield start, min(start + chunk, int(total))


# ---------------------------------------------------------------- 统计汇总
def tier_counts(draws: np.ndarray, tiers: Iterable[Tier]) -> list[dict]:
    """按分档统计轮次（下界上界都含端点，None 表示不设限）。"""
    n = draws.size
    out: list[dict] = []
    for label, lo, hi in tiers:
        mask = np.ones(n, dtype=bool)
        if lo is not None:
            mask &= draws >= lo
        if hi is not None:
            mask &= draws <= hi
        count = int(mask.sum())
        out.append({"label": label, "lo": lo, "hi": hi,
                    "count": count, "pct": count / n if n else 0.0})
    return out


def summarize(draws: np.ndarray, *, target: int, gem_per_draw: int = 0,
              tiers: Iterable[Tier], elapsed_ms: float = 0.0,
              extra: dict | None = None) -> dict:
    """把一轮轮抽数汇总成前端要的那点东西（几百字节，不传原始样本）。

    全部用 NumPy 向量化，不做全排序、也没有 Python 逐元素遍历：
      · min/max/mean 是 O(n) 归约；
      · 分位数用 `np.partition` 只做部分排序（O(n) 平均，比 `np.sort` 的 O(n log n) 快），
        只把需要的那个位置归位，不排整份数组；
      · 分档计数是整数组的布尔掩码求和。

    分位数口径与旧的前端实现一致：取 `sorted[int(n*p)]`，不做插值。
    """
    a = np.asarray(draws, dtype=np.int64)
    n = int(a.size)
    if n == 0:
        return {"runs": 0, "target": target, "min": 0, "max": 0, "mean": 0.0,
                "median": 0, "p10": 0, "p25": 0, "p75": 0, "p90": 0,
                "per_up": 0.0, "tiers": tier_counts(a, tiers),
                "elapsed_ms": round(elapsed_ms, 3), "draws_per_sec": 0}

    # min/max/mean：单趟归约，不排序
    lo = int(a.min())
    hi = int(a.max())
    mean = float(a.mean())

    # 分位数：只把需要的下标归位（partial partition），不做全排序。
    # 需要的下标按升序，一次 partition 覆盖所有位置。
    probs = (0.10, 0.25, 0.50, 0.75, 0.90)
    idx = [min(n - 1, int(n * p)) for p in probs]
    part = np.partition(a, idx)
    vals = part[idx]
    p10, p25, median, p75, p90 = (int(v) for v in vals)

    summary = {
        "runs": n,
        "target": target,
        "min": lo,
        "max": hi,
        "mean": mean,
        "median": median,
        "p10": p10,
        "p25": p25,
        "p75": p75,
        "p90": p90,
        "per_up": mean / target if target else 0.0,
        "tiers": tier_counts(a, tiers),
        "elapsed_ms": round(elapsed_ms, 3),
        "draws_per_sec": round(n / elapsed_ms * 1000) if elapsed_ms > 0 else 0,
    }
    if gem_per_draw:
        summary["gems"] = mean * gem_per_draw
    if extra:
        summary.update(extra)
    return summary


def hit_rate(draws: np.ndarray, limit: int) -> float:
    """多少比例的轮次在 `limit` 抽内就搞定了（用于文案里的把握度）。"""
    n = draws.size
    return float((draws <= limit).mean()) if n else 0.0


def timing_text(summary: dict, budget_ms: float = 100.0) -> str:
    """前端显示用的一句话：后端推演了多少轮、花了多久、达没达到「1 万轮 < 0.1 秒」。

    预算按「每万轮」折算，所以跑 20 万轮也不会因为总耗时变长就误报超预算。
    """
    runs = summary.get("runs", 0) or 0
    ms = summary.get("elapsed_ms", 0.0) or 0.0
    per_ten_k = ms / runs * 10_000 if runs else 0.0
    mark = "✓ 达标" if per_ten_k <= budget_ms else "⚠ 超预算"
    return (f"后端矩阵推演 {runs:,} 轮 · {ms:.1f} ms"
            f"（{per_ten_k:.1f} ms / 万轮，预算 {budget_ms:.0f} ms / 万轮　{mark}）")

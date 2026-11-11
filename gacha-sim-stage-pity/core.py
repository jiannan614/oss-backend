#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py —— 三阶段保底池抽卡内核（纯逻辑，无 IO）

规则（全部是常量，改规则只动本文件顶部）：

    最高稀有度   0.8%
    三阶段软保底 第 66 抽起分三段加速：
                 66–70 抽  每抽 +5.6%
                 71–75 抽  28% 起，每抽 +11.2%
                 76–79 抽  95.2% 起，每抽 +1.2%
                 第 80 抽  100%（硬保底）
    次级概率     四星 6%，且 10 抽内必出四星
    五星池权重   UP 5 : 其余各 1
    歪一次       下一次必出 UP（不跨池继承）
    满阶目标     7 个 UP
    货币         1 抽 = 160 抽卡货币

GachaCore 逐抽实现；draws_to_target_matrix() 是同一规则的矩阵版。
"""

from __future__ import annotations

import multiprocessing as mp
import random
import time
from bisect import bisect
from dataclasses import dataclass
from itertools import accumulate
from typing import Callable, Iterator

import numpy as np

import mc_matrix

VERSION = "v1.2.0"
BANNER = "示例卡池"
PERIOD = "3.7 上半活动抽取"

# ========== 卡池 ==========
UP = "UP角色"
POOL_FIVE = ["UP角色", "角色01", "角色02", "角色03", "角色04", "角色05"]
WEIGHTS_FIVE = [5, 1, 1, 1, 1, 1]

# 四星 UP 角色（3 个，官方：四星内容里 50% 是 UP、每个均等）
FOUR_UP = ["角色00", "角色01", "角色02"]
# 其他四星 = 常驻四星角色 + 四星武器
FOUR_ROLE = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08"]
FOUR_WEAPON = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14", "角色15", "角色16", "角色17", "角色18", "角色19"]
# 四星内容池：UP + 其他（角色 + 武器）。官方：50% 是 UP、50% 是其他。
# 权重取 UP 每个 29、其他每个 3 → 3×29 : 29×3 = 87 : 87 = 正好 50 : 50。
POOL_FOUR = FOUR_UP + FOUR_ROLE + FOUR_WEAPON
WEIGHTS_FOUR = [29] * len(FOUR_UP) + [3] * (len(FOUR_ROLE) + len(FOUR_WEAPON))

# 三星武器（15 把）
POOL_THREE = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14"]

CUM_FIVE = list(accumulate(WEIGHTS_FIVE))
CUM_FOUR = list(accumulate(WEIGHTS_FOUR))
SUM_FIVE, SUM_FOUR = CUM_FIVE[-1], CUM_FOUR[-1]

# ========== 规则常量 ==========
FIVE_BASE = 8            # 千分比：0.8%
HARD_PITY = 80           # 第 80 抽必出
SOFT_START = 66          # 第 66 抽起软保底
FOUR_BASE = 6            # 百分比
FOUR_PITY = 10           # 10 抽内必出四星
GEM_PER_DRAW = 160       # 1 抽 = 160 抽卡货币
TARGET_UP = 7            # 满阶：7 个 UP
MC_DEFAULT_RUNS = 10000
CHUNKS_PER_WORKER = 4    # 细粒度切块，缓解单组耗时方差造成的负载不均

FIVE, FOUR, THREE = 5, 4, 3
RARITY_LABEL = {FIVE: "★★★★★", FOUR: "★★★★", THREE: "★★★"}

# 蒙特长洛的分档口径（判定标准放内核里，前端只负责上色）
# (标签, 下界(含), 上界(含))；None = 不设限
MC_TIERS = (
    ("天选之子", None, 280),
    ("欧皇", 281, 400),
    ("普通人", 401, 550),
    ("非酋", 551, 700),
    ("地狱倒霉蛋", 701, None),
)


def five_rate(pity: int) -> int:
    """第 pity 抽的五星概率（千分比），与 demo 的 _roll_five 一致。"""
    if pity >= HARD_PITY:
        return 1000
    if SOFT_START <= pity <= 70:
        return (pity - 65) * 56
    if 71 <= pity <= 75:
        return 280 + (pity - 70) * 112
    if 76 <= pity <= 79:
        return 952 + (pity - 75) * 12
    return FIVE_BASE


# 五星间隔分布只取决于保底表，模块导入时算一次反查表
FIVE_GAP_LUT = mc_matrix.build_gap_lut(
    [five_rate(p) / 1000 for p in range(1, HARD_PITY + 1)]
)


@dataclass(frozen=True)
class DrawResult:
    index: int
    name: str
    rarity: int
    note: str = ""


class MCCore:
    """纯逻辑卡池内核：零 IO，状态全封装在实例里。"""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.reset()

    def reset(self) -> None:
        self.total = 0
        self.soft_pity = 0        # 距上一个五星多少抽
        self.four_pity = 0        # 距上一个四星多少抽
        self.up_guaranteed = False    # 上一个五星歪了 → 下一个必 UP
        self.four_guaranteed = False  # 上一个四星歪了 → 下一个必 UP
        self.count_five = 0
        self.count_five_up = 0
        self.count_four = 0
        self.count_four_up = 0
        self.count_three = 0
        self.count_big_pity = 0   # 吃到「必 UP」的次数
        self.five_detail: dict[str, int] = {}

    # ---------------- 概率 ----------------
    def _five_rate(self, pity: int) -> int:
        """第 pity 抽的五星概率（千分比），与 demo 的 _roll_five 一致。"""
        return five_rate(pity)

    @property
    def five_rate_now(self) -> float:
        """下一抽的五星率（0~1），给界面上的保底进度用。"""
        return self._five_rate(min(self.soft_pity + 1, HARD_PITY)) / 1000

    def _pick_five(self) -> str:
        return POOL_FIVE[bisect(CUM_FIVE, self.rng.random() * SUM_FIVE)]

    def _pick_four(self) -> str:
        return POOL_FOUR[bisect(CUM_FOUR, self.rng.random() * SUM_FOUR)]

    def _pick_four_up(self) -> str:
        return self.rng.choice(FOUR_UP)

    def _pick_three(self) -> str:
        return self.rng.choice(POOL_THREE)

    # ---------------- 抽一抽 ----------------
    def step(self) -> tuple[int, str, str]:
        self.total += 1
        self.soft_pity += 1
        self.four_pity += 1

        # ---- 五星 ----
        if self.rng.randint(1, 1000) <= self._five_rate(self.soft_pity):
            self.soft_pity = 0
            self.count_five += 1
            if self.up_guaranteed:
                name = UP
                self.up_guaranteed = False
                self.count_big_pity += 1
                note = "大保底·UP"
            else:
                name = self._pick_five()
                if name == UP:
                    note = "UP"
                else:
                    self.up_guaranteed = True
                    note = "歪 · 下次五星必UP"
            if name == UP:
                self.count_five_up += 1
            self.five_detail[name] = self.five_detail.get(name, 0) + 1
            return FIVE, name, note

        # ---- 四星（含 10 抽保底）----
        if self.four_pity >= FOUR_PITY or self.rng.randint(1, 100) <= FOUR_BASE:
            self.four_pity = 0
            self.count_four += 1
            if self.four_guaranteed:
                name = self._pick_four_up()
                self.four_guaranteed = False
                note = "四星大保底·UP"
            else:
                name = self._pick_four()
                if name in FOUR_UP:
                    note = "四星UP"
                else:
                    self.four_guaranteed = True
                    note = ""
            if name in FOUR_UP:
                self.count_four_up += 1
            return FOUR, name, note

        # ---- 三星 ----
        self.count_three += 1
        return THREE, self._pick_three(), ""

    def draw_one(self) -> DrawResult:
        rarity, name, note = self.step()
        return DrawResult(self.total, name, rarity, note)

    def run(self, n: int) -> Iterator[DrawResult]:
        for _ in range(n):
            yield self.draw_one()

    def draws_to_target(self, target: int = TARGET_UP) -> int:
        """蒙特卡洛单轮：一路抽到集满 target 个 UP（满阶）为止，返回总抽数。"""
        while self.count_five_up < target:
            self.step()
        return self.total


# ========== 渲染 ==========
def format_line(ev: DrawResult) -> str:
    label = RARITY_LABEL.get(ev.rarity, "")
    note = f" [{ev.note}]" if ev.note else ""
    return f"第{ev.index}抽 | {label} {ev.name}{note}"


def stats_text(core: MCCore) -> str:
    """命令行口径的统计（Web 上不用，留着方便贴日志/自测）。"""
    if core.total == 0:
        return "=== 统计 ===\n尚无抽取数据"
    avg_five = f"{core.total / core.count_five:.2f}" if core.count_five else "—"
    up_rate = f"{core.count_five_up / core.count_five * 100:.2f}" if core.count_five else "—"
    return (f"=== 统计 ===\n"
            f"总抽数:{core.total} | 消耗抽卡货币:{core.total * GEM_PER_DRAW:,} "
            f"({GEM_PER_DRAW}/抽) | 五星:{core.count_five}（UP {core.count_five_up}）| "
            f"四星:{core.count_four}（UP {core.count_four_up}）| 三星:{core.count_three}\n"
            f"平均 {avg_five} 抽/五星 | 五星中UP率 {up_rate}% | "
            f"大保底触发:{core.count_big_pity} 次")


# ========== 蒙特卡洛 ==========
def _run_chunk(task: tuple[int, int, int]) -> list[int]:
    """worker：跑 n 组模拟，返回每组所需抽数。

    必须是模块级顶层函数，Windows spawn 才能 pickle；子进程只 import 本模块，
    不会碰到 Streamlit。
    """
    n, target, seed = task
    rng = random.Random(seed)
    out: list[int] = []
    for _ in range(n):
        out.append(MCCore(rng).draws_to_target(target))
    return out


def _split(total: int, parts: int) -> list[int]:
    """把 total 均匀切成 parts 块，前 total % parts 块各多 1。"""
    q, r = divmod(total, parts)
    return [q + (1 if i < r else 0) for i in range(parts)]


def _tasks(runs: int, target: int, seed: int | None, chunks: int) -> list[tuple[int, int, int]]:
    master = random.Random(seed) if seed is not None else random.Random()
    sizes = _split(runs, chunks)
    return [(n, target, master.getrandbits(64)) for n in sizes if n > 0]


def simulate(runs: int, target: int = TARGET_UP, seed: int | None = None,
             progress: Callable[[int, int], None] | None = None,
             use_pool: bool = True) -> list[int]:
    """跑 runs 组「抽到满阶」的模拟，返回每组抽数。

    Windows 上 Pool 用 spawn，子进程会重新 import 本模块——所以 worker 必须是
    这里的顶层函数，不能让 Streamlit 的脚本参与。Pool 起不来就自动退单进程。
    """
    rng = random.Random(seed) if seed is not None else random.Random()
    out: list[int] = []
    if use_pool and runs >= 500:
        try:
            workers = max(1, min(mp.cpu_count(), runs))
            chunks = min(runs, max(1, workers * CHUNKS_PER_WORKER))
            with mp.Pool(processes=min(workers, chunks)) as pool:
                for part in pool.imap_unordered(_run_chunk, _tasks(runs, target, seed, chunks),
                                                chunksize=1):
                    out.extend(part)
                    if progress:
                        progress(len(out), runs)
            return out
        except (OSError, RuntimeError, ValueError):
            out.clear()   # 进程池不可用，退回单进程重跑
    for i in range(runs):
        out.append(MCCore(rng).draws_to_target(target))
        if progress and (i + 1) % max(1, runs // 40) == 0:
            progress(i + 1, runs)
    return out


def summarize(totals: list[int], target: int = TARGET_UP) -> dict:
    """把一组抽数整理成报告要用的统计量。"""
    s = sorted(totals)
    n = len(s)
    if not n:
        return {}
    pct = lambda p: s[min(n - 1, int(n * p))]
    mean = sum(s) / n
    return {
        "runs": n, "min": s[0], "max": s[-1], "mean": mean, "median": pct(0.50),
        "p10": pct(0.10), "p25": pct(0.25), "p75": pct(0.75), "p90": pct(0.90),
        "gems": mean * GEM_PER_DRAW, "per_up": mean / target,
    }


# ========== 蒙特卡洛 · 矩阵版（NumPy，无逐抽循环） ==========
def draws_to_target_matrix(runs: int, target: int = TARGET_UP,
                           seed: int | None = None) -> np.ndarray:
    """一次矩阵运算跑完 `runs` 轮「抽到满阶」，返回每轮消耗的抽数。

    推导（关键就三行）：
      · 五星之间隔几抽，只由保底表决定 → 独立同分布，可以一次性抽 `runs × K` 个；
      · `cumsum` 之后 `pos[i, j]` 就是「第 i 轮的第 j 次五星落在第几抽」；
      · 每个 UP 消耗 1 次五星（直接命中，50%）或 2 次（先歪、下次必 UP，50%），
        把每轮 target 个 UP 的消耗累加起来，就知道第 target 个 UP 打在第几次五星上。
    `K = 2 * target` 是精确上界（最坏情况每次都歪），所以不需要兜底。
    """
    n = int(runs)
    if n <= 0:
        return np.empty(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    k = 2 * int(target)
    gaps = mc_matrix.sample_gaps(rng, (n, k), FIVE_GAP_LUT)
    pos = np.cumsum(gaps, axis=1)
    cost = 1 + (rng.random((n, int(target))) >= 0.5)   # 1 次 = 直接 UP；2 次 = 歪一次
    need = np.cumsum(cost, axis=1)[:, -1] - 1          # 第 target 个 UP 用掉第几次五星
    return mc_matrix.pick(pos, need).astype(np.int64)


def simulate_summary(runs: int, target: int = TARGET_UP,
                     seed: int | None = None) -> dict:
    """跑一轮蒙特卡洛并**在后端把统计做完**，只回一个精简 dict 给前端。

    返回字段见 `mc_matrix.summarize`：runs / min / max / mean / median /
    p10·p25·p75·p90 / gems / per_up / tiers（分档计数）/ elapsed_ms。
    前端拿到直接排版，不再遍历一万个抽数。
    """
    t0 = time.perf_counter()
    draws = draws_to_target_matrix(runs, target, seed)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return mc_matrix.summarize(draws, target=target, gem_per_draw=GEM_PER_DRAW,
                            tiers=MC_TIERS, elapsed_ms=elapsed_ms)

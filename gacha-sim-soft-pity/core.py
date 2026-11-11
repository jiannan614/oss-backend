#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py —— 软保底池抽卡内核（纯逻辑，无 IO）

规则（全部是常量，改规则只动本文件顶部）：

    最高稀有度   2%
    软保底       连续 50 抽未出 → 第 51 抽 4%，之后每抽 +2%
    硬保底       第 99 抽 100% 必出；出任意最高稀有度后计数归零
    双 UP        合计占最高稀有度的 70%（主 UP 35% + 副 UP 35%）
    次级分配     非最高稀有度时按 8 : 50 : 40 分给五星 / 四星 / 三星
    累计赠送     满 300 抽额外赠 1 个主 UP，仅一次，不产出兑换点数
    满阶目标     6 个主 UP
    货币         1 抽 = 600 抽卡货币
    十连保底     开关 TEN_PITY_5STAR 默认关闭，改 True 即启用「十连必出五星+」

GachaCore 逐抽实现；draws_to_target_matrix() 是同一规则的矩阵版。
"""

from __future__ import annotations

import random
import time
from bisect import bisect
from dataclasses import dataclass
from itertools import accumulate
from typing import Iterator

import numpy as np

import mc_matrix

VERSION = "v1.2.0"
BANNER = "示例卡池"
PERIOD = "2026 周年庆限定抽取"

# ========== 卡池名单 ==========
MAIN_UP = "UP角色"
SIDE_UP = "副 UP 角色"

POOL_SIX_OTHER = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14", "角色15", "角色16", "角色17", "角色18", "角色19", "角色20", "角色21", "角色22", "角色23", "角色24", "角色25", "角色26", "角色27", "角色28", "角色29", "角色30", "角色31", "角色32", "角色33", "角色34", "角色35", "角色36", "角色37", "角色38", "角色39", "角色40", "角色41", "角色42", "角色43", "角色44", "角色45", "角色46", "角色47", "角色48", "角色49", "角色50", "角色51", "角色52", "角色53"]
POOL_SIX_BOOST = ["角色00", "角色01", "角色02"]
POOL_FIVE = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14", "角色15", "角色16", "角色17", "角色18", "角色19", "角色20", "角色21", "角色22", "角色23", "角色24", "角色25", "角色26", "角色27", "角色28", "角色29", "角色30", "角色31", "角色32", "角色33", "角色34", "角色35", "角色36", "角色37", "角色38", "角色39", "角色40", "角色41", "角色42", "角色43", "角色44", "角色45"]
POOL_FOUR = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14", "角色15", "角色16", "角色17", "角色18", "角色19", "角色20", "角色21", "角色22", "角色23", "角色24", "角色25", "角色26", "角色27", "角色28", "角色29", "角色30", "角色31", "角色32", "角色33", "角色34", "角色35", "角色36", "角色37", "角色38", "角色39", "角色40", "角色41", "角色42", "角色43", "角色44", "角色45", "角色46", "角色47"]
POOL_THREE = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08", "角色09", "角色10", "角色11", "角色12", "角色13", "角色14", "角色15"]

OTHER_SIX = POOL_SIX_OTHER + POOL_SIX_BOOST
OTHER_WEIGHTS = [1] * len(POOL_SIX_OTHER) + [5] * len(POOL_SIX_BOOST)
OTHER_CUM = list(accumulate(OTHER_WEIGHTS))
OTHER_TOTAL = OTHER_CUM[-1]

# ========== 规则常量 ==========
SIX_BASE = 0.02          # 基础六星率 2%
SIX_SOFT_START = 50      # 连续 50 抽未出后开始递增
SIX_STEP = 0.02          # 每抽 +2%
SIX_HARD_PITY = 99       # 第 99 抽 100%
UP_SHARE = 0.35          # UP角色 35% / 副 UP 角色 35%（合计 70%）
FIVE_SHARE = 8           # 非六星时按 8 : 50 : 40 分配
FOUR_SHARE = 50
THREE_SHARE = 40
NON_SIX_TOTAL = FIVE_SHARE + FOUR_SHARE + THREE_SHARE   # 98
WELL_AT = 300            # 累计 300 抽赠送主 UP 兑换券（限一次）
GEM_PER_DRAW = 600       # 1 抽 = 600 抽卡货币
TARGET_MAIN_UP = 6       # 满阶：1 本体 + 5 兑换券
MC_DEFAULT_RUNS = 10000
TEN_PITY_5STAR = False   # 官方「十连必出 5★+」，源文本未列，默认关闭

SIX, FIVE, FOUR, THREE = 6, 5, 4, 3
RARITY_LABEL = {SIX: "★★★★★★", FIVE: "★★★★★", FOUR: "★★★★", THREE: "★★★"}

# 蒙特卡洛的分档口径（按 300 井的体感划分），判定标准放内核，前端只负责上色
# (标签, 下界(含), 上界(含))；None = 不设限
MC_TIERS = (
    ("极欧", None, 249),
    ("正常", 250, 400),
    ("偏非", 401, 550),
    ("极非", 551, None),
)
# 矩阵引擎里一轮最多抽几次六星：96 次里凑不到 6 个 UP 的概率 ≈ 1e-9，
# 真落到这个尾巴上会被逐抽循环兜底，不影响正确性。
MC_MAX_EVENTS = 96


def six_rate(pity: int) -> float:
    """第 pity 抽（距上次六星）的六星率，与 AKPool.step 完全一致。"""
    return min(1.0, SIX_BASE + max(0, pity - SIX_SOFT_START) * SIX_STEP)


# 六星间隔分布（只由保底表决定），模块导入时算一次反查表
SIX_GAP_LUT = mc_matrix.build_gap_lut(
    [six_rate(p) for p in range(1, SIX_HARD_PITY + 1)]
)


@dataclass(frozen=True)
class DrawResult:
    index: int
    name: str
    rarity: int
    note: str = ""


@dataclass(frozen=True)
class RewardEvent:
    """非抽卡事件（例如 300 抽赠送兑换券），单独一类，别混进记录表。"""
    text: str
    index: int = 0
    name: str = MAIN_UP


SimEvent = DrawResult | RewardEvent


class AKPool:
    """纯逻辑卡池内核：零 IO，状态全封装在实例里。"""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.reset()

    def reset(self) -> None:
        self.total = 0
        self.pity = 0              # 连续未出六星的抽数
        self.since_five_plus = 0   # 十连保底用（TEN_PITY_5STAR）
        self.well_used = False
        self.count_six = 0
        self.count_five = 0
        self.count_four = 0
        self.count_three = 0
        self.count_main_up = 0     # 抽卡获得的UP角色
        self.count_gift_up = 0     # 300 抽赠送
        self.six_detail: dict[str, int] = {}

    @property
    def main_up_units(self) -> int:
        """已获得的主 UP 单位数（含 300 抽赠送的兑换券）。"""
        return self.count_main_up + self.count_gift_up

    @property
    def six_rate_now(self) -> float:
        """当前这一抽的六星率（给界面上的保底进度用）。"""
        return six_rate(self.pity)

    def _pick_other_six(self, detailed: bool) -> str:
        if not detailed:
            return "其他六星"
        return OTHER_SIX[bisect(OTHER_CUM, self.rng.random() * OTHER_TOTAL)]

    def _pick_six(self, detailed: bool) -> str:
        r = self.rng.random()
        if r < UP_SHARE:
            return MAIN_UP
        if r < UP_SHARE * 2:
            return SIDE_UP if detailed else "陪跑UP"
        return self._pick_other_six(detailed)

    def step(self, detailed: bool = True) -> tuple[int, str, str, list[SimEvent]]:
        """执行一抽。detailed=False 为蒙特卡洛快路径：跳过非 UP 六星的具名抽取。"""
        self.total += 1
        self.pity += 1
        self.since_five_plus += 1

        rate = six_rate(self.pity)
        note = ""
        if self.rng.random() < rate:
            rarity = SIX
            name = self._pick_six(detailed)
            self.pity = 0
            self.since_five_plus = 0
            self.count_six += 1
            if name == MAIN_UP:
                self.count_main_up += 1
                note = "主UP"
            elif name == SIDE_UP:
                note = "陪跑UP"
            if detailed:
                self.six_detail[name] = self.six_detail.get(name, 0) + 1
        else:
            r = self.rng.randint(1, NON_SIX_TOTAL)
            if r <= FIVE_SHARE or (TEN_PITY_5STAR and self.since_five_plus >= 10):
                rarity = FIVE
                name = POOL_FIVE[self.rng.randrange(len(POOL_FIVE))] if detailed else "五星角色"
                self.count_five += 1
            elif r <= FIVE_SHARE + FOUR_SHARE:
                rarity = FOUR
                name = POOL_FOUR[self.rng.randrange(len(POOL_FOUR))] if detailed else "四星角色"
                self.count_four += 1
            else:
                rarity = THREE
                name = POOL_THREE[self.rng.randrange(len(POOL_THREE))] if detailed else "三星角色"
                self.count_three += 1
            if rarity == FIVE:
                self.since_five_plus = 0

        # 累计 300 抽额外赠送主 UP 兑换券：限一次，不影响任何保底计数
        events: list[SimEvent] = []
        if self.total >= WELL_AT and not self.well_used:
            self.well_used = True
            self.count_gift_up += 1
            events.append(RewardEvent(
                f"累计{self.total}抽 | 额外赠送 | {MAIN_UP} 兑换券 ×1 | 限一次 |",
                index=self.total, name=MAIN_UP,
            ))
        return rarity, name, note, events

    def draw_one(self) -> tuple[DrawResult, list[SimEvent]]:
        rarity, name, note, events = self.step(detailed=True)
        return DrawResult(self.total, name, rarity, note), events

    def run(self, n: int) -> Iterator[SimEvent]:
        for _ in range(n):
            result, events = self.draw_one()
            yield result
            yield from events

    def draws_to_target(self, target: int = TARGET_MAIN_UP) -> int:
        """蒙特卡洛单轮：一路抽到集齐 target 个主 UP（含 300 抽赠送）为止，返回总抽数。"""
        while self.main_up_units < target:
            self.step(detailed=False)
        return self.total


# ========== 蒙特卡洛 · 矩阵版（NumPy，无逐抽 / 逐轮循环） ==========
def _draws_batch(rng: np.random.Generator, n: int, target: int) -> np.ndarray:
    """单批：一次矩阵运算跑完 n 轮「抽到满阶」，返回每轮消耗的抽数。

    推导：
      · 六星之间隔几抽只由保底表决定 → 独立同分布，一次性抽 `n × K` 个；
      · `cumsum` 得到「第 j 次六星落在第几抽」；
      · 每次六星独立按 35% 判 UP，再 `cumsum` 就知道第 6 个 UP 是第几次六星；
      · 300 抽赠送只 +1 个，所以：6 个 UP 在 300 抽内 → 就是那一抽；
        否则「第 5 个 UP 的那一抽」和「第 300 抽」取晚的那个（300 抽补第 6 个）。
    """
    k = MC_MAX_EVENTS
    pos = np.cumsum(mc_matrix.sample_gaps(rng, (n, k), SIX_GAP_LUT), axis=1)
    up = (rng.random((n, k)) < UP_SHARE).astype(np.int32)
    cum = np.cumsum(up, axis=1)

    has_full, idx_full = mc_matrix.first_true(cum >= target)
    has_one_less, idx_one_less = mc_matrix.first_true(cum >= target - 1)
    d_full = np.where(has_full, mc_matrix.pick(pos, idx_full), mc_matrix.UNRESOLVED)
    d_less = np.where(has_one_less, mc_matrix.pick(pos, idx_one_less), mc_matrix.UNRESOLVED)

    draws = np.where(d_full <= WELL_AT, d_full, np.maximum(d_less, WELL_AT))
    return np.where(has_one_less, draws, mc_matrix.UNRESOLVED)


def draws_to_target_matrix(runs: int, target: int = TARGET_MAIN_UP,
                           seed: int | None = None) -> np.ndarray:
    """跑 `runs` 轮「抽到满阶」，返回每轮消耗的抽数（大轮次自动分批，控制内存）。

    `K = 96` 只是性能上的截断（96 次六星凑不到 6 个 UP 的概率 ≈ 1e-9），
    真落到这个尾巴上的轮次会用逐抽循环重算，结果一定对得上。
    """
    n = int(runs)
    if n <= 0:
        return np.empty(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    out = np.empty(n, dtype=np.int64)
    for start, stop in mc_matrix.in_chunks(n):
        out[start:stop] = _draws_batch(rng, stop - start, int(target))

    odd = np.flatnonzero(out >= mc_matrix.UNRESOLVED)
    if odd.size:                                  # 尾巴：交回逐抽循环，保证精确
        rng_py = random.Random(seed)
        for i in odd:
            out[i] = AKPool(rng_py).draws_to_target(target)
    return out


def simulate_summary(runs: int, target: int = TARGET_MAIN_UP,
                     seed: int | None = None) -> dict:
    """跑一轮蒙特卡洛并**在后端把统计做完**，只回一个精简 dict 给前端。

    含 runs / min / max / mean / median / p10·p25·p75·p90 /
    gems / per_up / tiers（分档计数）/ gift_runs（用上 300 抽赠送的轮次）/
    elapsed_ms。前端拿到直接排版，不再遍历一万个抽数。
    """
    t0 = time.perf_counter()
    draws = draws_to_target_matrix(runs, target, seed)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    gift_runs = int((draws >= WELL_AT).sum())
    return mc_matrix.summarize(
        draws, target=target, gem_per_draw=GEM_PER_DRAW, tiers=MC_TIERS,
        elapsed_ms=elapsed_ms,
        extra={"gift_runs": gift_runs, "gift_pct": gift_runs / max(1, draws.size)},
    )


# ========== 渲染 ==========
def format_line(ev: SimEvent) -> str:
    if isinstance(ev, RewardEvent):
        return ev.text
    label = RARITY_LABEL.get(ev.rarity, "")
    note = f" [{ev.note}]" if ev.note else ""
    return f"第{ev.index}抽 | {label} {ev.name}{note}"


def stats_text(core: AKPool) -> str:
    """命令行口径的统计（Web 上不用，留着方便贴日志/自测）。"""
    if core.total == 0:
        return "=== 统计 ===\n尚无抽取数据"
    avg_six = f"{core.total / core.count_six:.1f}" if core.count_six else "—"
    avg_up = f"{core.total / core.main_up_units:.1f}" if core.main_up_units else "—"
    detail = " ".join(f"{n} x{c}" if c > 1 else n for n, c in core.six_detail.items())
    return (f"=== 统计 ===\n"
            f"总抽数:{core.total} | 平均 {avg_six} 抽/六星 | "
            f"平均 {avg_up} 抽/主UP({MAIN_UP} {core.main_up_units}个, 含赠送{core.count_gift_up}) | "
            f"累计消耗抽卡货币:{core.total * GEM_PER_DRAW:,} ({GEM_PER_DRAW}货币/抽)\n"
            f"六星详细：{detail or '暂无六星'}")

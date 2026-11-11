#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py —— 角色池抽卡内核（纯逻辑，无 IO）

规则（全部是常量，改规则只动本文件顶部）：

    基础概率   六星 0.8%，五星 8%
    软保底     第 66 抽起，每抽六星率 +5%
    硬保底     第 80 抽必出六星
    十连保底   连续 10 抽无五星 → 强制五星
    大保底     前 120 抽内必得当期 UP，同池仅一次、不继承：
               提前抽到 → 大保底消耗、立即失效
               第 120 抽仍未出 → 该抽强制 UP，小保底清零
    累计赠送   每满 240 抽额外赠 1 个兑换券，不占抽数、不动计数器
    保底十连   累计 30 抽触发一次，完全独立：不写计数器、不吃软保底、
               十连内必出五星及以上
    满阶目标   6 个 UP（1 本体 + 5 兑换券）
    货币       1 抽 = 500 抽卡货币

GachaCore 是「一次一抽」的标量实现；draws_to_target_matrix() 是同一规则的
蒙特卡洛矩阵版（依赖 mc_matrix.py），两者口径必须一致。
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

# ========== 卡池配置（换版本只改这几行） ==========
UP = "UP角色"                 # 当期 UP 角色
UP_BANNER = "示例卡池"     # 当期卡池名
UP_PERIOD = "2026.11.5 – 版本更新维护前"
VERSION = "v1.2.0"
POOL_SIX = ["UP角色", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07"]
# 首位是 UP 的权重，其余每位常驻 1：UP 权重 = 常驻数量 → 六星里 UP 占 50%
# （早期多了一个并不存在的「别离」，删掉后常驻变 7 个，首项同步 8 → 7，仍是 7/14 = 50%）
WEIGHTS_SIX = [7, 1, 1, 1, 1, 1, 1, 1]
POOL_FIVE = ["角色00", "角色01", "角色02", "角色03", "角色04", "角色05", "角色06", "角色07", "角色08"]
WEIGHTS_FIVE = [1, 1, 1, 1, 1, 1, 1, 1, 1]
POOL_FOUR = ["角色00", "角色01", "角色02", "角色03", "角色04"]
WEIGHTS_FOUR = [1, 1, 1, 1, 1]

CUM_SIX = list(accumulate(WEIGHTS_SIX))
CUM_FIVE = list(accumulate(WEIGHTS_FIVE))
CUM_FOUR = list(accumulate(WEIGHTS_FOUR))
SUM_SIX, SUM_FIVE, SUM_FOUR = CUM_SIX[-1], CUM_FIVE[-1], CUM_FOUR[-1]

# ========== 规则常量 ==========
SIX_BASE_RATE = 8          # 千分比，基础 0.8%
SIX_SOFT_PITY = 65         # 第 66 抽起概率线性递增
SIX_RATE_STEP = 50         # 每抽 +50/1000 = +5%
SIX_HARD_PITY = 80         # 硬保底
BIG_PITY_MAX = 120         # 前 120 抽内必得档期 UP（同卡池仅一次）
BIG_PITY_ONCE = True       # True=官方「消耗即失效」；False=消耗后重新计 120 抽
FIVE_BASE_RATE = 8         # 百分比，基础 8%
FIVE_PITY_MAX = 10
GIFT_INTERVAL = 240        # 每累计 240 抽赠送 1 个兑换券
FREE_TEN_TRIGGER = 30      # 累计抽取 30 次触发加急招募
FREE_TEN_SIZE = 10
GEM_PER_DRAW = 500         # 1 抽 = 500 抽卡货币
TARGET_UP_UNITS = 6        # 满阶：1 个本体 + 5 个兑换券

SIX, FIVE, FOUR = 6, 5, 4

RARITY_LABEL = {SIX: "6 星", FIVE: "5 星", FOUR: "4 星"}

# 蒙特卡洛的分档口径（< 欧 / 正常带 / > 偏非），判定标准放内核，前端只负责上色
MC_TIERS = (
    ("欧", None, 349),
    ("正常", 350, 450),
    ("偏非", 451, None),
)
# 矩阵引擎的截断宽度（概率尾巴会被逐抽循环兜底，不影响正确性）：
#   第一段（到第一次 UP 为止）最多算几次六星；后续每段共用一个更宽的间隔池。
MC_FIRST_EVENTS = 24
MC_GAP_POOL = 96
# 一轮最多可能拿到几次「每 240 抽赠送」
MC_GIFT_LIMIT = 8
# 六星里 UP 占多少（权重首位 : 权重和 = 7/14）
UP_SHARE_SIX = WEIGHTS_SIX[0] / SUM_SIX


def six_rate_permille(pity: int) -> int:
    """第 pity 抽（距上次六星）的六星率，千分比整数。

    写成整数是为了和 `GachaCore._roll_six` 的 `randint(1, 1000) <= rate`
    逐位一致（用浮点会出现 458 与 457.9999 这种边界偏差）。
    """
    if pity >= SIX_HARD_PITY:
        return 1000
    return SIX_BASE_RATE + max(0, pity - SIX_SOFT_PITY) * SIX_RATE_STEP


# 六星间隔分布（只由保底表决定），模块导入时算一次反查表
SIX_GAP_LUT = mc_matrix.build_gap_lut(
    [six_rate_permille(p) / 1000 for p in range(1, SIX_HARD_PITY + 1)]
)


@dataclass(frozen=True)
class DrawResult:
    """一次抽卡的结构化结果。free_index > 0 表示这是加急招募里的第几抽。"""

    index: int
    name: str
    rarity: int
    note: str = ""
    free_index: int = 0


@dataclass(frozen=True)
class GiftEvent:
    """累计赠送：独立于抽数的额外事件。"""

    index: int
    name: str


@dataclass(frozen=True)
class PhaseEvent:
    """阶段提示（加急招募开始 / 结束）。"""

    text: str


SimEvent = DrawResult | GiftEvent | PhaseEvent


class GachaCore:
    """纯逻辑卡池内核：不 print、不 input、不读文件。"""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng if rng is not None else random.Random()
        self.reset()

    def reset(self) -> None:
        # 外部（计入保底与累抽）状态
        self.total = 0
        self.small_pity = 0        # 距上次六星
        self.five_pity = 0         # 距上次五星
        self.up_pity = 0           # 本轮大保底累计抽数（仅在大保底有效时增长）
        self.big_pity_active = True    # 本卡池大保底是否仍可触发
        self.big_pity_consumed_at = 0  # 大保底失效时的抽数，0 = 仍有效
        self.big_pity_reason = ""      # "提前出货" / "第120抽必出"
        self.gift_step = 1         # 已赠送轮次 + 1
        self.count_six = 0
        self.count_five = 0
        self.count_four = 0
        self.count_up = 0          # 抽卡获得的 UP 本体/兑换券
        self.count_gift = 0        # 累计赠送
        self.count_big_pity = 0    # 大保底触发次数
        # 加急招募（完全隔离）状态
        self.free_ten_used = False
        self.free_ten_pending = False   # 到点了但还没领（界面手动触发时才置 True）
        self.free_count_six = 0
        self.free_count_five = 0
        self.free_count_four = 0
        self.free_count_up = 0

    @property
    def up_units(self) -> int:
        """已获得的 UP 单位总数：抽卡 + 240 赠送 + 加急招募。"""
        return self.count_up + self.count_gift + self.free_count_up

    # ---------- 规则 ----------
    def _pick(self, pool: list[str], cum: list[int], total_weight: int) -> str:
        return pool[bisect(cum, self.rng.random() * total_weight)]

    def _roll_six(self) -> bool:
        return self.rng.randint(1, 1000) <= six_rate_permille(self.small_pity)

    def _consume_big_pity(self, reason: str) -> None:
        """大保底是消耗品：官方规则下同卡池仅一次，消耗后永久失效。"""
        self.big_pity_consumed_at = self.total
        self.big_pity_reason = reason
        if BIG_PITY_ONCE:
            self.big_pity_active = False
        else:
            self.up_pity = 0       # 非官方循环模式：重新计 120 抽

    def _big_pity_text(self) -> str:
        if self.big_pity_active:
            return f"大保底待触发({self.up_pity}/{BIG_PITY_MAX})"
        return f"大保底已失效@{self.big_pity_consumed_at}抽({self.big_pity_reason})"

    def _run_free_ten(self) -> list[SimEvent]:
        """加急招募：从 0 开始的独立十连，不读也不写任何外部计数器。"""
        events: list[SimEvent] = [PhaseEvent(
            f"累计{self.total}抽 | 触发加急招募 | 免费十连 | 独立计算 · 不计入任何保底与累抽计数 |")]
        got_high = False

        for i in range(1, FREE_TEN_SIZE + 1):
            forced = i == FREE_TEN_SIZE and not got_high
            if forced:
                rarity = SIX if self.rng.randint(1, 1000) <= SIX_BASE_RATE else FIVE
            elif self.rng.randint(1, 1000) <= SIX_BASE_RATE:
                rarity = SIX
            elif self.rng.randint(1, 100) <= FIVE_BASE_RATE:
                rarity = FIVE
            else:
                rarity = FOUR

            if rarity in (SIX, FIVE):
                got_high = True
            if rarity == SIX:
                name = self._pick(POOL_SIX, CUM_SIX, SUM_SIX)
                self.free_count_six += 1
                if name == UP:
                    self.free_count_up += 1
            elif rarity == FIVE:
                name = self._pick(POOL_FIVE, CUM_FIVE, SUM_FIVE)
                self.free_count_five += 1
            else:
                name = self._pick(POOL_FOUR, CUM_FOUR, SUM_FOUR)
                self.free_count_four += 1

            events.append(DrawResult(0, name, rarity,
                                     "十连保底" if forced else "", free_index=i))

        events.append(PhaseEvent(
            f"加急招募结束 | 外部累计仍为 {self.total} 抽 | "
            f"小保底 {self.small_pity}/{SIX_HARD_PITY} · {self._big_pity_text()} |"))
        return events

    def step(self, *, auto_free: bool = True) -> tuple[int, str, str, list[SimEvent]]:
        """执行一抽，返回 (稀有度, 名称, 备注, 额外事件列表)。

        auto_free=False 时，累计 30 抽只把加急招募挂起（free_ten_pending），
        由界面按钮调用 run_free_ten() 真正执行。
        """
        self.total += 1
        self.small_pity += 1
        self.five_pity += 1
        if self.big_pity_active:
            self.up_pity += 1

        # ① 大保底：本卡池前 120 抽没出档期 UP，第 120 抽强制出一个 UP 六星
        if self.big_pity_active and self.up_pity >= BIG_PITY_MAX:
            self.count_six += 1
            self.count_up += 1
            self.count_big_pity += 1
            self.small_pity = 0        # 大保底触发时小保底清零
            self._consume_big_pity("第120抽必出")
            result = (SIX, UP, "大保底")

        # ② 常规六星（软/硬保底）
        elif self._roll_six():
            name = self._pick(POOL_SIX, CUM_SIX, SUM_SIX)
            self.small_pity = 0
            self.count_six += 1
            if name == UP:
                self.count_up += 1
                # 120 抽内提前出了当期 UP → 大保底消耗掉、立即失效
                if self.big_pity_active:
                    self._consume_big_pity("提前出货")
            result = (SIX, name, "")

        # ③ 五星（10 抽保底）或四星
        else:
            forced_five = self.five_pity >= FIVE_PITY_MAX
            if forced_five or self.rng.randint(1, 100) <= FIVE_BASE_RATE:
                name = self._pick(POOL_FIVE, CUM_FIVE, SUM_FIVE)
                self.five_pity = 0
                self.count_five += 1
                result = (FIVE, name, "五星保底" if forced_five else "")
            else:
                name = self._pick(POOL_FOUR, CUM_FOUR, SUM_FOUR)
                self.count_four += 1
                result = (FOUR, name, "")

        # ④ 累计赠送
        if self.total >= GIFT_INTERVAL * self.gift_step:
            self.gift_step += 1
            self.count_gift += 1

        # ⑤ 加急招募：到点后按调用方的意愿，要么立刻跑，要么挂起等玩家点
        free_events: list[SimEvent] = []
        if (self.total >= FREE_TEN_TRIGGER
                and not self.free_ten_used
                and not self.free_ten_pending):
            if auto_free:
                self.free_ten_used = True
                free_events = self._run_free_ten()
            else:
                self.free_ten_pending = True
        return *result, free_events

    def run_free_ten(self) -> list[SimEvent]:
        """执行被挂起的加急招募（界面上的「加急招募 ×10」）。"""
        if not self.free_ten_pending:
            return []
        self.free_ten_pending = False
        self.free_ten_used = True
        return self._run_free_ten()

    def draw_one(self) -> DrawResult:
        rarity, name, note, _ = self.step()
        return DrawResult(self.total, name, rarity, note)

    def run(self, n: int, *, auto_free: bool = True) -> Iterator[SimEvent]:
        """逐抽产出：抽卡结果 → 累计赠送 → 加急招募（若触发）。"""
        for _ in range(n):
            before = self.gift_step
            rarity, name, note, free_events = self.step(auto_free=auto_free)
            yield DrawResult(self.total, name, rarity, note)
            if self.gift_step > before:
                yield GiftEvent(self.total, UP)
            yield from free_events

    # ---------- 快路径：只推进状态，不建事件 ----------
    def _tick(self) -> None:
        """等价于 step() 但丢弃事件与名称，供蒙特卡洛批量跑用。"""
        self.total += 1
        self.small_pity += 1
        self.five_pity += 1
        if self.big_pity_active:
            self.up_pity += 1

        if self.big_pity_active and self.up_pity >= BIG_PITY_MAX:
            self.count_six += 1
            self.count_up += 1
            self.count_big_pity += 1
            self.small_pity = 0
            self._consume_big_pity("第120抽必出")
        elif self.small_pity >= SIX_HARD_PITY or (
                self.rng.randint(1, 1000) <= six_rate_permille(self.small_pity)):
            self.small_pity = 0
            self.count_six += 1
            # 只关心「是不是 UP」，用一次 random() 按权重判定，省掉角色名查表
            if self.rng.random() * SUM_SIX < WEIGHTS_SIX[0]:
                self.count_up += 1
                if self.big_pity_active:
                    self._consume_big_pity("提前出货")
        else:
            if self.five_pity >= FIVE_PITY_MAX or self.rng.randint(1, 100) <= FIVE_BASE_RATE:
                self.five_pity = 0
                self.count_five += 1
            else:
                self.count_four += 1

        if self.total >= GIFT_INTERVAL * self.gift_step:
            self.gift_step += 1
            self.count_gift += 1

        if self.total >= FREE_TEN_TRIGGER and not self.free_ten_used:
            self.free_ten_used = True
            self._run_free_ten_quiet()

    def _run_free_ten_quiet(self) -> None:
        """加急招募的无人值守版本：只更新计数，不产出事件。"""
        got_high = False
        for i in range(1, FREE_TEN_SIZE + 1):
            forced = i == FREE_TEN_SIZE and not got_high
            if forced:
                rarity = SIX if self.rng.randint(1, 1000) <= SIX_BASE_RATE else FIVE
            elif self.rng.randint(1, 1000) <= SIX_BASE_RATE:
                rarity = SIX
            elif self.rng.randint(1, 100) <= FIVE_BASE_RATE:
                rarity = FIVE
            else:
                rarity = FOUR
            if rarity in (SIX, FIVE):
                got_high = True
            if rarity == SIX:
                self.free_count_six += 1
                if self.rng.random() * SUM_SIX < WEIGHTS_SIX[0]:
                    self.free_count_up += 1
            elif rarity == FIVE:
                self.free_count_five += 1
            else:
                self.free_count_four += 1

    def draws_to_target(self, target: int = TARGET_UP_UNITS, max_draws: int = 20000) -> int:
        """跑快路径直到集齐 target 个 UP 单位，返回消耗抽数。超上限返回 max_draws。"""
        while self.up_units < target:
            self._tick()
            if self.total >= max_draws:
                break
        return self.total

    # ---------- 统计（一行） ----------
    def stats_line(self) -> str:
        if self.total == 0:
            return "尚无抽卡数据"
        avg_six = self.total / self.count_six if self.count_six else 0
        avg_up = self.total / self.up_units if self.up_units else 0
        return (f"总抽数:{self.total} | 六星:{self.count_six} | 五星:{self.count_five} | "
                f"四星:{self.count_four} | "
                f"加急招募:{int(self.free_ten_used)}次(六星{self.free_count_six}/UP{self.free_count_up}) | "
                f"已获得UP:{self.up_units}(含赠送{self.count_gift}/加急{self.free_count_up}) | "
                f"{self._big_pity_text()} | 第120抽强制:{self.count_big_pity}次 | "
                f"消耗:{self.total * GEM_PER_DRAW:,}货币 | "
                f"平均{avg_six:.1f}抽/六星 | 平均{avg_up:.1f}抽/UP")


# ========== 蒙特卡洛 · 矩阵版（NumPy，无逐抽 / 逐轮循环） ==========
def _draws_batch(rng: np.random.Generator, n: int, target: int) -> np.ndarray:
    """单批：一次矩阵运算跑完 n 轮「抽到满阶」，返回每轮消耗的抽数。

    和前两个池子不同，这里有「前 120 抽必得 UP」的**大保底**，它会把更新过程截断。
    但截断只发生在**第一次** UP 上，而且每次 UP 之后小保底都归零，所以整条路径可以
    拆成 target 段「到下一个 UP 为止」：

      · 第 1 段：自然情况下第一次 UP 落在第几抽，再用 120 抽截断（`min(nat, 120)`）；
      · 第 2..target 段：每段消耗 M 次六星（M ~ 几何分布，UP 率 50%），
        每段的抽数 = 这 M 个 iid 间隔之和 —— 用同一个 cumsum 按 M 累加即可；
      · 再加两个「按抽数位置」的额外来源：30 抽的白送十连、每 240 抽赠送 1 个；
      · 最后把「各段结束位置 / 赠送位置 / 30 抽」当候选点，取第一个
        「已到手 UP 数 ≥ target」的位置。

    `W1/W2` 只是性能上的截断（概率尾巴会被逐抽循环兜底），结果一定对得上。
    """
    w1, w2 = MC_FIRST_EVENTS, MC_GAP_POOL

    # ---- 第 1 段：第一次 UP，被 120 抽大保底截断 ----
    pos1 = np.cumsum(mc_matrix.sample_gaps(rng, (n, w1), SIX_GAP_LUT), axis=1)
    has, idx = mc_matrix.first_true(rng.random((n, w1)) < UP_SHARE_SIX)
    natural = np.where(has, mc_matrix.pick(pos1, idx), mc_matrix.UNRESOLVED)
    first = np.minimum(natural, BIG_PITY_MAX)

    # ---- 第 2..target 段：每段 M 次六星（几何分布），间隔从 iid 池里顺序取 ----
    m = mc_matrix.geometric_count(rng, (n, int(target) - 1), UP_SHARE_SIX)
    used = np.cumsum(m, axis=1)
    gaps = np.cumsum(mc_matrix.sample_gaps(rng, (n, w2), SIX_GAP_LUT), axis=1)
    cum_draws = np.where(used <= w2, mc_matrix.pick(gaps, used - 1), 0)
    costs = np.diff(np.concatenate([np.zeros((n, 1), np.int32), cum_draws], axis=1), axis=1)
    positions = np.concatenate([first[:, None], first[:, None] + np.cumsum(costs, axis=1)],
                               axis=1)                        # (n, target) 每个 UP 的抽数
    # ---- 30 抽白送十连：每抽 0.8% 六星、六星 50% 是 UP ----
    free_up = (rng.random((n, FREE_TEN_SIZE)) < SIX_BASE_RATE / 1000 * UP_SHARE_SIX).sum(axis=1)

    # ---- 候选点 1：每个 UP 到手的那一抽 ----
    k = np.arange(1, int(target) + 1)[None, :]
    units = k + positions // GIFT_INTERVAL + free_up[:, None] * (positions >= FREE_TEN_TRIGGER)
    hit, at = mc_matrix.first_true(units >= target)
    answer = np.where(hit, mc_matrix.pick(positions, at), mc_matrix.UNRESOLVED)

    # ---- 候选点 2：赠送节点（刚好在 240 / 480 / … 抽凑满） ----
    gifts = np.arange(1, MC_GIFT_LIMIT + 1) * GIFT_INTERVAL
    owned = (positions[:, None, :] <= gifts[None, :, None]).sum(axis=2)
    gift_units = owned + np.arange(1, MC_GIFT_LIMIT + 1)[None, :] \
        + free_up[:, None] * (gifts >= FREE_TEN_TRIGGER)[None, :]
    answer = np.minimum(answer, np.where(gift_units >= target, gifts, mc_matrix.UNRESOLVED).min(axis=1))

    # ---- 候选点 3：30 抽那一抽（白送十连直接凑满，几乎不会发生但要算） ----
    at_thirty = (positions <= FREE_TEN_TRIGGER).sum(axis=1) + free_up
    answer = np.where(at_thirty >= target, np.minimum(answer, FREE_TEN_TRIGGER), answer)
    return answer


def draws_to_target_matrix(runs: int, target: int = TARGET_UP_UNITS,
                           seed: int | None = None) -> np.ndarray:
    """跑 `runs` 轮「抽到满阶」，返回每轮消耗的抽数（大轮次自动分批，控制内存）。"""
    n = int(runs)
    if n <= 0:
        return np.empty(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    out = np.empty(n, dtype=np.int64)
    for start, stop in mc_matrix.in_chunks(n):
        out[start:stop] = _draws_batch(rng, stop - start, int(target))

    stray = np.flatnonzero(out >= mc_matrix.UNRESOLVED)
    if stray.size:                       # 概率尾巴：交回逐抽循环，保证精确
        rest = GachaCore()
        for i in stray:
            rest.reset()
            out[i] = rest.draws_to_target(target)
    return out


def simulate_summary(runs: int, target: int = TARGET_UP_UNITS,
                     seed: int | None = None) -> dict:
    """跑一轮蒙特卡洛并**在后端把统计做完**，只回一个精简 dict 给前端。

    含 runs / min / max / mean / median / p10·p25·p75·p90 /
    gems / per_up / tiers（分档计数）/ elapsed_ms。
    """
    t0 = time.perf_counter()
    draws = draws_to_target_matrix(runs, target, seed)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return mc_matrix.summarize(draws, target=target, gem_per_draw=GEM_PER_DRAW,
                            tiers=MC_TIERS, elapsed_ms=elapsed_ms)


def format_line(ev: SimEvent) -> str:
    """一条事件渲染成一行文本。"""
    if isinstance(ev, PhaseEvent):
        return ev.text
    if isinstance(ev, GiftEvent):
        return f"累计{ev.index}抽 | 额外赠送 | {ev.name} | 六星角色兑换券 ×1 |"
    label = {SIX: "恭喜出六星", FIVE: "恭喜出五星"}.get(ev.rarity)
    tail = f" | {label} |" if label else " |"
    note = f" [{ev.note}]" if ev.note else ""
    if ev.free_index:
        return f"〔加急 {ev.free_index}/{FREE_TEN_SIZE}〕{ev.name}{tail}{note}"
    return f"第{ev.index}抽 | {ev.name}{tail}{note}"

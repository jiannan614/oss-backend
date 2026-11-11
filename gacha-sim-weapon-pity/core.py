#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core.py —— 武器池抽卡内核（纯逻辑，无 IO）

规则（全部是常量，改规则只动本文件顶部）：

    最高稀有度   4%，恒定，无软保底
    其中 UP      25%（其余为其他最高稀有度武器）
    保底         40 抽内必出，出即重置、循环
    UP 保底      80 抽内必得 UP，一次性、不继承
    里程碑       10 / 26 / 42 / 58 / 74 期 → 随机最高稀有度箱（非 UP）
                 18 / 34 / 50 / 66 / 82 期 → 直接送 UP
                 （1 期 = 10 抽）
    满阶目标     6 把 UP（1 本体 + 5 次重复）
    次级概率     五星 15%，其余按四星处理

本池不满足「更新过程」前提（里程碑依赖本期已出过什么），所以矩阵版按抽推进，
而不是一次抽满间隔。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import numpy as np

import mc_matrix

# ---------------- 卡池配置（换版本只改这几行） ----------------
VERSION = "v1.2.0"
WPN_UP = "UP武器"
WPN_BANNER = "示例卡池"
WPN_PERIOD = "2026.11.5 – 未知"
OTHER_SIX_POOL = ["武器00", "武器01", "武器02", "武器03", "武器04", "武器05"]
OTHER_SIX = "其他六星武器"      # 池子外的兜底名

# 五星 / 四星武器池（名字 + 等权；要调权重就改 FIVE_WEIGHTS / FOUR_WEIGHTS）
FIVE_POOL = ["武器00", "武器01", "武器02", "武器03", "武器04", "武器05", "武器06", "武器07", "武器08", "武器09", "武器10", "武器11", "武器12", "武器13", "武器14", "武器15", "武器16", "武器17", "武器18", "武器19", "武器20"]
FOUR_POOL = ["武器00", "武器01", "武器02", "武器03", "武器04", "武器05", "武器06", "武器07", "武器08", "武器09"]
# 等权：每个 1；权重表单独留着，换版本只改这里
FIVE_WEIGHTS = [1] * len(FIVE_POOL)
FOUR_WEIGHTS = [1] * len(FOUR_POOL)

TARGET_UP_WEAPONS = 6          # 满阶：1 把本体 + 5 把潜能

# ---------------- 规则常量 ----------------
RATE_SIX = 0.04           # 恒定 4%，无软保底
SIX_PITY = 40             # 40 抽必出六星（循环）
UP_SHARE = 0.25           # 六星里 UP 占 25%
UP_PITY = 80              # 80 抽必出 UP（一次性）
RATE_FIVE = 0.15
DRAWS_PER_PERIOD = 10     # 1 期 = 10 抽

# 里程碑按「期」定义，内部换算成抽数
BOX_PERIODS = (10, 26, 42, 58, 74)          # 里程碑箱（非 UP）
UP_PERIODS = (18, 34, 50, 66, 82)           # 送 UP 武器
BOX_DRAWS = tuple(p * DRAWS_PER_PERIOD for p in BOX_PERIODS)
UP_DRAWS = tuple(p * DRAWS_PER_PERIOD for p in UP_PERIODS)

SIX, FIVE_STAR, FOUR_STAR = 6, 5, 4

RARITY_LABEL = {SIX: "6 星", FIVE_STAR: "5 星", FOUR_STAR: "4 星"}

# 蒙特卡洛的分档口径（按实测均值 ~328 抽定的阈值），前端只负责上色
MC_TIERS = (
    ("欧", None, 279),
    ("正常", 280, 380),
    ("偏非", 381, None),
)
def six_rate(pity: int) -> float:
    """第 pity 抽（距上次六星）的六星率，与 WeaponCore.step 完全一致。"""
    return 1.0 if pity >= SIX_PITY else RATE_SIX


# 六星间隔分布（只由保底表决定），模块导入时算一次反查表
SIX_GAP_LUT = mc_matrix.build_gap_lut(
    [six_rate(p) for p in range(1, SIX_PITY + 1)]
)


@dataclass(frozen=True)
class WpnDrop:
    index: int
    name: str
    rarity: int
    is_up: bool = False
    source: str = "常规"      # 常规 / 里程碑箱 / 里程碑 UP
    note: str = ""


class WeaponCore:
    """武器池内核：零 IO，状态全在实例里。"""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng if rng is not None else random.Random()
        self.reset()

    def reset(self) -> None:
        self.total = 0
        self.six_pity = 0          # 距上次六星
        self.up_pity = 0           # 距上次 UP（仅 up_active 时增长）
        self.up_active = True      # UP 保底是否仍可触发（一次性）
        self.up_consumed_at = 0
        self.up_reason = ""
        self.count_six = 0
        self.count_five = 0
        self.count_four = 0
        self.count_up = 0          # 抽到的 UP
        self.count_box = 0         # 里程碑箱开出的六星
        self.count_gift_up = 0     # 里程碑送的 UP
        self.count_forced_up = 0   # 80 抽强制 UP 次数
        self._box_seen = 0
        self._up_seen = 0
        self._period = 0
        self._period_high = False   # 本期（10 抽）内是否已出过 5★+

    # ---------------- 派生 ----------------
    @property
    def up_units(self) -> int:
        """已获得的 UP 武器总数：抽到 + 强制 + 里程碑送。"""
        return self.count_up + self.count_gift_up

    @property
    def six_total(self) -> int:
        """六星总数（含里程碑箱开出的，算掉落但不算抽卡保底）。"""
        return self.count_six + self.count_box

    def _consume_up_pity(self, reason: str) -> None:
        self.up_consumed_at = self.total
        self.up_reason = reason
        self.up_active = False

    def up_text(self) -> str:
        if self.up_active:
            return f"UP 保底 {self.up_pity}/{UP_PITY}"
        return f"UP 保底已用@{self.up_consumed_at}抽({self.up_reason})"

    # ---------------- 抽 ----------------
    def _pick_other_six(self) -> str:
        return self.rng.choice(OTHER_SIX_POOL) if OTHER_SIX_POOL else OTHER_SIX

    def _pick_five(self) -> str:
        return self.rng.choices(FIVE_POOL, weights=FIVE_WEIGHTS, k=1)[0]

    def _pick_four(self) -> str:
        return self.rng.choices(FOUR_POOL, weights=FOUR_WEIGHTS, k=1)[0]

    def step(self) -> list[WpnDrop]:
        """抽一次，返回这一抽产生的所有掉落（常规一抽 + 可能的里程碑奖励）。"""
        self.total += 1
        self.six_pity += 1
        if self.up_active:
            self.up_pity += 1

        drops: list[WpnDrop] = []

        # 期（每 10 抽为 1 期）边界：重置「本期是否已出 5★+」
        period = (self.total - 1) // DRAWS_PER_PERIOD
        if period != self._period:
            self._period = period
            self._period_high = False
        period_last = self.total % DRAWS_PER_PERIOD == 0

        # ① UP 保底：80 抽内必出 UP
        if self.up_active and self.up_pity >= UP_PITY:
            self.count_six += 1
            self.count_up += 1
            self.count_forced_up += 1
            self.six_pity = 0
            self._consume_up_pity("第80抽必出")
            drop = WpnDrop(self.total, WPN_UP, SIX, True, "常规", "UP 保底")

        # ② 六星（40 抽保底，循环）
        elif self.six_pity >= SIX_PITY or self.rng.random() < RATE_SIX:
            self.six_pity = 0
            self.count_six += 1
            if self.rng.random() < UP_SHARE:
                self.count_up += 1
                if self.up_active:
                    self._consume_up_pity("提前出货")
                drop = WpnDrop(self.total, WPN_UP, SIX, True, "常规")
            else:
                drop = WpnDrop(self.total, self._pick_other_six(), SIX)

        # ③ 十连保底：本期最后一抽还没出过 5★+，当场补一件
        elif period_last and not self._period_high:
            if self.rng.random() < RATE_SIX:
                self.six_pity = 0
                self.count_six += 1
                if self.rng.random() < UP_SHARE:
                    self.count_up += 1
                    if self.up_active:
                        self._consume_up_pity("提前出货")
                    drop = WpnDrop(self.total, WPN_UP, SIX, True, "常规", "十连保底")
                else:
                    drop = WpnDrop(self.total, self._pick_other_six(), SIX, False, "常规", "十连保底")
            else:
                self.count_five += 1
                drop = WpnDrop(self.total, self._pick_five(), FIVE_STAR, False, "常规", "十连保底")

        # ④ 五星 / 四星
        elif self.rng.random() < RATE_FIVE:
            self.count_five += 1
            drop = WpnDrop(self.total, self._pick_five(), FIVE_STAR)
        else:
            self.count_four += 1
            drop = WpnDrop(self.total, self._pick_four(), FOUR_STAR)

        if drop.rarity >= FIVE_STAR:
            self._period_high = True
        drops.append(drop)

        # ⑤ 里程碑
        while self._box_seen < len(BOX_DRAWS) and self.total >= BOX_DRAWS[self._box_seen]:
            self._box_seen += 1
            self.count_box += 1
            drops.append(WpnDrop(self.total, self._pick_other_six(), SIX, False, "里程碑箱",
                                 f"第 {BOX_DRAWS[self._box_seen - 1]} 抽里程碑箱"))
        while self._up_seen < len(UP_DRAWS) and self.total >= UP_DRAWS[self._up_seen]:
            self._up_seen += 1
            self.count_gift_up += 1
            drops.append(WpnDrop(self.total, WPN_UP, SIX, True, "里程碑 UP",
                                 f"第 {UP_DRAWS[self._up_seen - 1]} 抽赠送"))

        return drops

    def draw(self, n: int) -> list[WpnDrop]:
        out: list[WpnDrop] = []
        for _ in range(n):
            out.extend(self.step())
        return out

    def draws_to_target(self, target: int = TARGET_UP_WEAPONS,
                        max_draws: int = 30000) -> int:
        """一直申领到集齐 target 把 UP 武器，返回消耗抽数。只统计，不产出事件。"""
        while self.up_units < target:
            self.step()
            if self.total >= max_draws:
                break
        return self.total

def stats_line(self) -> str:
        if self.total == 0:
            return "尚无抽卡数据"
        avg_six = self.total / self.count_six if self.count_six else 0
        return (f"总抽数:{self.total} | 六星:{self.count_six}(+里程碑箱{self.count_box}) | "
                f"五星:{self.count_five} | 四星:{self.count_four} | "
                f"UP:{self.up_units}(抽卡{self.count_up}/赠送{self.count_gift_up}) | "
                f"{self.up_text()} | "
                f"平均{avg_six:.1f}抽/六星")


# ========== 蒙特卡洛 · 矩阵版（NumPy，无逐轮循环） ==========
def draws_to_target_matrix(runs: int, target: int = TARGET_UP_WEAPONS,
                           seed: int | None = None, max_draws: int = 1200,
                           compact_at: float = 0.15,
                           stats: dict | None = None) -> np.ndarray:
    """跑 `runs` 轮「抽到满阶」，返回每轮消耗的抽数（抽数维推进、runs 维向量化）。

    为什么这个池子不像另外三个那样「一次矩阵算完」：
      六星 40 抽保底、UP 80 抽保底都是「距上次多少抽」的计数，可以按 iid 间隔一口气抽完；
      但**十连保底是按抽数位置触发的**（每期第 10 抽还没出 5★+ 就当场补一件），
      它依赖「本期已出过什么」，更新过程的前提不成立。
      所以这里改成按抽推进：每一步只做一次 runs 维的布尔运算，
      已集齐的轮次不再参与判定，剩下的继续推进 —— 一起算完，没有逐轮循环。

    每轮最多需要 ~820 抽（10 万轮实测），`max_draws` 之后还没凑齐的极端轮次
    用逐抽循环单独补算，保证和旧实现完全一致。

    **性能优化（2026-11-10）**：以前只要有人完成，当抽立刻把 5 个状态数组整体拷贝一次；
    而第 200~350 抽之间几乎每抽都有人完成，于是每抽都在搬运十几万长度的数组 —— 这段
    开销跟随机推进本身无关。现在改成「完成的比例超过 `compact_at` 才真正收缩」，
    中间那段把已完成轮次冻结在原地（不再累计、不再判完成）。
    规则、概率、判定顺序完全没动，20 万轮从约 1220 ms 降到约 990 ms。
    回滚方式见 downloads/wpn_opt/README.md。

    ``compact_at``：已完成轮次占当前数组的比例超过它才真正剔除；设 0 就退化成旧行为。
    ``stats``：可选，传一个 dict 进来会填过程计数器（forced_up / ten_makeup / ten_six_up），
    只用于和逐抽基准对指纹，不影响计算。
    """
    n = int(runs)
    if n <= 0:
        return np.empty(0, dtype=np.int64)

    rng = np.random.default_rng(seed)
    sp = np.zeros(n, np.int32)          # 距上次六星
    up_p = np.zeros(n, np.int32)        # 距上次 UP（仅在 up_active 时增长）
    up_act = np.ones(n, bool)           # UP 保底是否仍可触发（一次性）
    period_high = np.zeros(n, bool)     # 本期（10 抽）内是否已出 5★+
    ups = np.zeros(n, np.int32)         # 抽到的 UP 数
    idx = np.arange(n)                  # 还在抽的轮次下标
    draws = np.full(n, -1, np.int64)
    alive = np.ones(n, bool)            # False = 已完成并记过数，冻结在原地
    gift_ptr = 0
    dead = 0

    step = 0
    while step < max_draws and idx.size:
        step += 1
        m = idx.size
        if step % DRAWS_PER_PERIOD == 1:
            period_high[:] = False
        sp += 1
        up_p += up_act

        # 三列随机数一次抽完：六星 / UP / 五星
        u = rng.random((m, 3), dtype=np.float32)
        a = up_act & (up_p >= UP_PITY)                      # ① 80 抽 UP 保底
        six = a | (sp >= SIX_PITY) | (u[:, 0] < RATE_SIX)   # ② 六星（含 40 抽保底）
        up = a | (six & (u[:, 1] < UP_SHARE))               # 六星里 UP 占 25%
        high = six | (u[:, 2] < RATE_FIVE)                  # 出现了 5★ 或 6★

        ten_six = None
        if step % DRAWS_PER_PERIOD == 0:                    # ③ 十连保底
            low = (~six) & (~period_high)                   # 本期没出过 5★+
            if low.any():
                ten_six = low & (u[:, 1] < RATE_SIX)        # 补的那件也可能是六星
                up |= ten_six & (u[:, 2] < UP_SHARE)
                if stats is not None:
                    stats["ten_makeup"] += int((low & alive).sum())
                    stats["ten_six_up"] += int(
                        (ten_six & (u[:, 2] < UP_SHARE) & alive).sum())

        if stats is not None:
            stats["forced_up"] += int((a & alive).sum())

        sp[six if ten_six is None else (six | ten_six)] = 0
        up_act &= ~up
        period_high |= high
        ups += up

        while gift_ptr < len(UP_DRAWS) and step >= UP_DRAWS[gift_ptr]:
            gift_ptr += 1
        # 已完成（冻结）的轮次不再重复记账
        done = (ups + gift_ptr >= target) & alive
        if done.any():
            draws[idx[done]] = step
            alive &= ~done
            dead += int(done.sum())

            if dead / m >= compact_at:                     # 死得够多才真正搬运一次
                keep = alive
                idx = idx[keep]
                sp, up_p, up_act, period_high, ups, alive = (
                    sp[keep], up_p[keep], up_act[keep],
                    period_high[keep], ups[keep], alive[keep])
                dead = 0
                if not idx.size:
                    break

    if (draws < 0).any():                          # 极端尾巴：逐抽循环补算
        rest = WeaponCore()
        for i in np.flatnonzero(draws < 0):
            rest.reset()
            draws[i] = rest.draws_to_target(target)
    return draws


def simulate_summary(runs: int, target: int = TARGET_UP_WEAPONS,
                     seed: int | None = None) -> dict:
    """跑一轮蒙特卡洛并**在后端把统计做完**，只回一个精简 dict 给前端。"""
    t0 = time.perf_counter()
    draws = draws_to_target_matrix(runs, target, seed)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return mc_matrix.summarize(draws, target=target, tiers=MC_TIERS,
                            elapsed_ms=elapsed_ms)

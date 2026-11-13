#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽数预算的蒙特卡洛内核（纯标准库，无第三方依赖）

回答一个很具体的问题：**手上这些资源，抽完大概能拿到什么、折成多少副产物？**

流程只有三步：

    资源（抽卡货币 / 高级货币 / 单抽券 / 十连券）
      → 折算成抽数
      → 按卡池机制跑 N 组（每组只抽这么多抽就停，不追求满阶）
      → 每组的稀有度分布 → 副产物账本 → 汇总成「均值 / 最欧 / 最非」

卡池机制（依据公开概率公示整理，规则本身是机制描述）：
    · 最高稀有度（6★）基础 0.8%，第 66 抽起每抽 +5%，第 80 抽必出
    · 次高（5★）8%，连续 10 抽没有 5★ 则强制一个
    · 前 120 抽内必得当期 UP，同卡池一次，消耗即失效
    · 每累计 240 抽赠 1 个 UP 凭据（不占抽数）
    · 累计 30 抽触发一次免费十连（独立计算，不写任何计数器）

副产物口径（常量在下面，改这里就够）：
    装备配额    4★ 20 / 5★ 200 / 6★ 2000，每次获得即得
    保底配额    同个角色第 2 份起：5★ 10 / 6★ 50（25 配额 = 1 抽）
    通用配额    满阶（6 份）之后多出来的每一份：4★ 5 / 5★ 20
    高级配额    6★ 满阶之后多出来的每一份：10
    —— 四个名字都是中性占位，换成你自己产品的叫法即可。

玩家档位（影响保底配额与通用配额）：
    new       所有角色从 0 份起算：第 1 份是新获得，第 2 份才给保底配额
    veteran   4★ / 5★ 图鉴已满阶、6★ 不含本体：抽到的每一份都算重复

统计口径：
    运气指数    长期平均间隔 ÷ 这组实际平均间隔 × 100（100 = 平均水平，越高越欧）
    档位        <50 欧 / 50~67 正常 / ≥67 非酋（按每组「抽数 ÷ 6★ 数」判）

命令行：
    python budget_core.py --orundum 150000
    python budget_core.py --orundum 150000 --profile veteran --pity 20
    python budget_core.py --json --orundum 150000
    python budget_core.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time

VERSION = "v1.0.0"

# ========== 唯一出处之一：资源折算 ==========
# (参数名, 显示名, 折算成多少抽卡货币)
RESOURCES = (
    ("orundum", "抽卡货币", 1),
    ("originite", "高级货币", 75),
    ("tickets", "单抽券", 500),
    ("ten", "十连券", 5000),
)
LABELS = {key: label for key, label, _ in RESOURCES}
RATES = {key: rate for key, _, rate in RESOURCES}
KEYS = tuple(RATES)
GEM_PER_DRAW = 500

# ========== 唯一出处之二：卡池机制 ==========
SIX, FIVE, FOUR = 6, 5, 4
RARITY_LABEL = {SIX: "6★", FIVE: "5★", FOUR: "4★"}

UP = "六星01"
SIX_POOL = ["六星01", "六星02", "六星03", "六星04", "六星05", "六星06", "六星07", "六星08"]
# 首位是 UP 的权重，其余各 1 → UP 占 6★ 的 7/14 = 50%
WEIGHTS_SIX = (7, 1, 1, 1, 1, 1, 1, 1)
# 把权重摊成索引表：模拟时一次 randrange(14) 就够，比 bisect 快
IDX_SIX = tuple(i for i, w in enumerate(WEIGHTS_SIX) for _ in range(w))
FIVE_POOL = [f"五星{i:02d}" for i in range(1, 10)]
FOUR_POOL = [f"四星{i:02d}" for i in range(1, 6)]

SIX_BASE_PERMILLE = 8          # 6★ 基础 0.8%
SIX_SOFT_PITY = 65             # 第 66 抽起（pity > 65）概率上涨
SIX_RATE_STEP = 50             # 每抽 +5%
SIX_HARD_PITY = 80             # 硬保底
BIG_PITY_MAX = 120             # 前 120 抽内必得 UP（一次性）
FIVE_BASE_PERCENT = 8          # 5★ 8%
FIVE_PITY_MAX = 10             # 连续 10 抽无 5★ 则强制
GIFT_INTERVAL = 240            # 每累计 240 抽赠 1 个 UP 凭据
FREE_TEN_TRIGGER = 30          # 累计 30 抽触发一次免费十连
FREE_TEN_SIZE = 10

# ========== 唯一出处之三：副产物 ==========
EQUIP_QUOTA = {FOUR: 20, FIVE: 200, SIX: 2000}      # 每次获得
PITY_QUOTA = {FIVE: 10, SIX: 50}                   # 同个角色第 2 份起
UNIVERSAL_QUOTA = {FOUR: 5, FIVE: 20}              # 满阶溢出
PREMIUM_QUOTA = {SIX: 10}                          # 6★ 满阶溢出
FULL_DUP = 6                                       # 满阶 = 6 份（1 本体 + 5 凭据）
# 保底配额：5★ / 6★ 都是「第 2 份起，每多一份给一次」，只是倍率不同（10 / 50）。
# 想改成「每个角色只给第二次那一次」，把下面打开。
GUARANTEE_ONCE = False

# 每 240 抽赠送的 UP 凭据（全部记在 UP 角色名下）：
#   计入「6★ 总数」—— 它确实是多了一个 6★；
#   计入「高级配额」的溢出份数 —— 它会把 UP 一路顶过满阶并溢出；
#   不计入「装备 / 保底 / 通用配额」—— 送的，不产生这三样收益；
#   不计入「平均每个 6★」的除数 —— 那个指标衡量的是「抽出来的」。
# 所以份数有两套：`c6`（抽到的，算保底）与 `c6_total`（抽到 + 赠送，算溢出）。
GIFT_COUNTS_IN_TOTAL = True
PITY_PER_DRAW = 25                                 # 25 保底配额 = 1 抽（页面显示可折抽数）

# 玩家档位：抽卡前「已经有几份」。
# `owned` 管 4★ / 5★（整档一个数）；`six_owned` 管 6★，必须按角色给 ——
# 8 位 6★ 里谁满阶、谁没有，直接决定高级配额从第几份开始算。
PROFILES = {
    "new": {
        "label": "新玩家",
        "owned": {FOUR: 0, FIVE: 0, SIX: 0},
        "six_owned": (0,) * 8,
        "desc": "全部从 0 份起算：第 1 份算新获得，第 2 份才给保底配额，"
                "满阶要抽到 6 份才开始溢出。",
    },
    "veteran": {
        "label": "老玩家",
        "owned": {FOUR: FULL_DUP, FIVE: FULL_DUP, SIX: 0},
        "six_owned": (0,) * 8,
        "desc": "4★ / 5★ 图鉴已满阶、6★ 不含本体（也按 0 份起算）："
                "4★ / 5★ 抽到的每一份都算重复，通用配额明显更多。",
    },
    "whale": {
        "label": "满图鉴氪佬",
        "owned": {FOUR: FULL_DUP, FIVE: FULL_DUP, SIX: 0},
        "six_owned": (0,) + (FULL_DUP,) * 7,   # 首位是 UP：当期新角色仍从 0 份起算
        "desc": "4★ / 5★ 已满阶，7 位非 UP 6★ 也已满阶、抽到任何一份都直接溢出；"
                "当期 UP 仍按 0 份起算。高级配额会明显高于其它档位。",
    },
}
PROFILE_KEYS = tuple(PROFILES)

# 档位：按每组的「抽数 ÷ 6★ 数」判
# 不设「≥80」档：单条间隔有 80 抽硬保底兜着，一条间隔不可能超过 80；
# 只有「一组里 6★ 太少、最后一抽停在半个周期里」才会把比值拉高，那种一并算非酋。
TIERS = (
    ("欧", None, 50),
    ("正常", 50, 67),
    ("非酋", 67, None),
)
# 配色：欧金 / 正常绿 / 非酋红
TIER_COLORS = {
    "欧": ("#E5A83B", "#8A6318"),
    "正常": ("#3FA96B", "#24663E"),
    "非酋": ("#E2554E", "#8E2E28"),
}

RUNS = 5000               # 固定组数：不开放给调用方，避免同一次查询给出不同数字
MAX_DRAWS = 3000          # 单组抽数上限（防手滑传一个天文数字把进程跑死）


# --------------------------------------------------------------------------- #
# 第一段：资源折算
# --------------------------------------------------------------------------- #
def convert(counts: dict[str, int]) -> dict:
    """纯函数：零 IO。``counts`` 的键是 RESOURCES 里的四个名字。"""
    items = []
    total = 0
    for key, label, rate in RESOURCES:
        count = max(0, int(counts.get(key, 0) or 0))
        subtotal = count * rate
        total += subtotal
        items.append({"key": key, "label": label, "count": count,
                      "rate": rate, "subtotal": subtotal})

    return {
        "items": items,
        "total": total,                        # 折算后的抽卡货币合计
        "gem_per_draw": GEM_PER_DRAW,
        "draws": total / GEM_PER_DRAW,         # 现有抽数（可以是小数）
        "full_draws": total // GEM_PER_DRAW,   # 能完整抽的次数（模拟用它）
        "remainder": total % GEM_PER_DRAW,     # 抽完之后剩下的货币
        "unit_rates": dict(RATES),
    }


# --------------------------------------------------------------------------- #
# 第二段：单组模拟
# --------------------------------------------------------------------------- #
def six_rate_permille(pity: int) -> int:
    """第 pity 抽（距上次 6★ 的抽数，已 +1）的 6★ 率，千分比整数。

    用整数是为了和逐抽判定 ``randrange(1000) < rate`` 逐位一致 ——
    浮点会在 458 / 457.9999 这种边界上给出不同结果。
    """
    if pity >= SIX_HARD_PITY:
        return 1000
    return SIX_BASE_PERMILLE + max(0, pity - SIX_SOFT_PITY) * SIX_RATE_STEP


_THEORY_CACHE: float | None = None


def theoretical_gap() -> float:
    """按保底表精算长期平均间隔：E[gap] = Σ P(还没出)，不抽样。约 53.9 抽。"""
    global _THEORY_CACHE
    if _THEORY_CACHE is None:
        survival, expected = 1.0, 0.0
        for pity in range(1, SIX_HARD_PITY + 1):
            expected += survival
            survival *= 1.0 - six_rate_permille(pity) / 1000.0
        _THEORY_CACHE = expected
    return _THEORY_CACHE


def luck_index(avg_gap: float) -> float:
    """运气指数：理论间隔 ÷ 实际间隔 × 100。100 = 平均水平，越高越欧。"""
    if not math.isfinite(avg_gap) or avg_gap <= 0:
        return 0.0
    return theoretical_gap() / avg_gap * 100.0


def _marginal(copies: int, owned: int, threshold: int, rate: int) -> int:
    """「超过 threshold 份之后，每一份给 rate」的边际收益。

    起手就超过阈值时（老玩家 4★ 起手 6 份、阈值 1），这一批抽到的每一份都算数。
    """
    before = max(0, owned - threshold)
    after = max(0, owned + copies - threshold)
    return (after - before) * rate


def _pity_quota(copies: int, owned: int, rate: int) -> int:
    """保底配额：同个角色第 2 份起给，5★ / 6★ 共用这一条，只是倍率不同（10 / 50）。

    ``GUARANTEE_ONCE=False``（默认）→ 第 2、3、4… 份**每份**都给；
    打开后 → 每个角色只认「第二次」那一份。
    """
    if copies <= 0:
        return 0
    if GUARANTEE_ONCE:
        return rate if owned < 2 else 0
    return _marginal(copies, owned, 1, rate)


def _ledger(draws: int, six: int, five: int, four: int,
            c4: list[int], c5: list[int], c6: list[int], owned: dict[int, int],
            gift: int = 0, six_owned: tuple[int, ...] | None = None) -> dict:
    """一组结果 → 副产物账本。纯函数，便于单测。

    参数 ``six`` 是**抽出来的** 6★ 数，``gift`` 是 240 抽赠送的凭据个数（全记在 UP 名下）：
    - 赠送的**进高级配额**：让 UP 一路涨过满阶并溢出；
    - 赠送的**不进**装备 / 保底 / 通用配额；
    - 赠送的**不进** ``avg_gap`` 的除数。

    所以 6★ 份数有两套：``c6``（抽到的 → 算保底）与 ``c6_total``（抽到 + 赠送 → 算溢出）。
    ``six_owned`` 是 8 位 6★ 各自的起手份数（氪佬档满 7 位），缺省按整档一个值。
    """
    if six_owned is None:
        six_owned = tuple(owned[SIX] for _ in c6)

    equip = (EQUIP_QUOTA[FOUR] * four + EQUIP_QUOTA[FIVE] * five
             + EQUIP_QUOTA[SIX] * six)

    # 保底：只认抽到的份数（赠送的那份不产生保底配额）
    pity = sum(_pity_quota(n, owned[FIVE], PITY_QUOTA[FIVE]) for n in c5)
    pity += sum(_pity_quota(n, own, PITY_QUOTA[SIX]) for n, own in zip(c6, six_owned))

    universal = sum(_marginal(n, owned[FOUR], FULL_DUP, UNIVERSAL_QUOTA[FOUR]) for n in c4)
    universal += sum(_marginal(n, owned[FIVE], FULL_DUP, UNIVERSAL_QUOTA[FIVE]) for n in c5)

    # 溢出（高级配额）：份数要算上赠送 —— 赠品也会把 UP 顶过满阶
    c6_total = list(c6)
    c6_total[0] += gift
    premium = sum(_marginal(n, own, FULL_DUP, PREMIUM_QUOTA[SIX])
                  for n, own in zip(c6_total, six_owned))

    avg_gap = (draws / six) if six else math.inf
    return {
        "draws": draws,
        "six": six + gift,          # 展示口径：抽到的 + 赠送的
        "six_drawn": six,           # 「抽出来的」—— 平均间隔、装备配额都用它
        "gift_six": gift,
        "up_six": c6_total[0],      # 当期 UP 的总份数（含赠送）
        "up_six_drawn": c6[0],      # 当期 UP 只算抽到的
        "five": five,
        "four": four,
        "avg_gap": avg_gap,
        "index": luck_index(avg_gap),
        "equip": equip,
        "pity": pity,
        "pity_draws": pity / PITY_PER_DRAW,
        "universal": universal,
        "premium": premium,
    }


def simulate_once(draws: int, rng: random.Random, *, start_pity: int = 0,
                  profile: str = "new") -> dict:
    """跑一组：只抽 ``draws`` 抽就停，返回这一组的账本。

    刻意不复用面向界面的逐抽对象模型：5000 组 × 几百抽会产生几百万个临时对象，
    光 GC 就吃掉几秒。这里只留「稀有度 → 计数 → 份数」，规则与界面一致。
    """
    owned = PROFILES[profile]["owned"]

    total = 0
    small_pity = max(0, min(int(start_pity), SIX_HARD_PITY - 1))
    five_pity = 0
    up_pity = 0
    big_active = True
    gift_step = 1
    free_used = False

    six = five = four = 0
    gift = 0                    # 240 抽赠送的凭据：只计 6★ 总数，不进份数、不产生副产物
    c6 = [0] * len(SIX_POOL)
    c5 = [0] * len(FIVE_POOL)
    c4 = [0] * len(FOUR_POOL)

    # 局部化，省掉循环里的属性查找（这个循环一次要跑几百万遍）
    randrange = rng.randrange
    rate_of = six_rate_permille
    idx6 = IDX_SIX
    n6, n5, n4 = len(SIX_POOL), len(FIVE_POOL), len(FOUR_POOL)

    while total < draws:
        total += 1
        small_pity += 1
        five_pity += 1
        if big_active:
            up_pity += 1

        if big_active and up_pity >= BIG_PITY_MAX:
            # ① 大保底：第 120 抽强制 UP 6★，小保底清零、大保底失效
            six += 1
            small_pity = 0
            big_active = False
            c6[0] += 1
        elif randrange(1000) < rate_of(small_pity):
            # ② 常规 6★（含软 / 硬保底）
            small_pity = 0
            six += 1
            idx = idx6[randrange(len(idx6))]
            c6[idx] += 1
            if idx == 0 and big_active:
                # 120 抽内提前出了 UP → 大保底消耗掉、立即失效
                big_active = False
        else:
            # ③ 5★（10 抽保底）或 4★
            if five_pity >= FIVE_PITY_MAX or randrange(100) < FIVE_BASE_PERCENT:
                five_pity = 0
                five += 1
                c5[randrange(n5)] += 1
            else:
                four += 1
                c4[randrange(n4)] += 1

        # ④ 每累计 240 抽赠 1 个 UP 凭据：
        #    计入「6★ 总数」，但不进 c6 份数矩阵 → 不给装备配额、不推进满阶、
        #    不产生保底 / 通用 / 高级配额；也不进 avg_gap 的除数。
        if total >= GIFT_INTERVAL * gift_step:
            gift_step += 1
            gift += 1

        # ⑤ 累计 30 抽触发一次免费十连：完全独立，6★ 率恒 0.8%、十连内必出 5★+
        if total >= FREE_TEN_TRIGGER and not free_used:
            free_used = True
            got_high = False
            for i in range(1, FREE_TEN_SIZE + 1):
                forced = i == FREE_TEN_SIZE and not got_high
                if forced:
                    if randrange(1000) < SIX_BASE_PERMILLE:
                        six += 1
                        c6[idx6[randrange(len(idx6))]] += 1
                    else:
                        five += 1
                        c5[randrange(n5)] += 1
                    got_high = True
                elif randrange(1000) < SIX_BASE_PERMILLE:
                    six += 1
                    c6[idx6[randrange(len(idx6))]] += 1
                    got_high = True
                elif randrange(100) < FIVE_BASE_PERCENT:
                    five += 1
                    c5[randrange(n5)] += 1
                    got_high = True
                else:
                    four += 1
                    c4[randrange(n4)] += 1

    return _ledger(draws, six, five, four, c4, c5, c6, owned, gift,
                   PROFILES[profile]["six_owned"])


# --------------------------------------------------------------------------- #
# 第三段：N 组 → 均值 / 边界 / 档位
# --------------------------------------------------------------------------- #
def _tier_of(avg_gap: float) -> str:
    for label, lo, hi in TIERS:
        if (lo is None or avg_gap >= lo) and (hi is None or avg_gap < hi):
            return label
    return TIERS[-1][0]


def _percentile(values: list[float], q: float) -> float:
    """线性插值分位，q 取 0~1。"""
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _mean(values: list[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    return (sum(finite) / len(finite)) if finite else math.inf


def _matrix_engine():
    """延迟加载矩阵引擎：NumPy 不在时返回 None，调用方自动回退到逐抽版。"""
    try:
        import mc_matrix
        return mc_matrix
    except Exception:           # noqa: BLE001 —— 缺 NumPy / 缺文件都算「不可用」
        return None


def simulate(draws: int, runs: int = RUNS, *, profile: str = "new",
             start_pity: int = 0, seed: int | None = None,
             engine: str = "auto") -> dict:
    """跑 ``runs`` 组，每组固定 ``draws`` 抽，返回统计结果（纯函数）。

    ``engine``：auto（有 NumPy 就走矩阵，没有退回逐抽）/ matrix（强制）/ loop（强制逐抽）。
    """
    runs = max(1, int(runs))
    draws = max(0, min(int(draws), MAX_DRAWS))
    started = time.perf_counter()

    matrix = _matrix_engine() if engine in ("auto", "matrix") else None
    if matrix is not None:
        ledgers = matrix.simulate_batch(draws, runs, profile=profile,
                                        start_pity=start_pity, seed=seed)
        engine_used = matrix.ENGINE_NAME
    else:
        rng = random.Random(seed)
        ledgers = [simulate_once(draws, rng, start_pity=start_pity, profile=profile)
                   for _ in range(runs)]
        engine_used = "python-loop"

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    best = max(ledgers, key=lambda r: (r["six"], r["five"], r["four"]))
    worst = min(ledgers, key=lambda r: (r["six"], r["five"], r["four"]))

    # 两个 6★ 口径：`six` 含 240 抽赠送的凭据（展示用），
    # `six_drawn` 只算抽出来的 —— 平均间隔、档位、分位全都用后者。
    mean_six = sum(r["six"] for r in ledgers) / runs
    mean_drawn = sum(r["six_drawn"] for r in ledgers) / runs
    mean_gap = (draws / mean_drawn) if mean_drawn else math.inf
    mean = {
        "draws": draws,
        "six": mean_six,
        "six_drawn": mean_drawn,
        "gift_six": sum(r["gift_six"] for r in ledgers) / runs,
        "up_six": sum(r["up_six"] for r in ledgers) / runs,
        "up_six_drawn": sum(r["up_six_drawn"] for r in ledgers) / runs,
        "five": sum(r["five"] for r in ledgers) / runs,
        "four": sum(r["four"] for r in ledgers) / runs,
        "avg_gap": mean_gap,
        "index": luck_index(mean_gap),
        "equip": sum(r["equip"] for r in ledgers) / runs,
        "pity": sum(r["pity"] for r in ledgers) / runs,
        "pity_draws": sum(r["pity_draws"] for r in ledgers) / runs,
        "universal": sum(r["universal"] for r in ledgers) / runs,
        "premium": sum(r["premium"] for r in ledgers) / runs,
    }

    buckets = {label: 0 for label, _, _ in TIERS}
    for row in ledgers:
        buckets[_tier_of(row["avg_gap"])] += 1
    tiers = [
        {"label": label, "lo": lo, "hi": hi, "count": buckets[label],
         "pct": buckets[label] / runs * 100}
        for label, lo, hi in TIERS
    ]
    if draws <= 0:
        # 一抽都没有时「每个 6★ 多少抽」是无穷大，全落进最后一档没有信息量
        for row in tiers:
            row["count"] = 0
            row["pct"] = 0.0

    gaps = [r["avg_gap"] for r in ledgers]
    sixes = [float(r["six"]) for r in ledgers]
    # 「一个 6★ 都没出」按抽出来的判：只有赠送凭据不算出过 6★
    zero_six = sum(1 for r in ledgers if r["six_drawn"] == 0)

    return {
        "draws": draws,
        "runs": runs,
        "profile": profile,
        "profile_label": PROFILES[profile]["label"],
        "start_pity": start_pity,
        "max": best,
        "mean": mean,
        "min": worst,
        "tiers": tiers,
        "percentiles": {
            "six": {f"p{int(q * 100)}": _percentile(sixes, q)
                    for q in (0.10, 0.25, 0.50, 0.75, 0.90)},
            "avg_gap": {f"p{int(q * 100)}": _percentile(gaps, q)
                        for q in (0.10, 0.25, 0.50, 0.75, 0.90)},
        },
        "zero_six_runs": zero_six,
        "zero_six_pct": zero_six / runs * 100,
        "mean_gap": _mean(gaps),
        "theory_gap": round(theoretical_gap(), 2),
        "engine_kind": engine_used,
        "elapsed_ms": elapsed_ms,
    }


# --------------------------------------------------------------------------- #
# 第四段：视图模型（前端照着排版即可）
# --------------------------------------------------------------------------- #
def json_safe(obj):
    """把 inf / nan 换成 None —— 它们在 JSON 里是非法的。

    ``json.dumps`` 默认会把 ``float("inf")`` 写成裸的 ``Infinity``，这不是合法 JSON，
    浏览器 ``JSON.parse`` 直接抛错，前端只能拿到空 body，显示成「HTTP 202」这种
    和真实原因毫无关系的提示（202 是「已受理」的正常状态码）。

    什么时候会出现 inf：一批抽数太少、某一组一个 6★ 都没出，``avg_gap = 抽数 / 0``。
    表格里已经格式化成「∞」字符串了，所以这里丢掉不会少信息。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(value) for value in obj]
    return obj


def _num(value: float, digits: int = 1) -> str:
    if not math.isfinite(value):
        return "—"
    return f"{value:,.{digits}f}"


def _gap(value: float) -> str:
    if math.isnan(value):
        return "—"
    return "∞" if not math.isfinite(value) else f"{value:.1f}"


def _compare_table(summary: dict) -> dict:
    """一张表看三段：行是指标，列是 Max / 平均 / Min。

    单元格只放数字，单位挪进表注 —— 否则「261.5 个」这种会把窄屏的列顶宽。
    """
    best, mean, worst = summary["max"], summary["mean"], summary["min"]

    def trio(fmt):
        return [fmt(best, 0), fmt(mean, 1), fmt(worst, 0)]

    rows = [
        ["总抽数"] + trio(lambda r, d: f"{r['draws']:,}"),
        ["6★"] + trio(lambda r, d: _num(r["six"], d)),
        ["其中 UP"] + trio(lambda r, d: _num(r["up_six"], d)),
        ["5★"] + trio(lambda r, d: _num(r["five"], d)),
        ["4★"] + trio(lambda r, d: _num(r["four"], d)),
        ["每个 6★"] + trio(lambda r, d: _gap(r["avg_gap"])),
        ["运气指数"] + trio(lambda r, d: _num(r["index"], d)),
        ["装备配额"] + trio(lambda r, d: _num(r["equip"], 0)),
        ["保底配额"] + trio(
            lambda r, d: f"{_num(r['pity'], 0)}（可折 {_num(r['pity_draws'], 0)} 抽）"),
        ["通用配额"] + trio(lambda r, d: _num(r["universal"], 0)),
        ["高级配额"] + trio(lambda r, d: _num(r["premium"], 0)),
    ]
    return {
        "title": "收益明细",
        "columns": ["指标", "Max", "平均", "Min"],
        "rows": rows,
        "collapsed": True,
        "note": "单位：抽数 = 抽，稀有度 = 个，每个 6★ = 抽，配额 = 点数"
                "（括号内为 ÷25 折算的抽数）。"
                "每 240 抽赠送的凭据计入「6★」与「其中 UP」两行，它会把 UP 一路顶过满阶并"
                "产生高级配额；但它是赠品，不给装备 / 保底 / 通用配额，也不计「每个 6★」。"
                "「每个 6★」按实际抽到的算，所以会和上一行对不上 —— 那是赠品。"
                "Max / Min 是单批极值，平均是全部批次的均值；运气指数 100 = 长期平均水平。",
    }


def _six_hint(mean: dict) -> str:
    """KPI「平均 6★」下面那行小字。

    有赠送凭据时，光写「每 54.5 抽一个」会和上面的 13 个对不上账，
    所以把「抽到的」也说出来 —— 用户一眼能看出差的那几个是送的。
    """
    if not math.isfinite(mean["avg_gap"]):
        return "这批抽数里没出 6★"
    gap = f"每 {mean['avg_gap']:.1f} 抽一个"
    drawn = mean.get("six_drawn", mean["six"])
    if mean.get("gift_six", 0) >= 0.5:
        return f"抽到 {_num(drawn, 1)} 个 · {gap}"
    return gap


def build_view(summary: dict, conv: dict) -> dict:
    """统计结果 → 前端视图（kpis / donut / tables / notes）。"""
    mean, best, worst = summary["mean"], summary["max"], summary["min"]
    max_six, min_six = int(best["six"]), int(worst["six"])
    floor_six = summary["draws"] // SIX_HARD_PITY      # 80 抽保底：这批抽数至少能出几个

    kpis = [
        {"label": "折算抽数", "value": f"{conv['full_draws']:,} 抽",
         "hint": f"{conv['total']:,} 抽卡货币 ÷ {conv['gem_per_draw']}"},
        {"label": "平均 6★", "value": f"{_num(mean['six'], 1)} 个",
         "hint": _six_hint(mean)},
        {"label": "运气指数", "value": _num(mean["index"], 0),
         "hint": "100 = 长期平均"},
        {"label": "保底可折", "value": f"{_num(mean['pity_draws'], 0)} 抽",
         "hint": f"保底配额 {_num(mean['pity'], 0)} ÷ {PITY_PER_DRAW}"},
    ]

    donut = {
        "title": "档位概率",
        "total": summary["runs"],
        "unit": "组",
        "legend": "side",      # 三档用侧边图例，不拉引线
        "caption": f"档位概率 · {summary['runs']:,} 组",
        "items": [
            {"label": row["label"], "count": row["count"],
             "pulls": row["count"], "percent": round(row["pct"], 1),
             "value": f"{row['count']:,} 组 · {row['pct']:.1f}%",
             "color": TIER_COLORS[row["label"]][0],
             "shade": TIER_COLORS[row["label"]][1]}
            for row in summary["tiers"] if row["count"] > 0
        ],
    }

    six_phrase = f"约 {_num(mean['six'], 1)} 个 6★"
    if mean.get("gift_six", 0) >= 0.5:
        # 有赠送时点一下：不然「13 个 6★」和「每 54.5 抽一个」看起来对不上
        six_phrase += f"（含 240 抽赠送的 {_num(mean['gift_six'], 0)} 个）"
    verdict = (
        f"理想情况会落在平均值附近：{six_phrase}、"
        f"{_num(mean['five'], 1)} 个 5★、{_num(mean['four'], 1)} 个 4★，"
        f"装备配额约 {_num(mean['equip'], 0)}，保底配额约 {_num(mean['pity'], 0)}"
        f"（可折 {_num(mean['pity_draws'], 0)} 抽）。"
        f"受随机性影响，个别批次会浮动到最欧 {max_six} 个 6★、最非 {min_six} 个 6★ 附近；"
        f"按 {SIX_HARD_PITY} 抽保底算，{summary['draws']:,} 抽至少能出 {floor_six} 个 6★。"
        "以上只是概率上的平均推算，不代表真实抽卡结果。"
    )
    notes = [verdict]
    if summary["draws"] <= 0:
        notes = ["这批资源折算不足 1 抽，没有可模拟的抽数。"]

    return {"kpis": kpis, "donut": donut, "tables": [_compare_table(summary)], "notes": notes}


def run(counts: dict[str, int], *, profile: str = "new", pity: int = 0,
        runs: int = RUNS, seed: int | None = None, engine: str = "auto") -> dict:
    """折算 + 模拟 + 视图，一次给全。worker 与命令行都走这里，口径不会分叉。"""
    profile = str(profile or "new").strip().lower()
    if profile not in PROFILES:
        raise ValueError(f"不认识的玩家档位：{profile}（可选 {' / '.join(PROFILE_KEYS)}）")
    if not 0 <= int(pity) < SIX_HARD_PITY:
        raise ValueError(f"垫抽要在 0~{SIX_HARD_PITY - 1} 之间：{pity}")

    conv = convert(counts)
    if conv["full_draws"] > MAX_DRAWS:
        raise ValueError(
            f"这批资源能抽 {conv['full_draws']:,} 抽，超过单次上限 {MAX_DRAWS} 抽"
        )

    summary = simulate(conv["full_draws"], runs, profile=profile,
                       start_pity=int(pity), seed=seed, engine=engine)
    view = build_view(summary, conv)
    view.update({
        "convert": conv,
        "summary": summary,
        "byproduct_rates": {
            "equip": {str(k): v for k, v in EQUIP_QUOTA.items()},
            "pity": {str(k): v for k, v in PITY_QUOTA.items()},
            "universal": {str(k): v for k, v in UNIVERSAL_QUOTA.items()},
            "premium": {str(k): v for k, v in PREMIUM_QUOTA.items()},
            "full_dup": FULL_DUP,
            "pity_per_draw": PITY_PER_DRAW,
        },
        "profiles": {k: {"label": v["label"], "desc": v["desc"]} for k, v in PROFILES.items()},
        "engine": f"budget_core {VERSION} · {summary['engine_kind']}",
        "elapsed_ms": summary["elapsed_ms"],
    })
    return view


# --------------------------------------------------------------------------- #
# 第五段：命令行
# --------------------------------------------------------------------------- #
def _envelope_ok(data: dict) -> str:
    return json.dumps({"ok": True, "data": json_safe(data),
                       "meta": {"adapter": "budget", "mode": "sync"}},
                      ensure_ascii=False, indent=2)


def _envelope_fail(message: str) -> str:
    return json.dumps({"ok": False, "error": "invalid_params", "message": message},
                      ensure_ascii=False, indent=2)


def _report(conv: dict, view: dict) -> str:
    lines = ["=== 资源折算 ==="]
    for item in conv["items"]:
        lines.append(f"  {item['label']}  {item['count']:,} × {item['rate']:,} = {item['subtotal']:,}")
    lines.append("-" * 44)
    lines.append(f"  合计：{conv['total']:,} 抽卡货币 = {conv['draws']:,.2f} 抽"
                 f"（可完整抽 {conv['full_draws']:,} 抽）")
    lines.append("")
    table = view["tables"][0]
    lines.append(f"=== {table['title']}（{view['summary']['runs']:,} 组 × "
                 f"{view['summary']['draws']:,} 抽）===")
    head = table["columns"]
    lines.append("  " + "  ".join(f"{h:>10}" for h in head))
    for row in table["rows"]:
        lines.append("  " + "  ".join(f"{c:>10}" for c in row))
    lines.append("")
    for row in view["summary"]["tiers"]:
        lines.append(f"  {row['label']:4} {row['count']:>6} 组  {row['pct']:5.1f}%")
    lines.append("")
    for note in view["notes"]:
        lines.append(f"· {note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="budget_core.py", description="抽数预算的蒙特卡洛内核")
    for key, label, _ in RESOURCES:
        parser.add_argument(f"--{key}", type=int, default=0, help=f"{label}数量")
    parser.add_argument("--profile", default="new", choices=list(PROFILE_KEYS), help="玩家档位")
    parser.add_argument("--pity", type=int, default=0, help="当前垫抽数（0~79）")
    parser.add_argument("--runs", type=int, default=RUNS, help=f"模拟组数（默认 {RUNS}）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    parser.add_argument("--engine", default="auto", choices=("auto", "matrix", "loop"),
                        help="计算引擎：auto 有 NumPy 走矩阵 / matrix 强制 / loop 逐抽")
    parser.add_argument("--json", action="store_true", help="输出 {ok, data} 信封")
    parser.add_argument("--selftest", action="store_true", help="跑内置自检")
    args = parser.parse_args(argv)

    if args.selftest:
        import selftest
        return 0 if selftest.main() else 1

    counts = {key: max(0, getattr(args, key)) for key, _, _ in RESOURCES}
    try:
        view = run(counts, profile=args.profile, pity=args.pity,
                   runs=args.runs, seed=args.seed, engine=args.engine)
    except ValueError as exc:
        print(_envelope_fail(str(exc)) if args.json else f"✗ {exc}")
        return 2

    print(_envelope_ok(view) if args.json else _report(view["convert"], view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

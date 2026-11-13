"""NumPy 矩阵引擎（与 ``budget_core.simulate_once`` 同一套规则，换成整批向量化）

逐抽版是「一组 × N 抽」的双重循环；这里把全部组摊平成 NumPy 数组，循环只走抽数这一维，
每一抽同时推进所有组 —— 5000 组 × 300 抽从 150 万次 Python 迭代压到 300 次数组运算。

规则与 ``simulate_once`` 逐条对齐（含 240 抽赠凭据、30 抽免费十连这些抽数触发的副作用）。
唯一不同的是随机数流：``default_rng``（PCG64）一次取一批，逐抽版用 ``random.Random``
一次取一个 —— 所以两种引擎**逐组结果不可能相同**，只能比分布，见 ``python budget_core.py --selftest``。

NumPy 不在时 ``budget_core`` 会自动回退到逐抽版，这个模块不被导入。

状态向量（长度 = 组数）：
    pity       距上次 6★ 的抽数（0~80）
    five_pity  距上次 5★ 的抽数（0~10）
    up_pity    大保底计数（只在 big_active 时涨）
    big_active 120 抽大保底是否还有效
"""

from __future__ import annotations

import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from budget_core import (  # noqa: E402  （规则常量只有那一份，不在这里重复定义）
    BIG_PITY_MAX,
    EQUIP_QUOTA,
    FIVE,
    FIVE_BASE_PERCENT,
    FIVE_PITY_MAX,
    FIVE_POOL,
    FOUR,
    FOUR_POOL,
    FREE_TEN_SIZE,
    FREE_TEN_TRIGGER,
    FULL_DUP,
    GIFT_INTERVAL,
    GUARANTEE_ONCE,
    PITY_PER_DRAW,
    PITY_QUOTA,
    PREMIUM_QUOTA,
    PROFILES,
    SIX,
    SIX_BASE_PERMILLE,
    SIX_HARD_PITY,
    SIX_POOL,
    UNIVERSAL_QUOTA,
    luck_index,
    six_rate_permille,
)

_RATE_TABLE = np.array(
    [six_rate_permille(p) for p in range(SIX_HARD_PITY + 1)], dtype=np.int32
)

N6, N5, N4 = len(SIX_POOL), len(FIVE_POOL), len(FOUR_POOL)
# 权重摊成索引表：首位 UP 占 7/14，其余各 1/14
_IDX6 = np.array([i for i, w in enumerate([7, 1, 1, 1, 1, 1, 1, 1]) for _ in range(w)],
                 dtype=np.int8)

ENGINE_NAME = "numpy-matrix"


def _marginal_vec(counts: np.ndarray, owned, threshold: int, rate: int) -> np.ndarray:
    """``_marginal()`` 的向量版：形状 (组数, 角色数) → 同形状的配额。

    ``owned`` 可以是标量（4★/5★ 整档一个值），也可以是长 8 的数组
    （6★ 每位角色各自的起手份数 —— 氪佬档有 7 位已满）。
    """
    owned = np.asarray(owned, dtype=np.int32)
    before = np.maximum(0, owned - threshold)
    after = np.maximum(0, counts + owned - threshold)
    return (after - before) * rate


def _flush(acc: list, width: int, total: int) -> np.ndarray | None:
    """把攒下的「组 × 角色」索引对落成份数矩阵。"""
    if not acc:
        return None
    keys = np.concatenate(acc) if len(acc) > 1 else acc[0]
    return np.bincount(keys, minlength=total * width).reshape(total, width).astype(np.int32)


def simulate_batch(draws: int, runs: int, *, profile: str = "new",
                   start_pity: int = 0, seed: int | None = None) -> list[dict]:
    """跑 ``runs`` 组、每组 ``draws`` 抽，返回与 ``_ledger()`` 同构的账本列表。"""
    draws = max(0, int(draws))
    runs = max(1, int(runs))
    owned = PROFILES[profile]["owned"]
    rng = np.random.default_rng(seed)

    pity = np.full(runs, max(0, min(int(start_pity), SIX_HARD_PITY - 1)), dtype=np.int16)
    five_pity = np.zeros(runs, dtype=np.int16)
    up_pity = np.zeros(runs, dtype=np.int16)
    big_active = np.ones(runs, dtype=bool)
    free_used = False

    # 每 64 抽清一次缓冲：3000 抽 × 5000 组会有上千万条 4★ 事件，
    # 一直攒着 concat 会有几百 MB 瞬时峰值，分批落账内存是常数级。
    ev6: list[np.ndarray] = []
    ev5: list[np.ndarray] = []
    ev4: list[np.ndarray] = []
    cnt6 = np.zeros((runs, N6), dtype=np.int32)
    cnt5 = np.zeros((runs, N5), dtype=np.int32)
    cnt4 = np.zeros((runs, N4), dtype=np.int32)

    def commit() -> None:
        nonlocal cnt6, cnt5, cnt4
        block = _flush(ev6, N6, runs)
        if block is not None:
            cnt6 = cnt6 + block
        ev6.clear()
        block = _flush(ev5, N5, runs)
        if block is not None:
            cnt5 = cnt5 + block
        ev5.clear()
        block = _flush(ev4, N4, runs)
        if block is not None:
            cnt4 = cnt4 + block
        ev4.clear()

    for total in range(1, draws + 1):
        # ① 状态推进（大保底计数只在大保底还有效时涨）
        pity += 1
        five_pity += 1
        if big_active.any():
            up_pity[big_active] += 1

        # ② 6★ 判定：先看大保底，再看软 / 硬保底
        forced = big_active & (up_pity >= BIG_PITY_MAX)
        roll6 = rng.integers(0, 1000, size=runs, dtype=np.int16)
        rate = _RATE_TABLE[np.minimum(pity, SIX_HARD_PITY)]
        hit6 = forced | (roll6 < rate)
        got6 = np.flatnonzero(hit6)

        if got6.size:
            chars = _IDX6[rng.integers(0, len(_IDX6), size=got6.size)]
            chars = np.where(forced[got6], 0, chars)     # 大保底那抽必是 UP
            ev6.append(got6.astype(np.int32) * N6 + chars.astype(np.int32))
            pity[got6] = 0
            up_hit = got6[chars == 0]
            if up_hit.size:
                big_active[up_hit] = False               # 出了 UP 就把大保底用掉

        # ③ 非 6★：5★（10 抽保底）或 4★
        miss = np.flatnonzero(~hit6)
        if miss.size:
            roll5 = rng.integers(0, 100, size=miss.size, dtype=np.int16)
            got5 = (five_pity[miss] >= FIVE_PITY_MAX) | (roll5 < FIVE_BASE_PERCENT)
            take5 = miss[got5]
            take4 = miss[~got5]
            if take5.size:
                chars5 = rng.integers(0, N5, size=take5.size)
                ev5.append(take5.astype(np.int32) * N5 + chars5.astype(np.int32))
                five_pity[take5] = 0
            if take4.size:
                chars4 = rng.integers(0, N4, size=take4.size)
                ev4.append(take4.astype(np.int32) * N4 + chars4.astype(np.int32))

        # ④ 每累计 240 抽赠 1 个 UP 凭据：
        #    进「6★ 总数」，但**不进 cnt6** —— 它是送的，不产生任何配额、也不推进满阶。
        #    个数按 draws // 240 在最后一次性补（每组同抽数，是常数）。
        #    逐抽版对应 `gift += 1`，两边不要写歪。

        # ⑤ 累计 30 抽触发一次免费十连（完全独立于上面的大小保底）
        if total >= FREE_TEN_TRIGGER and not free_used:
            free_used = True
            high = np.zeros(runs, dtype=bool)            # 这十抽里是否已出 5★+
            for i in range(1, FREE_TEN_SIZE + 1):
                roll = rng.integers(0, 1000, size=runs, dtype=np.int16)
                is6 = roll < SIX_BASE_PERMILLE
                # 最后一抽且还没出过 5★+ → 兜底必出 5★（除非这一抽本身就是 6★）
                must = (i == FREE_TEN_SIZE) & ~high & ~is6
                roll5 = rng.integers(0, 100, size=runs, dtype=np.int16)
                is5 = ~is6 & (must | (roll5 < FIVE_BASE_PERCENT))
                is4 = ~is6 & ~is5

                g6 = np.flatnonzero(is6)
                if g6.size:
                    chars = _IDX6[rng.integers(0, len(_IDX6), size=g6.size)]
                    ev6.append(g6.astype(np.int32) * N6 + chars.astype(np.int32))
                g5 = np.flatnonzero(is5)
                if g5.size:
                    chars = rng.integers(0, N5, size=g5.size)
                    ev5.append(g5.astype(np.int32) * N5 + chars.astype(np.int32))
                g4 = np.flatnonzero(is4)
                if g4.size:
                    chars = rng.integers(0, N4, size=g4.size)
                    ev4.append(g4.astype(np.int32) * N4 + chars.astype(np.int32))
                high |= is6 | is5

        if total % 64 == 0:
            commit()

    commit()

    # ⑥ 份数矩阵 → 账本（整块向量化，与 ``_ledger()`` 同一套口径）
    # cnt6 里只有「抽出来的」6★（赠礼在 ④ 里就没进来），所以：
    #   total6_drawn / cnt6      → 装备配额、保底配额、平均间隔、档位
    #   cnt6_total（加赠送）      → 高级配额（赠品也会把 UP 顶过满阶）
    #   total6（抽到的 + 赠送）   → 页面上那个「6★」数字
    gifts = draws // GIFT_INTERVAL
    total6_drawn = cnt6.sum(axis=1).astype(np.int32)
    total6 = total6_drawn + gifts
    total5 = cnt5.sum(axis=1).astype(np.int32)
    total4 = cnt4.sum(axis=1).astype(np.int32)

    # 赠送的凭据全记在 UP（第 0 列）名下：只影响溢出，不碰保底
    cnt6_total = cnt6.copy()
    cnt6_total[:, 0] += gifts
    six_owned = np.asarray(PROFILES[profile]["six_owned"], dtype=np.int32)

    # 装备配额只认抽出来的：赠送的凭据不给 2000
    equip = (EQUIP_QUOTA[SIX] * total6_drawn + EQUIP_QUOTA[FIVE] * total5
             + EQUIP_QUOTA[FOUR] * total4)

    # 保底配额：5★ / 6★ 都是「第 2 份起每份给」，倍率 10 / 50；
    # 只认抽到的份数（赠送的那份不产生保底）。
    if GUARANTEE_ONCE:
        pity_quota = ((owned[FIVE] < 2) * (cnt5 > 0) * PITY_QUOTA[FIVE]).sum(axis=1)
        pity_quota = pity_quota + ((six_owned < 2) * (cnt6 > 0) * PITY_QUOTA[SIX]).sum(axis=1)
    else:
        pity_quota = _marginal_vec(cnt5, owned[FIVE], 1, PITY_QUOTA[FIVE]).sum(axis=1)
        pity_quota = pity_quota + _marginal_vec(cnt6, six_owned, 1, PITY_QUOTA[SIX]).sum(axis=1)

    universal = _marginal_vec(cnt4, owned[FOUR], FULL_DUP, UNIVERSAL_QUOTA[FOUR]).sum(axis=1)
    universal = universal + _marginal_vec(cnt5, owned[FIVE], FULL_DUP, UNIVERSAL_QUOTA[FIVE]).sum(axis=1)
    # 溢出用「抽到 + 赠送」的份数，起手份数按角色各算（氪佬 7 位已满）
    premium = _marginal_vec(cnt6_total, six_owned, FULL_DUP, PREMIUM_QUOTA[SIX]).sum(axis=1)

    # 平均间隔按「抽出来的」算：拿赠品当除数会把间隔压低（显得更欧）
    with np.errstate(divide="ignore", invalid="ignore"):
        avg_gap = np.where(total6_drawn > 0, draws / np.maximum(total6_drawn, 1), np.inf)

    ledgers: list[dict] = []
    for i in range(runs):
        gap = float(avg_gap[i])
        ledgers.append({
            "draws": draws,
            "six": int(total6[i]),
            "six_drawn": int(total6_drawn[i]),
            "gift_six": int(gifts),
            "up_six": int(cnt6_total[i, 0]),
            "up_six_drawn": int(cnt6[i, 0]),
            "five": int(total5[i]),
            "four": int(total4[i]),
            "avg_gap": gap,
            "index": luck_index(gap),
            "equip": int(equip[i]),
            "pity": int(pity_quota[i]),
            "pity_draws": float(pity_quota[i]) / PITY_PER_DRAW,
            "universal": int(universal[i]),
            "premium": int(premium[i]),
        })
    return ledgers

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抽数规划（纯标准库，无需安装任何依赖）

功能
----
1. 资源折算：抽卡货币 / 高级货币 / 单抽券 / 十连券 / 兑换凭证阶段兑换 → 总抽数。
2. 爆率与保底：按单抽概率分布，计算 N 抽内至少出一个 6 星的期望值与概率分位。
3. 攒抽预测：给定每日稳定收入（日常、周常、每周副本、月卡、签到、次级凭证商店、
   维护补偿、兑换凭证兑换），预测攒到目标抽数所需天数。
4. 活动预估：读取本地日期，算出距下一个大活动（周年 5/1、半周年 11/1、春节）
   还有多少天、期间能攒多少抽，并给出限定池建议（够不够 300 井、
   能拿几个角色、拿下当期限定的概率）。
5. 抽卡模拟：蒙特卡洛模拟，估算拿到至少 1 个（或 k 个）6 星的概率。
6. 交互菜单（while 循环，输入 0 退出）+ 命令行参数两种用法。

抽卡规则（依据公开概率公示与 公开资料 抽取规则，可在 GachaConfig 中调整）
--------------------------------------------------------------------
* 6 星基础概率 2%，5 星 8%，4 星 50%，3 星 40%。
* 6 星保底：连续 50 抽未出 6 星后，第 51 抽起每抽 +2%（4% / 6% / 8% …）。
* 硬保底：累计到 100% 时必出，即最坏第 99 抽必出。
* 限定池：当期限定与陪跑各占 6 星的 35%（合计 70%，即“UP 率 70%”）。
* 限定池井：每抽获得 1 张兑换点数，300 张可兑换当期限定；
  自 2024-05-01 起，累计 300 抽额外赠送一份当期限定角色简历（限一次）。

用法示例
--------
  python planner_core.py                              # 交互菜单（输入 0 退出）
  python planner_core.py count --orundum 18000 --originite 12 --tickets 5 --yellow 258
  python planner_core.py rate --target 300 --monthly-card
  python planner_core.py event --orundum 12000 --monthly-card
  python planner_core.py sim --pulls 200 --trials 200000 --want 2
"""

from __future__ import annotations

import argparse
import datetime as _dt
import math
import random
import sys
import unicodedata

# ----------------------------- 基础常量 -----------------------------

ORUNDUM_PER_PULL = 600          # 1 抽 = 600 抽卡货币
ORIGINITE_TO_ORUNDUM = 180      # 1 高级货币 = 180 抽卡货币

# 收入口径按“每月 4 周”计（1800 货币/周 × 4 = 7200 货币/月）：
# 每周副本作战：每周上限 1800 货币，一个月按 4 周计 → 7200 / 30 = 240 货币/天
# 取 240 作为“老玩家稳定满奖励”的默认值（每月 4 周 × 1800 = 7200 货币）。
DEFAULT_ORUNDUM_PER_RUN = 240
# 每周任务：500 货币/周，一个月按 4 周计 → 2000 / 30 ≈ 66.67 货币/天
DEFAULT_WEEKLY_TASK = 500 * 4 / 30
# 每日任务：100 货币/天
DEFAULT_DAILY_TASK = 100
# 月卡每天额外 200 货币
MONTHLY_CARD_ORUNDUM = 200
# 月卡（每月抽取组合包）的购买口径：30 元一张 → 立得 6 高级货币，之后 30 天每天 200 货币。
# 「有月卡」勾上后只当**本月这张**还在生效；要一路有月卡，就得每个月再买一张，
# 所以「次月 ~ 目标月（含目标月）」的张数就是这段时间还要花的钱。见 monthly_card_plan()。
MONTHLY_CARD_PRICE = 30            # 元 / 张
MONTHLY_CARD_ORIGINITE = 6         # 每张立得高级货币
MONTHLY_CARD_DAYS = 30             # 一张覆盖 30 天

# 每月签到累计奖励（按 25 天算）：1 张单抽券
DEFAULT_SIGNIN_TICKETS = 1
# 次级凭证商店（资质凭证区）每月刷新一次，合计 **4 抽**：
#   抽卡货币 6×100 = 600 货币（= 1 抽）
#   单抽券 × 3（= 3 抽）
# 注意 DEFAULT_GREEN_TICKETS 是**券的张数**（3 张），不是「抽数」——
# 整店折合 4 抽（验证：3 个月 = 1,800 货币 + 9 券 = 3 + 9 = 12 抽）。
DEFAULT_GREEN_TICKETS = 3
DEFAULT_GREEN_ORUNDUM = 600
# 维护补偿（合计 800 货币/月）：
#   维护补偿 200 货币 × 4 次（每月 7/15/22/30 号，保守取 4 次）
#   「更新补偿 5 高级货币」不再摊进月收入 —— 它绑定在每个大版本开服那天（见 daily_ledger）
DEFAULT_MAINTENANCE_ORUNDUM = 800
DEFAULT_MAINTENANCE_ORIGINITE = 0

# 兑换凭证（高级凭证）阶段兑换：10 / 18 / 40 / 70 / 120，合计 258 兑换凭证 = 38 抽
# 数据来源：公开资料 采购中心/凭证交易所 · 高级凭证区兑换阶梯表
#   第 1 档 1 抽 / 第 2 档 2 抽 / 第 3 档 5 抽 / 第 4 档 1 张十连券 / 第 5 档 2 张十连券
# 注意：每档每月只能换一次，换满 258 票（38 抽）后要等次月刷新才能再换。
YELLOW_EXCHANGE_TIERS: tuple[tuple[int, int], ...] = (
    (10, 1),    # 10 票 → 1 抽
    (18, 2),    # 18 票 → 2 抽
    (40, 5),    # 40 票 → 5 抽
    (70, 10),   # 70 票 → 1 张十连券（10 抽）
    (120, 20),  # 120 票 → 2 张十连券（20 抽）
)
YELLOW_FULL_COST = sum(c for c, _ in YELLOW_EXCHANGE_TIERS)      # 258
YELLOW_FULL_PULLS = sum(p for _, p in YELLOW_EXCHANGE_TIERS)     # 38
YELLOW_MONTHLY_LIMIT = YELLOW_FULL_COST                          # 每月最多消耗 258 票

# 兑换凭证折算的统一口径说明（网页 / JSON 接口 / 报表共用同一句）
YELLOW_VALUE_NOTE = (
    f"本程序只按「最性价比的大众习惯」计算：某个月把阶梯换满 "
    f"{YELLOW_FULL_COST} 票 = {YELLOW_FULL_PULLS} 抽才值得动手；"
    f"换不满整条阶梯的月份不往后延续，剩余票留着下个月凑满再用（票不会作废）。"
)

# 限定抽取机制
CONTRACT_COST_PER_SPARK = 300   # 300 张兑换点数可兑换当期限定 6 星
NEW_PITY_SPARK_THRESHOLD = 300  # 2024-05-01 起：累计 300 抽额外赠送当期限定
BANNER_RATE_UP_TOTAL = 0.70     # 限定池 6 星 UP 总概率（陪跑 + 限定）
BANNER_RATE_LIMITED = 0.35      # 其中当期限定角色占 6 星的 35%

# 兑换所需的兑换点数（井）
SPARK_COSTS: tuple[tuple[int, str], ...] = (
    (300, "当期限定 / 当期 UP 6★"),
    (200, "解限满 3 年的旧限定（春节系列 4 年）"),
    (75, "当期 UP 5★"),
)

# 大活动固定档期：周年 5/1、夏活 8/1、半周年 11/1（各持续 14 日）；春节为农历正月初一起 14 日
# 数据来源：公开资料 抽取规则 · 限定抽取分类（庆典 5/1、11/1；夏季 8 月；春节）
MAJOR_EVENT_SPECS: tuple[tuple[str, int, int, str], ...] = (
    ("周年庆", 5, 1, "限定抽取·庆典"),
    ("夏活", 8, 1, "限定抽取·夏季"),
    ("半周年庆", 11, 1, "限定抽取·庆典"),
)
EVENT_DURATION_DAYS = 14        # 大活动持续天数

# 大活动期间的额外赠送（每个大活动都可获得）
#   抽卡货币 8300（= 8300/600 ≈ 13.83 抽）
#   限时单抽券总计 24 抽 —— 注意：官方口径的“约 38 抽”是
#     24 张限时券 + 8300 抽卡货币（≈13.8 抽）合计，所以抽卡货币单独计入后，
#     限时券就只能是 24 抽，不能再用 38。
#   发放节奏：第 1 天 12 抽，其余 12 抽摊到第 2~14 天（每天 1 抽，最后一天补齐）
EVENT_REWARD_ORUNDUM = 8300
EVENT_REWARD_TIMED_PULLS_TOTAL = 24
# 逐日发放表：第 1 天 12 抽（活动开幕大放送），第 2~13 天每天 1 抽，第 14 天补齐。
# 12 + 12×1 + 0 = 24？ 不对——24 无法拆成「12 + 之后每天 1 张」（那是 25），
# 所以这里把余数放到最后一天，并保证合计严格等于 24。
EVENT_REWARD_TIMED_DAY1 = 12          # 活动第 1 天的限时券
EVENT_REWARD_TIMED_SCHEDULE: tuple[int, ...] = (
    (EVENT_REWARD_TIMED_DAY1,)
    + (1,) * (EVENT_DURATION_DAYS - 2)
    + (EVENT_REWARD_TIMED_PULLS_TOTAL - EVENT_REWARD_TIMED_DAY1
       - (EVENT_DURATION_DAYS - 2),)
)
assert sum(EVENT_REWARD_TIMED_SCHEDULE) == EVENT_REWARD_TIMED_PULLS_TOTAL, \
    f"发放表合计 {sum(EVENT_REWARD_TIMED_SCHEDULE)} ≠ {EVENT_REWARD_TIMED_PULLS_TOTAL}"
assert len(EVENT_REWARD_TIMED_SCHEDULE) == EVENT_DURATION_DAYS

# 抽卡副产物换算（满阶溢出兑换券兑换凭证）
#   4★ 出率 ~50%：满阶后每只 1 张兑换凭证
#   5★ 出率 ~8%：满阶后每只 5 张兑换凭证；重复（非首次）13 张
#   6★ 综合出率 ~2.89%：首次 1 张；重复 10 张
#   3★ 出率 ~40%：每只 5 张次级凭证（资质凭证）
BYPRODUCT_4STAR_YELLOW = 1
BYPRODUCT_4STAR_YELLOW_FIRST = 1
BYPRODUCT_5STAR_YELLOW_REPEAT = 5
BYPRODUCT_5STAR_YELLOW_MAXPOT = 13
BYPRODUCT_6STAR_YELLOW_FIRST = 1
BYPRODUCT_6STAR_YELLOW_REPEAT = 10
BYPRODUCT_3STAR_GREEN = 5
RARITY_3 = 0.40
RARITY_4 = 0.50
RARITY_5 = 0.08

# 玩家档位：只影响「副产物里兑换凭证」的估算，不影响抽数与概率。
#   6★：首次 1 张 / 重复 10 张     5★：首次 1 张 / 重复 5 张 / 满阶重复 13 张
# 按「抽到的这只有多少概率是重复」折成每只的平均兑换凭证：
#   新玩家 重复率 ≈ 0.2 → 6★ 0.2×10 + 0.8×1 = 2.8；5★ 按首次~满阶混合 ≈ 3.8
#   中玩家 重复率 ≈ 0.6 → 6★ 0.6×10 + 0.4×1 = 6.4；5★ 混合           ≈ 8.2
#   老玩家 基本全满阶   → 6★ 直接按重复 10 张（沿用旧口径，保留"首次那 1 个"不重复）
# 这三档是**估算**，不是查表：真实收益取决于你自己的图鉴。
PLAYER_PROFILES: tuple[dict, ...] = (
    {"key": "new", "label": "新玩家", "six_yellow": 2.8, "five_yellow": 3.8,
     "note": "图鉴空，绝大多数是首次获得"},
    {"key": "mid", "label": "中期玩家", "six_yellow": 6.4, "five_yellow": 8.2,
     "note": "部分满阶，首次与重复各占一半"},
    {"key": "veteran", "label": "资深玩家", "six_yellow": 10.0, "five_yellow": 13.0,
     "note": "基本全满阶，按重复获得计"},
)
PLAYER_PROFILE_MAP = {p["key"]: p for p in PLAYER_PROFILES}
DEFAULT_PLAYER_PROFILE = "veteran"


def profile_is_veteran(profile: str) -> bool:
    """玩家档位 → 新老玩家口径：**资深玩家**算老玩家，**新玩家 / 中期玩家**算新玩家。

    这个口径决定「每月浮动高级货币」拿多少（新玩家能打复刻拿首通高级货币，老玩家打过了
    拿不到），所以「攒多少天」和「攒到哪天有多少」必须走同一个判断。
    """
    return (profile or DEFAULT_PLAYER_PROFILE) == "veteran"

# ----------------------------- 月度浮动资源（估算） -----------------------------
# 口径：按“活动类型”粗估每月通过新剧情 / 新关卡首通能拿到的高级货币，
# 以及该月抽卡资源的量级。数据来自玩家社区经验值，只当**量级参考**。
#
# 两套口径（这是关键区别）：
#   veteran = 老玩家。官方复刻公告明确“活动关卡将继承玩家此前的活动关卡进度、
#             首次掉落进度”，且“无法重复获得的奖励按比例兑换为【情报凭证】”，
#             所以复刻关卡对老玩家**不再产出高级货币**，只能算新内容。
#   newbie  = 新玩家。复刻活动此前没打过，可以正常拿首通高级货币，因此比老玩家多。
#
# ⚠️ 单月浮动可能达到 ±15 高级货币甚至更多，本表仅用于给决策做参考。
# pulls_ref 是“该活动的抽卡资源量级”，只作参考展示：其中大活动的部分
# 已由 event_rewards() 精确计入，所以**不重复计入合计**。
MONTHLY_EVENT_ESTIMATES: tuple[dict, ...] = (
    {"months": (1,), "label": "春节限定 + SS",
     "veteran_originite": 30, "newbie_originite": 34,
     "pulls_ref": 77.5, "note": "生肖签到 + 限定赠送"},
    {"months": (2, 3), "label": "故事集 / SS",
     "veteran_originite": 22, "newbie_originite": 28,
     "pulls_ref": None, "note": "视活动而定"},
    {"months": (4,), "label": "周年庆（SS + 限定）",
     "veteran_originite": 30, "newbie_originite": 40,
     "pulls_ref": 90.0, "note": "周年赠送 + 签到"},
    {"months": (5,), "label": "周年庆后续 / SS",
     "veteran_originite": 30, "newbie_originite": 40,
     "pulls_ref": 30.0, "note": "承接周年庆档期的签到与赠礼"},
    {"months": (6,), "label": "复刻 / 联动",
     "veteran_originite": 28, "newbie_originite": 40,
     "pulls_ref": None, "note": "含轻量化复刻（老玩家无高级货币）"},
    {"months": (7,), "label": "新 SS / 联动作战",
     "veteran_originite": 28, "newbie_originite": 40,
     "pulls_ref": None, "note": "视活动而定"},
    {"months": (8,), "label": "夏活（SS + 限定）",
     "veteran_originite": 37, "newbie_originite": 37,
     "pulls_ref": 55.0, "note": "夏活赠送 + 签到"},
    {"months": (9,), "label": "主线章节 / 新 SS",
     "veteran_originite": 40, "newbie_originite": 40,
     "pulls_ref": None, "note": "主线首通高级货币"},
    {"months": (10,), "label": "主线后续 / 新 SS",
     "veteran_originite": 40, "newbie_originite": 40,
     "pulls_ref": None, "note": "主线首通高级货币"},
    {"months": (11,), "label": "感谢庆典（SS + 限定）",
     "veteran_originite": 40, "newbie_originite": 40,
     "pulls_ref": 90.0, "note": "感谢庆典赠送 + 签到"},
    {"months": (12,), "label": "SS / 复刻 / 跨年",
     "veteran_originite": 28, "newbie_originite": 40,
     "pulls_ref": None, "note": "跨年庆抽取同期"},
)
# 同一档覆盖多个月（如 2、3 月）时，各月各自按上面的值计，不做比例拆分，
# 这样某个中间月份不在计划窗口内时不会把其它月份的值算少。

# 浮动资源的不确定度说明（换算成抽数更好理解：约 15 高级货币 ≈ 4.5 抽）
MONTHLY_ESTIMATE_FUZZ = "±3 抽/月"

# ----------------------------- 新每周副本首通 -----------------------------
# 新每周副本地图首次通关（首通）可获得 2000 抽卡货币。
# ⚠️ 上线节奏**没有固定的官方规律**：历史上大致落在每年 4 月、7 月、10 月的
#    中旬，但具体日期年年都会浮动，有时还可能少一张或多一张——只能预测。
# 本程序采用**保守估计**：一年只按 3 张算（4/7/10 各一张），日期固定取中旬
# 的 15 日作为「预测节点」。计划窗口每跨过一个节点，就加 2000 抽卡货币。
# 如果当年多上线一张，这里会低估；少上线一张则会高估，所以只在结果里单独
# 列出这一项并明确标注为「无历史规律、保守估计」。
NEW_ANNIHILATION_ORUNDUM = 2000          # 每张新每周副本首通 = 2000 抽卡货币
NEW_ANNIHILATION_PREDICT_MONTHS = (4, 7, 10)   # 预测上线月份（中旬）
NEW_ANNIHILATION_PREDICT_DAY = 15              # 预测上线日（只做节点判断）

# ----------------------------- 春节签到 -----------------------------
# 春节那几天有个累计签到（差不多 2000 货币），拆成 4 个发放节点：
#   除夕 / 初一 / 初五 / 初八，每个节点 500 货币。
# 除夕 = 正月初一的前一天，其余按「初几」直接加天数（初五 = 正月初一 + 4 天）。
# 合计 500 × 4 = 2000 货币（≈ 3.33 抽）。
SPRING_SIGNIN_NODES: tuple[tuple[str, int], ...] = (
    ("除夕", -1),   # 春节前一天
    ("初一", 0),
    ("初五", 4),
    ("初八", 7),
)
SPRING_SIGNIN_ORUNDUM_EACH = 500         # 每个节点 500 货币
SPRING_SIGNIN_ORUNDUM_TOTAL = SPRING_SIGNIN_ORUNDUM_EACH * len(SPRING_SIGNIN_NODES)

# 抽卡概率 / 副产物里计入的限时券上限。
# 大活动送的限时券必然在当期卡池用掉，所以算进概率；但窗口跨了多个档期时
# （例如 96 张）那属于多个卡池，不能全记到这一次头上，统一按 24 封顶。
ACTIVITY_TIMED_PULLS_CAP = 24
# 首通高级货币属于「一次性」收益，不进每月固定收入，也不进浮动高级货币表
# （高级货币表只统计复刻差异），而是按窗口跨过的节点单独计入自由资源。
# 大版本前后常夹带小签到 / 维护补偿等活动（会送抽卡材料），无法精确预测，
# 这里给一个统一的乐观误差（用户口径：+2 抽），只作为结果里的备注，不再单独询问
VARIANCE_EXTRA_PULLS = 2

# ----------------------------- 自造货币（高级货币碎片） -----------------------------
# 计算依据（用户提供）：
#   刷 1-7 关卡的土块期望为 4.8 理智/个
#   月卡党每日理智 = 自然恢复 240 + 月卡体力药 80 = 320
#     → 320 / 4.8 ≈ 66.667 土块
#     → 制造站 2 土块 = 1 高级货币碎片 → 约 33.334 碎片
#     → 1 高级货币碎片 = 10 抽卡货币 → 约 333.34，取整 330 抽卡货币/天（抵消掉落期望误差）
#   无月卡党每日理智 = 240 → 240 / 4.8 = 50 土块 → 25 碎片 → 250 抽卡货币/天
CRAFT_JADE_PER_DAY_MONTHLY_CARD = 320
CRAFT_JADE_PER_DAY_NO_CARD = 240
CRAFT_ROCK_SANITY_PER_UNIT = 4.8      # 1-7 关卡一个土块的理智期望
CRAFT_DAILY_SANITY_CARD = 320         # 240 自然恢复 + 80 月卡体力药
CRAFT_DAILY_SANITY_NO_CARD = 240
CRAFT_ROCKS_PER_FRAGMENT = 2          # 制造站比例 2 土块 : 1 高级货币碎片
CRAFT_ORUNDUM_PER_FRAGMENT = 10       # 1 高级货币碎片 = 10 抽卡货币


def crafted_orundum_per_day(monthly_card: bool) -> int:
    """自造货币每天可得的抽卡货币（默认值）。"""
    return (CRAFT_JADE_PER_DAY_MONTHLY_CARD if monthly_card
            else CRAFT_JADE_PER_DAY_NO_CARD)


def craft_jade_detail(monthly_card: bool) -> str:
    """自造货币的推算过程说明，用于输出里备注。"""
    sanity = CRAFT_DAILY_SANITY_CARD if monthly_card else CRAFT_DAILY_SANITY_NO_CARD
    rocks = sanity / CRAFT_ROCK_SANITY_PER_UNIT
    fragments = rocks / CRAFT_ROCKS_PER_FRAGMENT
    raw = fragments * CRAFT_ORUNDUM_PER_FRAGMENT
    card_text = ("自然恢复 240 + 月卡体力药 80 = 320" if monthly_card
                 else "自然恢复 240（无月卡体力药）")
    return (f"{card_text} 理智 ÷ {CRAFT_ROCK_SANITY_PER_UNIT} ≈ {rocks:.1f} 土块 → "
            f"制造站 {CRAFT_ROCKS_PER_FRAGMENT}:1 → {fragments:.1f} 高级货币碎片 × "
            f"{CRAFT_ORUNDUM_PER_FRAGMENT} = {raw:.0f}，取整 "
            f"{crafted_orundum_per_day(monthly_card)} 抽卡货币/天")


# ----------------------------- 抽卡概率模型 -----------------------------

class GachaConfig:
    """概率与保底参数，可自行调整以适配不同卡池。"""

    def __init__(
        self,
        base_six: float = 0.02,
        six_step: float = 0.02,
        soft_pity_start: int = 50,
        hard_pity: int = 99,
        five_star: float = 0.08,
        four_star: float = 0.50,
    ) -> None:
        self.base_six = base_six                # 6 星基础概率
        self.six_step = six_step                # 保底后每抽增加的概率
        self.soft_pity_start = soft_pity_start  # 连续多少抽未出 6 星后开始加成
        self.hard_pity = hard_pity              # 第几抽必定出 6 星（1 基）
        self.five_star = five_star
        self.four_star = four_star

    def hazard(self, since_six: int) -> float:
        """第 m 抽出 6 星的条件概率（m = since_six + 1）。

        since_six = 抽这一抽之前，已经连续多少抽没出 6 星。
        规则（依据游戏内公示）：
          * 前 50 抽（since_six = 0..49）恒为基础概率 2%；
          * 第 51 抽起（since_six >= 50）每抽在基础概率上 +2%，即
            第 51 抽 4%、第 52 抽 6%、第 53 抽 8% ……；
          * 第 99 抽（since_six = 98）必定出 6 星，即 100%。
        概率上限为 1。
        """
        if since_six >= self.hard_pity - 1:
            return 1.0
        if since_six < self.soft_pity_start:
            return self.base_six
        bonus = (since_six - self.soft_pity_start + 1) * self.six_step
        return min(1.0, self.base_six + bonus)


DEFAULT_CONFIG = GachaConfig()


def pull_distribution(cfg: GachaConfig = DEFAULT_CONFIG) -> list[float]:
    """返回长度为 hard_pity+1 的列表，dist[n] = 从零开始恰好第 n 抽出 6 星的概率。

    按“条件概率 × 存活概率”逐抽递推，因此天然满足 Σ dist = 1，
    且 dist[hard_pity] 吸收了全部剩余存活概率。
    """
    dist: list[float] = [0.0]  # 索引 0 占位
    survive = 1.0              # 走到第 n 抽时仍未出过 6 星的概率
    for m in range(1, cfg.hard_pity + 1):
        p = cfg.hazard(m - 1)
        dist.append(survive * p)
        survive *= (1.0 - p)
    # 理论上 survive 已为 0；浮点兜底，保证概率和为 1
    dist[cfg.hard_pity] += survive
    return dist


def p_at_least_six(n: int, k: int, cfg: GachaConfig = DEFAULT_CONFIG) -> float:
    """精确计算：n 抽内至少出 k 个 6 星的概率。

    用“状态 = 距离上次出 6 星已经过了多少抽”的严格递推：
        dp[c]  = 当前时刻，恰好已连续 c 抽未出 6 星、且 6 星总数 < k 的概率
        每一抽：以 hazard(c) 的概率出货（计数 +1，c 归零），
                以 1-hazard(c) 的概率继续（计数不变，c + 1）
        出货次数达到 k 后进入 done 状态，不再参与后续统计。
    n 抽后 done 即为“至少 k 个”的概率。

    复杂度 O(n * hard_pity * k)，n 在几百的量级下毫秒级完成。
    """
    if k <= 0:
        return 1.0
    if n <= 0:
        return 0.0
    hard = cfg.hard_pity
    # 状态：(出货计数 j, 当前连抽未出数 c) -> 概率
    cur = [[0.0] * hard for _ in range(k)]
    cur[0][0] = 1.0
    done = 0.0
    for _ in range(n):
        nxt = [[0.0] * hard for _ in range(k)]
        for j in range(k):
            row = cur[j]
            for c in range(hard):
                pr = row[c]
                if pr <= 0.0:
                    continue
                p = cfg.hazard(c)
                if p > 0.0:
                    if j + 1 >= k:
                        done += pr * p
                    else:
                        nxt[j + 1][0] += pr * p
                if p < 1.0:
                    nc = c + 1
                    if nc >= hard:      # 理论上不会发生：c = hard-1 时 p = 1
                        nc = hard - 1
                    nxt[j][nc] += pr * (1.0 - p)
        cur = nxt
    if k == 1:
        return min(1.0, max(0.0, done))
    # k >= 2 时 done 同样表示“已经出满 k 个”的概率，即至少 k 个
    return min(1.0, max(0.0, done))


def _expected_and_confidence(cfg: GachaConfig) -> tuple[float, list[float]]:
    """返回 (出一个 6 星的期望抽数, 累计出货概率表 cum[n] = 前 n 抽内出货的概率)。"""
    dist = pull_distribution(cfg)
    cum: list[float] = []
    acc = 0.0
    for p in dist:
        acc += p
        cum.append(min(1.0, acc))
    ev = sum(n * p for n, p in enumerate(dist))
    return ev, cum


def expected_pulls(cfg: GachaConfig = DEFAULT_CONFIG) -> float:
    """出一个 6 星的平均抽数（期望）。"""
    return _expected_and_confidence(cfg)[0]


def pulls_for_confidence(confidence: float, cfg: GachaConfig = DEFAULT_CONFIG) -> int:
    """达到给定把握（如 0.9）至少出一个 6 星所需的最少抽数。"""
    cum = _expected_and_confidence(cfg)[1]
    for n, c in enumerate(cum):
        if c >= confidence:
            return n
    return cfg.hard_pity


# ----------------------------- 资源换算 -----------------------------

def pulls_from_resources(
    orundum: int = 0,
    originite: int = 0,
    tickets: int = 0,
    ten_tickets: int = 0,
    use_originite: bool = True,
    originite_keep: int = 0,
) -> dict:
    """把资源折算成抽数。

    originite_keep：打算保留不换的高级货币数量（买皮肤等），只折算超出的部分。
    """
    originite_drawable = max(0, originite - originite_keep) if use_originite else 0
    od_from_originite = originite_drawable * ORIGINITE_TO_ORUNDUM
    total_orundum = max(0, orundum) + od_from_originite
    pulls_from_orundum = total_orundum // ORUNDUM_PER_PULL
    leftover_orundum = total_orundum % ORUNDUM_PER_PULL
    total_pulls = pulls_from_orundum + max(0, tickets) + max(0, ten_tickets) * 10
    # 抽数归因：抽卡货币先按自己的整除部分计，**余数归高级货币**。
    # 以前是 `from_orundum - from_originite`（两个各自 floor 再相减），
    # 于是 400 货币 + 2 颗高级货币会被报成「抽卡货币 1 抽 / 高级货币 0 抽」——
    # 明明 360 货币是高级货币来的，环形图却全记在抽卡货币头上。
    own_orundum_pulls = max(0, orundum) // ORUNDUM_PER_PULL
    from_orundum_only = min(own_orundum_pulls, pulls_from_orundum)
    from_originite_only = pulls_from_orundum - from_orundum_only
    return {
        "total_pulls": total_pulls,
        "from_orundum": pulls_from_orundum,
        "from_originite": from_originite_only,
        "from_orundum_only": from_orundum_only,
        "from_tickets": max(0, tickets) + max(0, ten_tickets) * 10,
        "leftover_orundum": leftover_orundum,
        "originite_used": originite_drawable,
        "total_orundum": total_orundum,
    }


# ----------------------------- 兑换凭证阶段兑换 -----------------------------

def yellow_exchange_until_done(yellow: int,
                               tiers: tuple[tuple[int, int], ...] = YELLOW_EXCHANGE_TIERS,
                               monthly_limit: int = YELLOW_MONTHLY_LIMIT,
                               consumed_this_month: int = 0) -> dict:
    """把兑换凭证换成抽数：逐月消耗，算出总抽数、需要几次刷新、最后剩多少。

    **口径备注（重要）**：本程序只按「最性价比的大众习惯」算 —— 也就是把某个月的
    阶梯**换满 258 票 → 38 抽**才值得动手。所以只要某个月换不满整条阶梯
    （票不够领到最后一档），就认为这个月的兑换到此为止，不再往下算剩余的零头。

    为什么不去榨干余票：只领前几档的性价比很差（前 2 档 28 票才 3 抽，
    而换满是 258 票 38 抽），正常玩家不会为了那 1~2 抽专门等一个月。
    余票会留在账上，并不"作废"，只是不计入这次的兑换计划。

    consumed_this_month：本月在**当前档位**上已经消耗的票数（默认 0）。
      传入它时会相应扣掉本月已花的额度与已领的档位。

    例：637 票，本月还没换过
      第 1 个月：换满 5 档 = 38 抽（消耗 258 票）
      第 2 个月：换满 5 档 = 38 抽（消耗 258 票）
      第 3 个月：只剩 121 票，够 10+18+40 = 68 票 → 8 抽，余 53 票
                （下一档要 70 票，换不满整条阶梯）→ 到此为止
      → 合计 84 抽，需要 3 次刷新，余 53 票
    """
    yellow = max(0, int(yellow))
    consumed = max(0, int(consumed_this_month))
    min_cost = min(c for c, _ in tiers)
    full_pulls = sum(p for _, p in tiers)
    batches: list[dict] = []
    remaining = yellow
    index = 0
    while True:
        budget = min(remaining, monthly_limit)
        state = yellow_month_state(budget, consumed_this_month=consumed, tiers=tiers,
                                   monthly_limit=monthly_limit)
        if state["claim_pulls"] <= 0:
            break
        batches.append({
            "pulls": state["claim_pulls"],
            "spent": state["month_spent"],
            "claimed": list(state["claimed"]),
            "stages": [f"{c}票→{g}抽" for c, g in state["claimed"]],
            "month_index": index,
            "is_current_month": index == 0,
            "block_reason": state["block_reason"],
            "left_after": state["extra"],
        })
        remaining -= state["month_spent"]
        consumed = 0          # 之后的月份都是从第 1 档开始
        index += 1
        # 只有「票不够换满整条阶梯」才是终点；
        # 若是被每月上限截断（block_reason == month_limit），余票下个月还能接着换满
        if state["claim_pulls"] < full_pulls and state["block_reason"] != "month_limit":
            break
        if index > 600:
            break
    return {
        "yellow": yellow,
        "batches": batches,
        "total_pulls": sum(b["pulls"] for b in batches),
        "months_needed": len(batches),
        "leftover": remaining,
        "now_pulls": batches[0]["pulls"] if batches else 0,
        "later_pulls": sum(b["pulls"] for b in batches[1:]),
        "last_batch": batches[-1] if batches else None,
        "full_month_pulls": full_pulls,
        "monthly_limit": monthly_limit,
        "consumed_this_month": max(0, int(consumed_this_month)),
    }


def yellow_stage_to_consumed(stage: int,
                             tiers: tuple[tuple[int, int], ...] = YELLOW_EXCHANGE_TIERS) -> int:
    """把“本月已换到的档位”换算成本月已消耗的兑换凭证数。

    界面上的“已换到 X 票档”指最后一档的**票面值**（10/18/40/70/120），
    换算成累计消耗量就是前几档的消耗之和：
        换到 10  → 已消耗 0   （还没换，默认值）
        换到 18  → 已消耗 10
        换到 40  → 已消耗 28
        换到 70  → 已消耗 68
        换到 120 → 已消耗 138
        已换满   → 已消耗 258
    """
    stage = max(0, int(stage)) # type: ignore
    if stage <= 0:
        return 0
    if stage >= YELLOW_FULL_COST:
        return YELLOW_FULL_COST
    consumed = 0
    for cost, _gain in tiers:
        if stage <= cost:
            break
        consumed += cost
    return consumed


def yellow_month_state(
    yellow: int,
    consumed_this_month: int = 0,
    tiers: tuple[tuple[int, int], ...] = YELLOW_EXCHANGE_TIERS,
    monthly_limit: int = YELLOW_MONTHLY_LIMIT,
) -> dict:
    """本月兑换凭证兑换状态：还能领哪些档位、能换多少抽、余票怎么安排。

    规则（公开资料 高级凭证区）：
      * 兑换阶梯按顺序领取，不能跳档；
      * 每月最多消耗 258 票（= 换满 38 抽），超出部分要等次月 1 日 4:00 刷新。

    consumed_this_month：本月在**当前档位**上已经消耗的兑换凭证数。
      界面上问“已换到哪个档位 X”，换算规则见 yellow_stage_to_consumed，
      对第 1 档（10 票）来说「已换到 10」= 还没开始换，消耗为 0。
    """
    yellow = max(0, int(yellow))
    consumed = max(0, int(consumed_this_month))
    budget = max(0, monthly_limit - consumed)      # 本月还剩多少可消耗额度

    slot = 0
    pool = max(0, yellow - consumed)                # 手上还能花的票（已花掉的不算）
    month_used = consumed                           # 本月累计消耗
    claimed: list[tuple[int, int]] = []             # (本次实付, 得到抽数)
    block_reason = ""

    # consumed 是「本月已经花在这条阶梯上的票」，它覆盖到的档位要**整段跳过**：
    # 那些档位的抽早就领过了，再算一次等于白送（实付 0 却记 1 抽）。
    covered = 0
    while slot < len(tiers):
        covered += tiers[slot][0]
        if covered > consumed:
            break
        slot += 1

    while slot < len(tiers):
        cost, gain = tiers[slot]
        if month_used + cost > monthly_limit:       # 触及每月 258 票上限
            block_reason = "month_limit"
            break
        if pool < cost:                             # 票不够，换不到这一档
            block_reason = "no_tickets"
            break
        pool -= cost
        month_used += cost
        claimed.append((cost, gain))
        slot += 1
    else:
        # 所有档位都领完了；若恰好用满本月额度，说明是被月上限收尾的
        if month_used >= monthly_limit:
            block_reason = "month_limit"
        elif pool <= 0:
            block_reason = "no_tickets"

    claim_pulls = sum(g for _, g in claimed)
    return {
        "yellow": yellow,
        "budget": budget,
        "claimed": claimed,
        "claim_pulls": claim_pulls,
        "month_spent": month_used,
        "extra": pool,
        "block_reason": block_reason,
        "blocked": bool(block_reason),
        "capacity_left": max(0, monthly_limit - month_used),
        "monthly_limit": monthly_limit,
        "tiers": tiers,
        # 兼容旧字段名
        "leftover": pool,
        "spent": month_used,
        "blocked_by_stage": block_reason == "no_tickets",
    }


def yellow_pulls(yellow: int, tiers: tuple[tuple[int, int], ...] = YELLOW_EXCHANGE_TIERS) -> dict:
    """把兑换凭证（高级凭证）按阶段兑换折算成抽数。

    兑换是阶段式的：必须按顺序一档一档换，换完一档才解锁下一档，
    所以兑换凭证不够满额时只能拿到前面几档，不能跳档或拆档。
    默认档位（公开资料 高级凭证区兑换阶梯表）：
        10 兑换凭证 → 1 抽
        18 兑换凭证 → 2 抽
        40 兑换凭证 → 5 抽
        70 兑换凭证 → 1 张十连券（10 抽）
        120 兑换凭证 → 2 张十连券（20 抽，写成一档 10 抽）
        合计 258 兑换凭证 → 38 抽
    """
    yellow = max(0, int(yellow))
    pulls = 0
    spent = 0
    stages: list[str] = []
    for cost, gain in tiers:
        if yellow - spent >= cost:
            spent += cost
            pulls += gain
            stages.append(f"{cost}票→{gain}抽")
        else:
            break
    full_cost = sum(c for c, _ in tiers)
    full_pulls = sum(p for _, p in tiers)
    return {
        "pulls": pulls,
        "spent": spent,
        "left": yellow - spent,
        "stages": stages,
        "is_full": spent >= full_cost,
        "missing_to_full": max(0, full_cost - spent),
        "full_cost": full_cost,
        "full_pulls": full_pulls,
    }


# ----------------------------- 收入模型 -----------------------------

def income_breakdown(
    monthly_card: bool = False,
    od_per_run: float = DEFAULT_ORUNDUM_PER_RUN,
    weekly_task: float = DEFAULT_WEEKLY_TASK,
    daily_task: float = DEFAULT_DAILY_TASK,
    signin_tickets: int = DEFAULT_SIGNIN_TICKETS,
    green_tickets: int = DEFAULT_GREEN_TICKETS,
    green_orundum: int = DEFAULT_GREEN_ORUNDUM,
    maintenance_orundum: int = DEFAULT_MAINTENANCE_ORUNDUM,
    maintenance_originite: int = DEFAULT_MAINTENANCE_ORIGINITE,
    yellow_exchange_pulls: int = 0,
) -> dict:
    """返回每月 / 每日收入明细。

    把每月一次性的收入（签到、次级凭证商店、维护补偿）分摊到天，
    这样“还要攒多少天”的预测才准确（否则会低估收入）。
    """
    per_day: list[tuple[str, float]] = [
        ("每日任务", float(daily_task)),
        ("每周任务（500×4 周/月）", float(weekly_task)),
        ("每周副本作战（1800×4 周）", float(od_per_run)),
    ]
    if monthly_card:
        per_day.append(("月卡每日", float(MONTHLY_CARD_ORUNDUM)))

    od_total = sum(v for _, v in per_day)

    # 每月一次性项目
    monthly: list[tuple[str, float, str]] = []
    if signin_tickets:
        monthly.append(("每月签到累计", signin_tickets * ORUNDUM_PER_PULL, f"{signin_tickets} 抽"))
    if green_tickets or green_orundum:
        green_total = green_tickets * ORUNDUM_PER_PULL + green_orundum
        monthly.append((
            f"次级凭证商店（{green_tickets} 券 + {green_orundum} 货币 "
            f"= {green_total / ORUNDUM_PER_PULL:g} 抽）",
            float(green_total), f"{green_total / ORUNDUM_PER_PULL:g} 抽"))
    if maintenance_orundum:
        monthly.append(("维护补偿·抽卡货币", float(maintenance_orundum), f"{maintenance_orundum} 货币"))
    if maintenance_originite:
        monthly.append(("更新补偿·高级货币", maintenance_originite * ORIGINITE_TO_ORUNDUM,
                        f"{maintenance_originite} 颗"))

    monthly_od = sum(v for _, v, _ in monthly)
    monthly_od += od_total * 30
    # 注意：兑换凭证兑换是**一次性**资源，不进“每月收入速率”，
    # 否则会与“现有抽数”里的兑换凭证重复计算。这里只把它单独返回。
    daily_total = monthly_od / 30.0
    return {
        "per_day": per_day,
        "od_per_day": od_total,
        "monthly": monthly,
        "monthly_od": monthly_od,
        "daily_total": daily_total,
        "daily_pulls": daily_total / ORUNDUM_PER_PULL,
        "yellow_pulls": yellow_exchange_pulls,
        "yellow_orundum": yellow_exchange_pulls * ORUNDUM_PER_PULL,
    }


def monthly_card_plan(start: "_dt.date", end: "_dt.date") -> dict:
    """从「次月」到「目标月」（含目标月）还要买几张月卡，以及这笔钱和附赠高级货币。

    口径（按约定算法，不按天数摊）：
      · **本月这张**算你已经在用，不再购买；
      · 次月 1 日起，每个自然月都要有一张，所以张数 = 目标月序号 − 本月序号；
      · 每张 30 元，购买立得 6 高级货币（≈ 1080 货币 ≈ 1.8 抽）。

    例：1/15 → 7/20，次月到目标月是 2~7 月 → 6 张 → **180 元、36 高级货币**。
    本月内就收工（start 与 end 同月）→ 0 张，不用再花一分钱。
    """
    if end < start:
        start, end = end, start

    months = max(0, (end.year * 12 + end.month) - (start.year * 12 + start.month))
    originite = months * MONTHLY_CARD_ORIGINITE
    labels: list[str] = []
    for i in range(1, months + 1):
        # 从次月开始往后数，转回 1~12
        ordinal = start.year * 12 + (start.month - 1) + i
        labels.append(f"{ordinal % 12 + 1}月")

    return {
        "months": months,
        "cost": months * MONTHLY_CARD_PRICE,
        "originite": originite,
        "orundum": originite * ORIGINITE_TO_ORUNDUM,
        "pulls": originite * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "labels": labels,
        "price_each": MONTHLY_CARD_PRICE,
        "originite_each": MONTHLY_CARD_ORIGINITE,
        "start": start,
        "end": end,
    }


def days_to_target(
    current_pulls: int,
    target_pulls: int,
    od_per_day: float,
    extra_orundum: int = 0,
) -> dict:
    """预测攒到目标抽数所需天数（不满一天按一天算，向上取整）。"""
    need = target_pulls - current_pulls
    if need <= 0:
        return {"days": 0, "need_pulls": 0, "need_orundum": 0, "reachable": True}
    need_orundum = need * ORUNDUM_PER_PULL - max(0, extra_orundum)
    if need_orundum <= 0:
        return {"days": 0, "need_pulls": need, "need_orundum": 0, "reachable": True}
    if od_per_day <= 0:
        return {"days": None, "need_pulls": need, "need_orundum": need_orundum, "reachable": False}
    return {
        "days": math.ceil(need_orundum / od_per_day),
        "need_pulls": need,
        "need_orundum": need_orundum,
        "reachable": True,
    }


def first_day_reaching(current_pulls: int, target_pulls: int, *,
                       extra_orundum: int = 0, yellow: int = 0, yellow_base: int = 0,
                       veteran: bool = True, monthly_card: bool = False,
                       craft_jade: bool = False, consumed_this_month: int = 0,
                       start: "_dt.date | None" = None,
                       max_days: int = 3650) -> int | None:
    """目标抽数算法：逐日累加，第一次「现有 + 期间累计 ≥ 目标」就是答案。

    按用户口径实现 —— 不做二分、也不做「先算天数再修正」的迭代：
        缺口 = (目标 − 现有) × 600 货币（再扣掉一次性额外收入）
        从 start 当天开始，一天一天把账本上的资源换成抽数累加，够了就停，
        累加的次数就是这个天数，带回日历就是「预计达成日」。

    为什么二分反而不行：账本里有按日期跳变的东西（15 号复刻 +3 抽、大版本开幕
    +3 抽 +11 张限时券），再加上限时券「过期作废」会让总量中途回落，
    「天数 → 总量」不是严格单调的，二分会在窗口边界上给出错误的日期。
    逐日扫描取「第一次达标」，语义更贴近玩家真实的「到那天够不够」。

    限时券不参与滚动累加，而是按「当天是否在某个大版本窗口内」现算
    （`major_timed_live`）：活动结束就作废，不能算进后面的日子。
    """
    start = start or _dt.date.today()
    if target_pulls - current_pulls <= 0:
        return 0

    led = daily_ledger(start, start + _dt.timedelta(days=max_days),
                       veteran=veteran, monthly_card=monthly_card,
                       craft_jade=craft_jade)
    per_day: dict[int, float] = {}
    for row in led["rows"]:
        # 统一换成**抽卡货币**单位累加（高级货币 180 货币、单抽券 600 货币），
        # 这样就不存在「0.9 抽怎么取整」的问题，也和「缺口 = X 抽 × 600 货币」同量纲。
        # 限时券不滚存，按天用 major_timed_live() 现算。
        idx = (row["date"] - start).days
        per_day[idx] = per_day.get(idx, 0.0) + (
            row["orundum"] + row["originite"] * ORIGINITE_TO_ORUNDUM
            + row["tickets"] * ORUNDUM_PER_PULL)

    # 兑换凭证只在每月 1 日刷新时跳一次，先把刷新点算好，别天天重算
    refresh_days: list[int] = []
    probe = start
    for _ in range(600):
        probe = _add_months(probe, 1)
        gap_days = (probe - start).days
        if gap_days > max_days:
            break
        refresh_days.append(gap_days)

    # 缺口统一换成抽卡货币：目标抽数 × 600 − 现有抽数 × 600 − 一次性额外收入
    gap_orundum = ((target_pulls - current_pulls) * ORUNDUM_PER_PULL
                   - max(0, extra_orundum))
    if gap_orundum <= 0:
        return 0

    running = 0.0
    y_extra = 0.0
    ptr = 0
    for i in range(max_days + 1):
        running += per_day.get(i, 0.0)
        while ptr < len(refresh_days) and refresh_days[ptr] <= i:
            ptr += 1
            y_extra = max(0.0, yellow_exchange_plan(
                yellow, ptr, consumed_this_month=consumed_this_month)["total_pulls"]
                - max(0, yellow_base))
        live = major_timed_live(start + _dt.timedelta(days=i), start)
        if running + y_extra * ORUNDUM_PER_PULL + live * ORUNDUM_PER_PULL >= gap_orundum:
            return i
    return None


# ----------------------------- 活动日历 / 限定池分析 -----------------------------

def resolve_event(event: str, today: "_dt.date | None" = None) -> dict:
    """解析活动名称与今天的日期，返回活动日期与剩余天数。

    支持：周年 / 半周年 / 夏活 / 春节 / YYYY-MM-DD / MM-DD。
    档期来源：周年 5/1、夏活 8/1、半周年 11/1（各持续 14 日）；春节为农历正月初一。
    """
    today = today or _dt.date.today()
    text = (event or "").strip()
    low = text.lower()

    if _looks_like_date(text) or _looks_like_month_day(text):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m-%d", "%m/%d"):
            try:
                parsed = _dt.datetime.strptime(text, fmt).date()
            except ValueError:
                continue
            if fmt.startswith("%m"):
                parsed = parsed.replace(year=today.year)
                if parsed < today:
                    parsed = parsed.replace(year=today.year + 1)
            # 自定义输入 = **目标日**：要的是「到这一天为止能攒多少」，
            # 所以截止日就是这一天，不再往后推 14 天。
            # 若这天正好落在某个大活动档期内，名字沿用档期名（限时券按大活动口径），
            # 但统计范围仍然只到这一天。
            window = _event_window_at(parsed)
            if window:
                kind = "cny" if "春节" in window else "major"
                return _event_result(f"{window} · 目标日", parsed, today, kind=kind,
                                     end_date=parsed, is_target=True)
            return _event_result(f"目标日 {parsed.isoformat()}", parsed, today,
                                 kind="custom", end_date=parsed, is_target=True)

    if "春节" in text or "新年" in text or "春池" in text or low in ("cny", "spring"):
        date = _spring_festival(today)
        return _event_result("春节限定（正月初一）", date, today, kind="cny")

    if any(k in text for k in ("夏活", "夏日", "夏季")) or low in ("summer",):
        date = _next_annual(8, 1, today)
        return _event_result("夏活（8/1）", date, today, kind="summer")

    if any(k in text for k in ("半周年", "半週年")) or low in ("half", "0.5"):
        date = _next_annual(11, 1, today)
        return _event_result("半周年庆（11/1）", date, today, kind="half")

    if "周年" in text or low in ("anniv", "anniversary"):
        date = _next_annual(5, 1, today)
        return _event_result("周年庆（5/1）", date, today, kind="anniv")

    # 默认取“最近的下一个限定抽取档期”（周年 / 夏活 / 半周年 / 春节 取最早）
    candidates: list[tuple["_dt.date", str, str]] = []
    for name, month, day, kind in (
        ("周年庆（5/1）", 5, 1, "anniv"),
        ("夏活（8/1）", 8, 1, "summer"),
        ("半周年庆（11/1）", 11, 1, "half"),
    ):
        candidates.append((_next_annual(month, day, today), name, kind))
    cny = _spring_festival(today)
    candidates.append((cny, "春节限定（正月初一）", "cny"))
    date, name, kind = min(candidates, key=lambda item: item[0])
    return _event_result(name, date, today, kind=kind)


def _looks_like_date(text: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text or ""))


def _looks_like_month_day(text: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{1,2}[-/]\d{1,2}", text or ""))


def _next_annual(month: int, day: int, today: "_dt.date") -> "_dt.date":
    """取今年或明年的固定档期（5/1、8/1、11/1）。

    活动要持续 EVENT_DURATION_DAYS 天，所以「开始日 + 13 天」之前都算**这一期还没结束**，
    必须返回今年的日期。

    以前写成 `if date < today`，于是活动进行中（例如半周年 11/1~11/14，今天 11/7）
    会整段被跳过、直接跳到明年，目标日一下差出一年、抽数虚高几百。
    """
    date = _dt.date(today.year, month, day)
    if date + _dt.timedelta(days=EVENT_DURATION_DAYS - 1) < today:
        date = _dt.date(today.year + 1, month, day)
    return date


# 春节（农历正月初一）公历日期表，覆盖 2025–2126，避免日历“用完”
SPRING_FESTIVAL_TABLE: dict[int, tuple[int, int]] = {
    2025: (1, 29), 2026: (2, 17), 2027: (2, 6), 2028: (1, 26), 2029: (2, 13),
    2030: (2, 3), 2031: (1, 23), 2032: (2, 11), 2033: (1, 31), 2034: (2, 19),
    2035: (2, 8), 2036: (1, 28), 2037: (2, 15), 2038: (2, 4), 2039: (1, 24),
    2040: (2, 12), 2041: (2, 1), 2042: (1, 22), 2043: (2, 10), 2044: (1, 30),
    2045: (2, 17), 2046: (2, 6), 2047: (1, 26), 2048: (2, 14), 2049: (2, 2),
    2050: (1, 23), 2051: (2, 11), 2052: (2, 1), 2053: (2, 19), 2054: (2, 8),
    2055: (1, 28), 2056: (2, 15), 2057: (2, 4), 2058: (1, 24), 2059: (2, 12),
    2060: (2, 2), 2061: (1, 21), 2062: (2, 9), 2063: (1, 29), 2064: (2, 17),
    2065: (2, 5), 2066: (1, 26), 2067: (2, 14), 2068: (2, 3), 2069: (1, 23),
    2070: (2, 11), 2071: (1, 31), 2072: (2, 19), 2073: (2, 7), 2074: (1, 27),
    2075: (2, 15), 2076: (2, 5), 2077: (1, 24), 2078: (2, 12), 2079: (2, 2),
    2080: (1, 22), 2081: (2, 9), 2082: (1, 29), 2083: (2, 17), 2084: (2, 6),
    2085: (1, 26), 2086: (2, 14), 2087: (2, 3), 2088: (1, 24), 2089: (2, 10),
    2090: (1, 30), 2091: (2, 18), 2092: (2, 7), 2093: (1, 27), 2094: (2, 15),
    2095: (2, 5), 2096: (1, 25), 2097: (2, 12), 2098: (2, 1), 2099: (1, 21),
    2100: (2, 9), 2101: (1, 29), 2102: (2, 17), 2103: (2, 7), 2104: (1, 27),
    2105: (2, 14), 2106: (2, 3), 2107: (1, 24), 2108: (2, 11), 2109: (1, 31),
    2110: (2, 19), 2111: (2, 8), 2112: (1, 28), 2113: (2, 15), 2114: (2, 5),
    2115: (1, 25), 2116: (2, 13), 2117: (2, 1), 2118: (1, 22), 2119: (2, 10),
    2120: (1, 30), 2121: (2, 17), 2122: (2, 7), 2123: (1, 27), 2124: (2, 15),
    2125: (2, 3), 2126: (1, 24),
}


def _spring_festival_by_year(year: int) -> "_dt.date":
    """取指定年份的春节（农历正月初一）。

    表内年份直接用表；表外（2127 年及以后）用「19 年周期（默冬章）」外推：
    农历 19 年 7 闰，春节公历日期几乎重复，与 2025–2126 表实测相差 0~1 天。
    这样日历不会“用完”，再远的年份也能给出量级正确的日期。
    """
    entry = SPRING_FESTIVAL_TABLE.get(year)
    if entry is not None:
        return _dt.date(year, entry[0], entry[1])
    # 往前找最近一个表内年份，按整数个 19 年周期回推
    first_year = min(SPRING_FESTIVAL_TABLE)
    cycles = (year - first_year) // 19
    anchor_year = year - cycles * 19
    while anchor_year not in SPRING_FESTIVAL_TABLE:   # 兜底：逐年后退
        anchor_year -= 1
        cycles = (year - anchor_year) // 19
    anchor = _dt.date(anchor_year, *SPRING_FESTIVAL_TABLE[anchor_year])
    return anchor + _dt.timedelta(days=cycles * 6939)   # 19 年 ≈ 6939 天


def _spring_festival(today: "_dt.date") -> "_dt.date":
    """取下一个春节（农历正月初一）。表外年份用 19 年周期外推。

    同样要考虑「活动还没结束」：初一到十四之间都算这一期，不能跳到明年。
    """
    for year in range(today.year, today.year + 4):
        date = _spring_festival_by_year(year)
        if date + _dt.timedelta(days=EVENT_DURATION_DAYS - 1) >= today:
            return date
    return _spring_festival_by_year(today.year + 1)


def _event_result(name: str, date: "_dt.date", today: "_dt.date",
                  kind: str = "custom",
                  end_date: "_dt.date | None" = None,
                  is_target: bool = False) -> dict:
    """end_date 默认按「活动持续 14 天」推；目标日则直接等于当天。

    is_target 单独标出来：用户填的是「目标日」还是「活动档期」，文案不一样
    （「攒到目标日」vs「攒到活动最后一天」）。目标日恰好落在春节/大活动窗口里时，
    kind 会是 cny/major，所以不能用 kind == "custom" 来判断。
    """
    days = (date - today).days
    return {"name": name, "date": date, "days": max(0, days), "today": today,
            "kind": kind, "is_target": is_target,
            "end_date": end_date or (date + _dt.timedelta(days=EVENT_DURATION_DAYS - 1)),
            "is_estimate": "估算" in name}


def _event_window_at(day: "_dt.date") -> str | None:
    """这一天落在哪个大活动档期里（含首尾）；不在任何档期内返回 None。

    用途：用户填「目标日」时，用来说明这天处于哪个活动窗口——决定限时券要不要按
    大活动口径补，但不改变「统计到这一天为止」这件事本身。
    """
    for name, month, mday, _cls in MAJOR_EVENT_SPECS:
        for year in (day.year - 1, day.year, day.year + 1):
            start = _dt.date(year, month, mday)
            if start <= day <= start + _dt.timedelta(days=EVENT_DURATION_DAYS - 1):
                return name
    for year in (day.year - 1, day.year, day.year + 1):
        start = _spring_festival_by_year(year)
        if start <= day <= start + _dt.timedelta(days=EVENT_DURATION_DAYS - 1):
            return "春节限定（正月初一）"
    return None


# ----------------------------- 大活动赠送 -----------------------------

def event_rewards(days_into_event: int) -> dict:
    """大活动（周年/夏活/半周年/春节）期间的额外赠送，按“活动第几天”累计。

    每个大活动共送 **24 张限时单抽券 + 8300 抽卡货币**（合计约 38 抽）。
      * 抽卡货币 8300（≈13.83 抽）按 14 天均匀摊到每天
      * 限时凭证按 EVENT_REWARD_TIMED_SCHEDULE 逐日发放，总和 24
    days_into_event 为 1 表示活动第 1 天。
    """
    if days_into_event <= 0:
        return {"orundum": 0, "timed_pulls": 0, "days": 0, "orundum_per_day": 0.0}
    days = min(days_into_event, EVENT_DURATION_DAYS)
    orundum_per_day = EVENT_REWARD_ORUNDUM / EVENT_DURATION_DAYS
    orundum = orundum_per_day * days
    timed = sum(EVENT_REWARD_TIMED_SCHEDULE[:days])
    return {
        "orundum": orundum,
        "orundum_per_day": orundum_per_day,
        "timed_pulls": timed,
        "days": days,
    }


def rewards_between(start: "_dt.date", end: "_dt.date",
                    today: "_dt.date | None" = None) -> dict:
    """统计 [start, end] 区间内的大活动赠送（考虑周年/夏活/半周年/春节）。"""
    today = today or _dt.date.today()
    dates: list[tuple[str, "_dt.date"]] = []
    for name, month, day, _kind in MAJOR_EVENT_SPECS:
        for year in range(start.year - 1, end.year + 2):
            dates.append((name, _dt.date(year, month, day)))
    for year in range(start.year - 1, end.year + 2):
        # 表外年份也能外推（19 年周期），不能因为不在表里就跳过，
        # 否则 2127 年以后整个春节档期的赠送都会凭空消失
        dates.append(("春节", _spring_festival_by_year(year)))

    total_orundum = 0.0
    total_timed = 0
    details: list[dict] = []
    for name, begin in dates:
        finish = begin + _dt.timedelta(days=EVENT_DURATION_DAYS - 1)
        if finish < start or begin > end:
            continue
        # 只统计落在窗口内的那部分天数
        first = max(begin, start)
        last = min(finish, end)
        if last < first:
            continue
        days_in = (last - first).days + 1
        # 活动第几天开始计入窗口
        offset = (first - begin).days + 1
        gained_full = event_rewards(EVENT_DURATION_DAYS)
        gained_part = event_rewards(offset + days_in - 1)
        # 只取窗口内新增的部分
        orundum = gained_part["orundum"] - event_rewards(offset - 1)["orundum"]
        timed = gained_part["timed_pulls"] - event_rewards(offset - 1)["timed_pulls"]
        total_orundum += orundum
        total_timed += timed
        if orundum or timed:
            details.append({
                "name": name, "begin": begin, "end": finish,
                "days_in_window": days_in, "orundum": orundum, "timed_pulls": timed,
                "full_orundum": gained_full["orundum"],
                "full_timed_pulls": gained_full["timed_pulls"],
            })
    return {
        "orundum": total_orundum,
        "timed_pulls": total_timed,
        "pulls_from_orundum": total_orundum / ORUNDUM_PER_PULL,
        "total_pulls": total_timed + total_orundum / ORUNDUM_PER_PULL,
        "details": sorted(details, key=lambda d: d["begin"]),
    }


# ----------------------------- 兑换凭证月度兑换 -----------------------------

def _add_months(date: "_dt.date", months: int) -> "_dt.date":
    month_index = date.month - 1 + months
    year = date.year + month_index // 12
    month = month_index % 12 + 1
    return _dt.date(year, month, 1)


def month_refreshes_between(start: "_dt.date", end: "_dt.date") -> int:
    """(start, end] 内还会经历几次月初刷新（= 当前月之外还能再兑换几次）。

    注意：start 所在月的兑换额度现在就可用，所以不计入这里。
    例：10/10 → 11/1 返回 1（只多出 11/1 那一次）；同月内返回 0。
    """
    if end <= start:
        return 0
    count = 0
    probe = start
    while True:
        probe = _add_months(probe, 1)
        if probe > end:
            break
        count += 1
    return count


def yellow_exchange_plan(yellow: int, months_available: int = 0,
                         yellow_gain_per_month: int = 0,
                         consumed_this_month: int = 0) -> dict:
    """按“每月最多 258 票 = 38 抽”把兑换凭证拆成本月 / 之后各月可兑换。

    months_available = 当前月之外还能再兑换的**额外**月数（同月内兑换为 0）。
    consumed_this_month = 本月已经在这条阶梯上消耗掉的票（见 yellow_stage_to_consumed）。

    口径与 `yellow_exchange_until_done()` **完全一致**：某个月如果换不满整条阶梯
    （票不够领到最后一档），就是这个月的终点，不把余票挪到下个月再榨那 1~2 抽。
    以前这里图省事用了「min(remaining, 258) 逐月贪心」，于是 40 票会算出
    4 抽/2 个月，而兑换凭证页说 3 抽/1 个月 —— 同一份票两个页面两个答案。
    """
    yellow = max(0, int(yellow))
    consumed = max(0, int(consumed_this_month))
    total_rounds = 1 + max(0, int(months_available))   # 本月 + 额外月数
    batches: list[dict] = []
    remaining = yellow
    for index in range(total_rounds):
        state = yellow_month_state(
            remaining,
            consumed_this_month=(consumed if index == 0 else 0),
        )
        if state["claim_pulls"] <= 0:
            break
        info = {
            "pulls": state["claim_pulls"],
            "spent": state["month_spent"],
            "left": state["extra"],
            "stages": [f"{c}票→{g}抽" for c, g in state["claimed"]],
            "claimed": list(state["claimed"]),
            "block_reason": state["block_reason"],
            "month_index": index,
            "is_current_month": index == 0,
        }
        batches.append(info)
        remaining = state["extra"]
        # 换不满整条阶梯（且不是被每月上限截断）→ 这个月就是终点
        if (state["claim_pulls"] < YELLOW_FULL_PULLS
                and state["block_reason"] != "month_limit"):
            break

    now_pulls = sum(b["pulls"] for b in batches if b["is_current_month"])
    later_pulls = sum(b["pulls"] for b in batches if not b["is_current_month"])
    return {
        "yellow": yellow,
        "months_available": max(0, int(months_available)),
        "batches": batches,
        "now_pulls": now_pulls,
        "later_pulls": later_pulls,
        "now_spent": sum(b["spent"] for b in batches if b["is_current_month"]),
        "later_spent": sum(b["spent"] for b in batches if not b["is_current_month"]),
        "total_pulls": now_pulls + later_pulls,
        "leftover": remaining,
        "months_needed": len(batches),
        "consumed_this_month": consumed,
    }


def spark_tiers(pulls: int) -> dict:
    """按井的档位递减，算出能兑换几个角色。

    井（兑换点数交换所）的档位：
      300 兑换点数 → 当期限定 6★（或当期 UP 6★）
      200 兑换点数 → 解限满 3 年的旧限定（春节系列 4 年）
       75 兑换点数 → 当期 UP 5★
    兑换次数不限，但每兑一次就扣掉对应兑换点数数；这里按“从高到低尽量兑”的
    贪心策略给出可兑换数量：优先保证当期限定，剩下的再去兑 200 / 75 档。
    """
    n = max(0, int(pulls))
    result = {
        "pulls": n,
        "sparks": [],
        "limited_count": 0,
        "old_limited_count": 0,
        "five_star_count": 0,
        "total_operators": 0,
        "left_contracts": n,
    }
    # 优先兑 300（当期限定）：能吃几个吃几个（第一个是刚需）
    while result["left_contracts"] >= 300:
        result["limited_count"] += 1
        result["left_contracts"] -= 300
    while result["left_contracts"] >= 200:
        result["old_limited_count"] += 1
        result["left_contracts"] -= 200
    while result["left_contracts"] >= 75:
        result["five_star_count"] += 1
        result["left_contracts"] -= 75
    result["total_operators"] = (result["limited_count"] + result["old_limited_count"]
                                 + result["five_star_count"])
    result["sparks"] = [
        (300, result["limited_count"], "当期限定 / 当期 UP 6★"),
        (200, result["old_limited_count"], "解限旧限定 6★"),
        (75, result["five_star_count"], "当期 UP 5★"),
    ]
    return result


def pulls_byproducts(pulls: int, cfg: GachaConfig = DEFAULT_CONFIG,
                     five_star_repeat_only: bool = False,
                     profile: str = DEFAULT_PLAYER_PROFILE) -> dict:
    """估算抽卡副产物：各稀有度数量 + 溢出兑换券换到的兑换凭证 / 次级凭证。

    换算依据（满阶溢出兑换券兑换凭证）：
      * 3★（出率 40%）：每只 5 张次级凭证（资质凭证）
      * 4★（出率 50%）：每只 1 张兑换凭证（高级凭证）
      * 5★（出率 8%）：重复获得 5 张兑换凭证；满阶重复 13 张
      * 6★（综合出率 ≈ 2.89%）：首次 1 张兑换凭证；重复 10 张

    profile 是玩家档位（见 PLAYER_PROFILES），决定 6★/5★ 按「首次」还是「重复」估：
      * 老玩家（默认）：5★ 走满阶 13 张（five_star_repeat_only 切 5 张），
                       6★ 只算重复（总期望减去首次那 1 个）—— 保持旧口径不变；
      * 新玩家 / 中玩家：按档位给的每只平均兑换凭证直接乘期望个数。
    """
    n = max(0, int(pulls))
    ev_six = expected_pulls(cfg)
    six_expected = n / ev_six if ev_six > 0 else 0.0
    prof = PLAYER_PROFILE_MAP.get(profile) or PLAYER_PROFILE_MAP[DEFAULT_PLAYER_PROFILE]
    is_veteran = prof["key"] == "veteran"

    if is_veteran:
        five_yellow = (BYPRODUCT_5STAR_YELLOW_REPEAT if five_star_repeat_only
                       else BYPRODUCT_5STAR_YELLOW_MAXPOT)
        # 6★ 的“重复获得”期望：总 6★ 期望减去首次那 1 个
        six_yellow = max(0.0, six_expected - 1) * BYPRODUCT_6STAR_YELLOW_REPEAT
    else:
        five_yellow = prof["five_yellow"]
        six_yellow = six_expected * prof["six_yellow"]

    breakdown = {
        "three": n * RARITY_3 * BYPRODUCT_3STAR_GREEN,   # 次级凭证，不算兑换凭证
    }
    yellow = {
        "four": n * RARITY_4 * BYPRODUCT_4STAR_YELLOW,
        "five": n * RARITY_5 * five_yellow,
        "six": six_yellow,
    }
    green = breakdown["three"]
    return {
        "pulls": n,
        "count_3": n * RARITY_3,
        "count_4": n * RARITY_4,
        "count_5": n * RARITY_5,
        "count_6": six_expected,
        "yellow_full": yellow,
        "yellow_full_total": sum(yellow.values()),
        "yellow_repeat": yellow,
        "yellow_repeat_total": sum(yellow.values()),
        "green": green,
        "five_star_yellow_each": five_yellow,
        "six_star_yellow_each": BYPRODUCT_6STAR_YELLOW_REPEAT,
        "profile": prof["key"],
        "profile_label": prof["label"],
    }


def resource_summary(orundum: int = 0, originite: int = 0, tickets: int = 0,
                     ten_tickets: int = 0, yellow: int = 0, yellow_stage: int = 10,
                     use_originite: bool = True, originite_keep: int = 0,
                     profile: str = DEFAULT_PLAYER_PROFILE) -> dict:
    """现有资源汇总（功能 1）：合计抽数 + 明细 + 兑换凭证余数 + 这批抽的副产物。

    兑换凭证只按**当前这一轮** 258 票算（最多 38 抽），超出的票不折进合计 ——
    那些要等次月刷新才换得动，属于"以后的事"，不算"现在能抽多少"。
    例：500 票、本月已换到 18 票档 → 本月还能换 37 抽，余 242 张。
    """
    r = pulls_from_resources(orundum=orundum, originite=originite, tickets=tickets,
                             ten_tickets=ten_tickets, use_originite=use_originite,
                             originite_keep=originite_keep)
    state = yellow_month_state(
        yellow, consumed_this_month=yellow_stage_to_consumed(yellow_stage))
    yellow_pulls = min(state["claim_pulls"], YELLOW_FULL_PULLS)
    yellow_left = max(0, state["extra"])

    # 高级货币折算出的抽数已经由 pulls_from_resources 归因好了（余数归高级货币）
    from_orundum_only = r["from_orundum_only"]
    total = r["total_pulls"] + yellow_pulls

    profile_key = profile if profile in PLAYER_PROFILE_MAP else DEFAULT_PLAYER_PROFILE
    products = pulls_byproducts(total, profile=profile_key)

    items = [
        {"key": "orundum", "label": "抽卡货币", "pulls": from_orundum_only,
         "value": f"{max(0, orundum):,} 货币", "color": "#E24B4A", "shade": "#8E2A2A"},
        {"key": "originite", "label": "高级货币", "pulls": r["from_originite"],
         "value": f"{r['originite_used']} 颗", "color": "#EF9F27", "shade": "#8A5A0B"},
        {"key": "tickets", "label": "单抽凭证", "pulls": max(0, tickets),
         "value": f"{max(0, tickets)} 张", "color": "#FAC775", "shade": "#888780"},
        {"key": "ten", "label": "十连凭证", "pulls": max(0, ten_tickets) * 10,
         "value": f"{max(0, ten_tickets)} 张", "color": "#B4B2A9", "shade": "#EF9F27"},
    ]
    # 兑换凭证也占抽数，不给它一片的话四个占比加起来到不了 100%
    if yellow_pulls:
        items.append({"key": "yellow", "label": "兑换凭证", "pulls": yellow_pulls,
                      "value": f"{yellow} 票 → {yellow_pulls} 抽",
                      "color": "#7F77DD", "shade": "#534AB7"})
    for item in items:
        item["percent"] = (item["pulls"] / total * 100) if total else 0.0

    return {
        "total_pulls": total,
        "orundum": max(0, orundum),
        "originite": max(0, originite),
        "originite_used": r["originite_used"],
        "tickets": max(0, tickets),
        "ten_tickets": max(0, ten_tickets),
        "leftover_orundum": r["leftover_orundum"],
        "yellow": max(0, yellow),
        "yellow_stage": yellow_stage,
        "yellow_pulls": yellow_pulls,
        "yellow_left": yellow_left,
        "yellow_claimed": list(state["claimed"]),
        "yellow_block": state["block_reason"],
        "items": items,
        "products": products,
        "profile": profile_key,
        "profile_label": PLAYER_PROFILE_MAP[profile_key]["label"],
        "profile_note": PLAYER_PROFILE_MAP[profile_key]["note"],
        "resource_pulls": r["total_pulls"],
    }


def spark_summary(pulls: int) -> dict:
    """可井：把 X 抽全砸进一个限定池，各档位能换几个 + 还剩多少兑换点数。

    口径（按约定的迭代顺序）：
      * 总计 **> 300** → 先按 300 档换（pulls // 300 个），另外累计 300 抽
        还额外赠送 1 个当期限定（2024-05-01 起），余数不再往下换；
      * 否则能换 200 档就换 200；再否则 75 档；都不够就只报余数。
    注意 300 档/200 档换的是 6★，75 档换的是当期 UP 5★，种类不同。
    """
    n = max(0, int(pulls))
    cost = CONTRACT_COST_PER_SPARK          # 300
    # 注意是 >= 而不是 >：刚好 300 抽时既能用 300 兑换点数换 1 个，又能领「满 300 抽赠送」
    # 的那 1 个，一共 2 个。以前写成 >，300 抽会被算成「200 档 1 个、赠送 0 个」，
    # 而在 301 抽处又突变回 2 个 —— 是断点，也是 README 明确写过的「满 300 送 1 个」。
    if n >= cost:
        count = n // cost
        return {"tier300": count, "tier200": 0, "tier75": 0,
                "gift": 1, "left": n - count * cost,
                "total_operators": count + 1}
    if n >= 200:
        return {"tier300": 0, "tier200": 1, "tier75": 0,
                "gift": 0, "left": n - 200, "total_operators": 1}
    if n >= 75:
        return {"tier300": 0, "tier200": 0, "tier75": 1,
                "gift": 0, "left": n - 75, "total_operators": 1}
    return {"tier300": 0, "tier200": 0, "tier75": 0,
            "gift": 0, "left": n, "total_operators": 0}


def limited_banner_analysis(pulls: int, limited_rate: float = BANNER_RATE_LIMITED,
                            rate_up_total: float = BANNER_RATE_UP_TOTAL,
                            spark: bool = True,
                            cfg: GachaConfig = DEFAULT_CONFIG) -> dict:
    """分析限定池：n 抽能拿几个角色、拿下当期限定的概率。

    模型：
      * 每抽出 6 星的概率按 hazard 保底机制（skip 已有实现）。
      * 出 6 星后，该 6 星是当期限定的概率为 limited_rate（默认 35%）；
        是“陪跑 UP”的概率为 rate_up_total - limited_rate（默认 35%）；
        剩下 30% 歪其它任选 6 星。
      * 300 井：300 张兑换点数可兑换当期限定（一次），
        且 2024-05-01 起累计 300 抽额外赠送一份当期限定 —— 所以 ≥300 抽时必定拿到。
        两者都只针对**当期**限定，兑换点数交换所在关闭后不再保留。
    """
    n = max(0, int(pulls))
    if n <= 0:
        return {
            "pulls": 0, "expected_six": 0.0, "p_at_least_1_six": 0.0,
            "p_limited": 0.0, "expected_limited": 0.0, "expected_other_up": 0.0,
            "exp_rate_up": 0.0, "need_spark": CONTRACT_COST_PER_SPARK,
            "spark_ready": False, "spark_short": CONTRACT_COST_PER_SPARK,
            "spark_by_gift": False,
        }

    ev = expected_pulls(cfg)
    expected_six = n / ev
    p1 = p_at_least_six(n, 1, cfg)
    other_up_rate = max(0.0, rate_up_total - limited_rate)

    # 精确算“至少 1 个限定”：P(不到 300 抽时靠抽卡拿到) ；到 300 抽直接必定拿到
    if spark and n >= NEW_PITY_SPARK_THRESHOLD:
        p_limited = 1.0
        spark_by_gift = True
    else:
        p_limited = _p_at_least_one_specific(n, limited_rate, cfg)
        spark_by_gift = False

    return {
        "pulls": n,
        "expected_six": expected_six,
        "p_at_least_1_six": p1,
        "p_limited": p_limited,
        "expected_limited": expected_six * limited_rate,
        "expected_other_up": expected_six * other_up_rate,
        "exp_rate_up": expected_six * rate_up_total,
        "need_spark": CONTRACT_COST_PER_SPARK,
        "spark_ready": n >= CONTRACT_COST_PER_SPARK,
        "spark_short": max(0, CONTRACT_COST_PER_SPARK - n),
        "spark_by_gift": spark_by_gift,
    }


def _six_count_distribution(n: int, cfg: GachaConfig = DEFAULT_CONFIG) -> list[float]:
    """返回长度为 n+1 的列表，d[k] = n 抽内恰好出 k 个 6 星的概率（精确 DP）。"""
    if n <= 0:
        return [1.0]
    # 状态：j = 已出 6 星数（截断到 n），c = 距上次出的抽数
    hard = cfg.hard_pity
    cur: dict[tuple[int, int], float] = {(0, 0): 1.0}
    for _ in range(n):
        nxt: dict[tuple[int, int], float] = {}
        for (j, c), pr in cur.items():
            p = cfg.hazard(c)
            if p > 0.0:
                key = (j + 1 if j + 1 <= n else n, 0)
                nxt[key] = nxt.get(key, 0.0) + pr * p
            if p < 1.0:
                nc = c + 1 if c + 1 < hard else hard - 1
                key = (j, nc)
                nxt[key] = nxt.get(key, 0.0) + pr * (1.0 - p)
        cur = nxt
    dist = [0.0] * (n + 1)
    for (j, _c), pr in cur.items():
        dist[j] += pr
    return dist


def _p_at_least_one_specific(n: int, rate: float, cfg: GachaConfig = DEFAULT_CONFIG) -> float:
    """n 抽内至少拿到 1 个“指定的某类 6 星”（每个 6 星有 rate 概率属于该类）的概率。

    P(一个都没有) = Σ_k P(出 k 个 6 星) × (1 - rate)^k
    """
    if n <= 0:
        return 0.0
    if rate <= 0.0:
        return 0.0
    if rate >= 1.0:
        return 1.0
    dist = _six_count_distribution(n, cfg)
    miss = 1.0 - rate
    p_none = sum(prob * (miss ** k) for k, prob in enumerate(dist))
    return min(1.0, max(0.0, 1.0 - p_none))


def monthly_float_resources(start: "_dt.date", end: "_dt.date",
                            veteran: bool = True) -> dict:
    """统计 [start, end] 区间内各月的浮动资源（高级货币估算 + 抽卡资源参考值）。

    只统计**完整跨过或部分落在**区间内的自然月，按月份归类到
    MONTHLY_EVENT_ESTIMATES 的档位。高级货币按 1 颗 = 180 货币折算成抽数。

    veteran=True 用老玩家口径（复刻无高级货币），False 用新玩家口径。
    返回的 pulls_ref 只是参考量级，**不要**直接加到总抽数上（会与大活动
    赠送重复），所以单独返回。
    """
    if end < start:
        start, end = end, start
    months: list[int] = []
    cursor = start
    while True:
        months.append(cursor.month)
        if cursor.year == end.year and cursor.month == end.month:
            break
        cursor = _add_months(cursor, 1)
    months = sorted(set(months))

    rows: list[dict] = []
    total_veteran = 0
    total_newbie = 0
    for entry in MONTHLY_EVENT_ESTIMATES:
        hit = [m for m in months if m in entry["months"]]
        if not hit:
            continue
        # 命中几个月就按几个月计（各月的估值为同一量级，不做比例拆分，
        # 以免中间月份不在窗口内时把其它月份算少）
        vet = entry["veteran_originite"] * len(hit)
        new = entry["newbie_originite"] * len(hit)
        total_veteran += vet
        total_newbie += new
        rows.append({
            "label": entry["label"],
            "months": hit,
            "veteran_originite": vet,
            "newbie_originite": new,
            "pulls_ref": entry["pulls_ref"],
            "note": entry["note"],
        })

    chosen = total_veteran if veteran else total_newbie
    return {
        "months": months,
        "rows": rows,
        "veteran_originite": total_veteran,
        "newbie_originite": total_newbie,
        "originite": chosen,
        "originite_orundum": chosen * ORIGINITE_TO_ORUNDUM,
        "pulls_from_originite": chosen * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "veteran_pulls": total_veteran * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "newbie_pulls": total_newbie * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "veteran": veteran,
        "fuzz": MONTHLY_ESTIMATE_FUZZ,
    }


def new_annihilation_nodes(start: "_dt.date", end: "_dt.date") -> list["_dt.date"]:
    """返回 [start, end] 期间**跨过**的新每周副本预测节点（每年 4/7/10 月的 15 日）。

    节点取「中旬」的 15 日，只用于判断某个月份是否已经进入窗口，不代表真实
    上线日期。例如今天 10/10、活动 12/01：跨过 10/15 → 1 张。
    """
    if end < start:
        start, end = end, start
    nodes: list["_dt.date"] = []
    for year in range(start.year, end.year + 1):
        for month in NEW_ANNIHILATION_PREDICT_MONTHS:
            node = _dt.date(year, month, NEW_ANNIHILATION_PREDICT_DAY)
            if start <= node <= end:
                nodes.append(node)
    return nodes


def new_annihilation_between(start: "_dt.date", end: "_dt.date") -> dict:
    """保守估计窗口内能拿到几次新每周副本首通，以及合计抽卡货币。

    统计口径与「大活动赠送」一致：算到**活动结束日**为止，只要节点落在
    [今天, 结束日] 内就计入（首通当天即可用，不必等）。
    """
    nodes = new_annihilation_nodes(start, end)
    count = len(nodes)
    orundum = count * NEW_ANNIHILATION_ORUNDUM
    return {
        "nodes": nodes,
        "count": count,
        "orundum": orundum,
        "pulls": orundum / ORUNDUM_PER_PULL,
        "per_map": NEW_ANNIHILATION_ORUNDUM,
        "months": NEW_ANNIHILATION_PREDICT_MONTHS,
        "fuzz": "无历史规律，按每年 3 张保守估计（可能少也可能多）",
    }


def spring_signin_nodes(year: int) -> list[tuple["_dt.date", str]]:
    """某一年春节签到的 4 个发放节点：除夕 / 初一 / 初五 / 初八。

    以农历正月初一为基准，除夕取前一天、初五取 +4 天、初八取 +7 天。
    返回 [(日期, 节点名), ...]，按日期升序。
    """
    if year in SPRING_FESTIVAL_TABLE:
        month, day = SPRING_FESTIVAL_TABLE[year]
        first = _dt.date(year, month, day)
    else:
        first = _spring_festival_by_year(year)
    nodes = [(first + _dt.timedelta(days=offset), label)
             for label, offset in SPRING_SIGNIN_NODES]
    return sorted(nodes, key=lambda item: item[0])


def spring_signin_between(start: "_dt.date", end: "_dt.date") -> dict:
    """统计 [start, end] 期间跨过的春节签到节点，以及合计抽卡货币。

    与「大活动赠送」「新每周副本首通」同一口径：算到活动结束日为止，
    节点当天即可领取，所以用闭区间。
    """
    if end < start:
        start, end = end, start
    hits: list[tuple["_dt.date", str]] = []
    for year in range(start.year, end.year + 1):
        for node, label in spring_signin_nodes(year):
            if start <= node <= end:
                hits.append((node, label))
    orundum = len(hits) * SPRING_SIGNIN_ORUNDUM_EACH
    return {
        "nodes": hits,
        "count": len(hits),
        "orundum": orundum,
        "pulls": orundum / ORUNDUM_PER_PULL,
        "per_node": SPRING_SIGNIN_ORUNDUM_EACH,
        "total_per_year": SPRING_SIGNIN_ORUNDUM_TOTAL,
        "labels": [label for _node, label in hits],
    }


def monthly_estimate_table() -> list[tuple[str, str, str, str]]:
    """把月度浮动表整理成可直接打印的行（月份 / 活动类型 / 老玩家 / 新玩家 / 抽卡参考）。"""
    out: list[tuple[str, str, str, str]] = []
    for entry in MONTHLY_EVENT_ESTIMATES:
        months = entry["months"]
        month_text = f"{months[0]} 月" if len(months) == 1 else f"{months[0]}~{months[-1]} 月"
        originite = (f"{entry['veteran_originite']} 颗" if entry["veteran_originite"]
                     == entry["newbie_originite"]
                     else f"{entry['veteran_originite']} / {entry['newbie_originite']} 颗")
        pulls = f"约 {entry['pulls_ref']:g} 抽" if entry["pulls_ref"] else "视活动而定"
        out.append((month_text, f"{entry['label']}（{entry['note']}）", originite, pulls))
    return out


# ======================= 逐日日历引擎（v3 口径） =======================
# 为什么要有这一层
# ----------------
# 旧版把「每月固定收入」摊薄成「货币/天」再乘天数，活动赠送 / 复刻 / 春节签到又各走
# 一套「按日期跳变」的函数；两套口径永远对不齐（同一段时间能差 24 抽）。
#
# 现在改成**逐日历遍**：从今天开始一天一天往后走，当天该发什么就记一笔。
#   * 目标日算法  ：累加到目标日，看总量；
#   * 目标抽数算法：累加到「总量 ≥ 缺口」的那一天为止。
# 两个算法共用这台引擎，所以不可能再打架。
#
# 每一天的记账规则（用户口径）
# ----------------------------
#   每日      : 100 货币（每日任务）
#   每周一    : +500 货币（周常）、+1800 货币（每周副本作战）
#   每月 1 号 : +2 张单抽券、+600 货币（次级凭证商店）
#   每月 17 号: +1 张单抽券（每月签到）
#   每月 7/15/22/30 号: +200 货币（维护补偿，保守按每月 4 次）
#   每月 15 号: 复刻活动 —— 新玩家 +36 高级货币 +3 抽；老玩家 +2000 货币 +3 抽
#   大版本 14 天（正月初一 / 5.1 / 8.1 / 11.1 起）
#       第 1 天 : +10 高级货币 +3 抽 + 5 高级货币（停机维护）+ 600 货币 + 11 张限时券
#       第 8 天 : +16 高级货币
#       第 15 天: +10 高级货币（活动只到第 14 天，此节点用于「结束次日」的收尾口径）
#       第 2~14 天: 每天 +600 货币 + 1 张限时券
#       开始前 7 天: +1200 货币（前瞻预告）
#   春节签到  : 除夕 / 初一 / 初五 / 初八，各 +500 货币
#   自造货币      : 无月卡 +240 货币/天；有月卡 +320 货币/天（不是 240 + 200）
#   月卡      : +200 货币/天；次月起每自然月买一张，立得 6 高级货币

# ---- 每日 ----
LEDGER_DAILY_ORUNDUM = 100           # 每日任务
LEDGER_MONTHLY_CARD_ORUNDUM = MONTHLY_CARD_ORUNDUM     # 月卡每日 200
# ---- 每周一 ----
LEDGER_WEEKLY_TASK = 500             # 每周任务
LEDGER_WEEKLY_ANNIHILATION = 1800    # 每周副本作战
# ---- 每月 ----
LEDGER_GREEN_DAY = 1                 # 次级凭证商店刷新
LEDGER_GREEN_TICKETS = 3             #   3 张单抽券（+600 货币 = 整店 4 抽）
LEDGER_GREEN_ORUNDUM = 600           #   600 货币
LEDGER_SIGNIN_DAY = 17               # 每月签到券
LEDGER_SIGNIN_TICKETS = 1
LEDGER_FLASH_DAYS = (7, 15, 22, 30)  # 维护补偿：每月保守 4 次
LEDGER_FLASH_ORUNDUM = 200
# ---- 每月复刻（15 号）----
LEDGER_RERUN_DAY = 15
LEDGER_RERUN_NEW_ORIGINITE = 36      # 新玩家：复刻没打过，能拿首通高级货币
LEDGER_RERUN_VETERAN_ORUNDUM = 2000  # 老玩家：打过了，换成抽卡货币
LEDGER_RERUN_TICKETS = 3
# ---- 每月新活动（非大活动月）----
# 除大活动月（正月初一 / 5·1 / 8·1 / 11·1 所在月）以外，每月 10 号有一个新活动，
# 新老玩家统一口径：+28 高级货币 + 3 抽。
LEDGER_NEW_EVENT_DAY = 10
LEDGER_NEW_EVENT_ORIGINITE = 28
LEDGER_NEW_EVENT_TICKETS = 3
# ---- 大版本（每个档期 14 天）----
LEDGER_MAJOR_NODE_ORIGINITE = {1: 10, 8: 16}   # 开幕 / 第 8 天
LEDGER_MAJOR_FINAL_ORIGINITE = 10             # 第 15 天（春节的正月十五 / 5·8·11 月的 15 号）
LEDGER_MAJOR_NODE_TICKETS = 3        # 开幕当天 +3 抽
LEDGER_MAJOR_MAINT_ORIGINITE = 5     # 停机维护补偿
LEDGER_MAJOR_DAY1_ORUNDUM = 600
LEDGER_MAJOR_DAY1_TIMED = 11         # 开幕 11 张限时券
LEDGER_MAJOR_DAILY_ORUNDUM = 600     # 第 2~14 天每天 600 货币
LEDGER_MAJOR_DAILY_TIMED = 1         # 第 2~14 天每天 1 张限时券
LEDGER_PREVIEW_DAYS = 7              # 前瞻预告：开始前 7 天
LEDGER_PREVIEW_ORUNDUM = 1200

# 记账分类（前端照着分组渲染，别再各写一套口径）
LEDGER_CATS: tuple[str, ...] = ("日常", "周期", "月度", "活动")


def _major_window_of(day: "_dt.date"):
    """day 落在哪个大版本窗口里 → (name, begin, 第几天)；不在窗口内返回 None。"""
    for name, month, mday, _cls in MAJOR_EVENT_SPECS:
        for year in (day.year - 1, day.year, day.year + 1):
            begin = _dt.date(year, month, mday)
            if begin <= day <= begin + _dt.timedelta(days=EVENT_DURATION_DAYS - 1):
                return name, begin, (day - begin).days + 1
    for year in (day.year - 1, day.year, day.year + 1):
        begin = _spring_festival_by_year(year)
        if begin <= day <= begin + _dt.timedelta(days=EVENT_DURATION_DAYS - 1):
            return "春节", begin, (day - begin).days + 1
    return None


def major_timed_live(day: "_dt.date", start: "_dt.date | None" = None) -> int:
    """day 当天仍然有效的限时单抽券数量（封顶 24）。

    大版本第 1 天发 11 张、之后每天 1 张，所以第 n 天累计 n + 10 张。

    **有效期到第 15 天**（春节是正月十五、其余是当月 15 号），不是第 14 天：
    卡池最后一天仍能抽、券也仍然能砸进去，第 16 天才算作废。
    以前按 14 天判「已结束」，于是「攒到正月十五」这类目标会把整档 24 张券算丢
    —— 同一份资源比自己手算少 24 抽，就是这里来的。

    传了 start 就**只算 start 之后发的**：活动进行中被问到（例如 11/7 问 11/14）时，
    前面那几天发的券早就用掉了，不该当成「现在手上还有」。
    """
    info = _major_window_of(day)
    if info:
        _name, begin, index = info
    else:
        # 第 15 天：14 天档期已过，但券还在这一期卡池的有效期内
        tail = _major_window_of(day - _dt.timedelta(days=EVENT_DURATION_DAYS))
        if not tail or tail[2] != 1:
            return 0
        _name, begin = tail[0], tail[1]
        index = EVENT_DURATION_DAYS + 1

    first = max(begin, start) if start else begin
    from_index = (first - begin).days + 1
    # 和账本保持同一口径：start 当天的「每日份」不算（今天那张已经用掉了），
    # 但开幕那天的 11 张是节点，当天仍在手上。
    if start and first == start and from_index >= 2:
        from_index += 1
    total = 0
    for k in range(from_index, index + 1):
        total += LEDGER_MAJOR_DAY1_TIMED if k == 1 else LEDGER_MAJOR_DAILY_TIMED
    return min(total, ACTIVITY_TIMED_PULLS_CAP)


def _major_starts(start: "_dt.date", end: "_dt.date") -> list[tuple[str, "_dt.date"]]:
    """与 [start, end] 相关的大版本档期（按开始日排序）。

    包含**开始日早于 start、但还没结束**的档期 —— 否则「活动进行中」会被当成
    「这段时间没有活动」，进行中那一期的限时券和大版本节点就全丢了。
    """
    out: list[tuple[str, "_dt.date"]] = []
    span = _dt.timedelta(days=EVENT_DURATION_DAYS - 1)
    for year in range(start.year - 1, end.year + 2):
        for name, month, mday, _cls in MAJOR_EVENT_SPECS:
            begin = _dt.date(year, month, mday)
            if begin <= end and begin + span >= start:
                out.append((name, begin))
        begin = _spring_festival_by_year(year)
        if begin <= end and begin + span >= start:
            out.append(("春节", begin))
    return sorted(out, key=lambda item: item[1])


def daily_ledger(start: "_dt.date", end: "_dt.date", *,
                 veteran: bool = True, monthly_card: bool = False,
                 craft_jade: bool = False) -> dict:
    """把 [start, end] 这段时间一天一天走完，逐笔记账，返回明细与合计。

    约定（重要）：
      * **每日类**（日常 / 周常 / 每周副本 / 月卡 / 自造货币）从 start 的**次日**开始计，
        因为今天的日常此刻已经拿到手了；0 天窗口 = 0 收入。
      * **节点类**（1 号次级凭证、17 号签到、15 号复刻、维护、大版本、春节签到、前瞻）
        只要落在 [start, end] 内（含当天）就计，当天就能领。
    """
    if end < start:
        start, end = end, start
    days = (end - start).days

    # 预生成节点表，避免在日循环里反复构造日期
    starts = _major_starts(start, end + _dt.timedelta(days=LEDGER_PREVIEW_DAYS))
    preview = {(begin - _dt.timedelta(days=LEDGER_PREVIEW_DAYS)): name
               for name, begin in starts}
    spring_nodes: dict["_dt.date", str] = {}
    for year in range(start.year - 1, end.year + 2):
        for node, label in spring_signin_nodes(year):
            spring_nodes[node] = label

    # 「大活动月」= 正月初一 / 5·1 / 8·1 / 11·1 所在的自然月；这些月份不再排每月新活动
    major_months: set[tuple[int, int]] = set()
    for year in range(start.year - 1, end.year + 2):
        for _name, month, _mday, _cls in MAJOR_EVENT_SPECS:
            major_months.add((year, month))
        cny = _spring_festival_by_year(year)
        major_months.add((cny.year, cny.month))

    craft = crafted_orundum_per_day(monthly_card) if craft_jade else 0
    start_month = start.year * 12 + start.month
    rows: list[dict] = []

    def add(day, label, cat, orundum=0.0, originite=0, tickets=0, timed=0) -> None:
        rows.append({"date": day, "label": label, "cat": cat,
                     "orundum": float(orundum), "originite": int(originite),
                     "tickets": int(tickets), "timed": int(timed)})

    for i in range(days + 1):
        day = start + _dt.timedelta(days=i)
        elapsed = i >= 1                      # 今天已经过去的日常不再计

        if elapsed:
            add(day, "每日任务", "日常", orundum=LEDGER_DAILY_ORUNDUM)
            if monthly_card:
                add(day, "月卡每日", "日常", orundum=LEDGER_MONTHLY_CARD_ORUNDUM)
            if craft:
                add(day, "自造货币", "日常", orundum=craft)
        if elapsed and day.weekday() == 0:    # 周一
            add(day, "每周任务", "周期", orundum=LEDGER_WEEKLY_TASK)
            add(day, "每周副本作战", "周期", orundum=LEDGER_WEEKLY_ANNIHILATION)

        if day.day == LEDGER_GREEN_DAY:
            add(day, "次级凭证商店", "月度", orundum=LEDGER_GREEN_ORUNDUM,
                tickets=LEDGER_GREEN_TICKETS)
            if monthly_card and (day.year * 12 + day.month) != start_month:
                add(day, "月卡购买", "月度", originite=MONTHLY_CARD_ORIGINITE)
        if day.day == LEDGER_SIGNIN_DAY:
            add(day, "每月签到", "月度", tickets=LEDGER_SIGNIN_TICKETS)
        if day.day in LEDGER_FLASH_DAYS:
            add(day, "维护补偿", "月度", orundum=LEDGER_FLASH_ORUNDUM)

        if day.day == LEDGER_RERUN_DAY:
            if veteran:
                add(day, "复刻活动", "活动",
                    orundum=LEDGER_RERUN_VETERAN_ORUNDUM,
                    tickets=LEDGER_RERUN_TICKETS)
            else:
                add(day, "复刻活动", "活动",
                    originite=LEDGER_RERUN_NEW_ORIGINITE,
                    tickets=LEDGER_RERUN_TICKETS)

        if (day.day == LEDGER_NEW_EVENT_DAY
                and (day.year, day.month) not in major_months):
            add(day, "每月新活动", "活动",
                originite=LEDGER_NEW_EVENT_ORIGINITE,
                tickets=LEDGER_NEW_EVENT_TICKETS)

        info = _major_window_of(day)
        if info:
            name, _begin, index = info
            if index == 1:
                add(day, f"{name}·开幕", "活动", orundum=LEDGER_MAJOR_DAY1_ORUNDUM,
                    originite=LEDGER_MAJOR_NODE_ORIGINITE[1],
                    tickets=LEDGER_MAJOR_NODE_TICKETS,
                    timed=LEDGER_MAJOR_DAY1_TIMED)
                add(day, f"{name}·停机维护", "活动",
                    originite=LEDGER_MAJOR_MAINT_ORIGINITE)
            else:
                # 每日 600 货币 + 1 张券：和日常一样，**今天这一份已经算过了**，从次日起计
                if elapsed:
                    add(day, f"{name}·每日", "活动",
                        orundum=LEDGER_MAJOR_DAILY_ORUNDUM,
                        timed=LEDGER_MAJOR_DAILY_TIMED)
                # 节点高级货币（第 8 天）：当天就能领
                node_originite = LEDGER_MAJOR_NODE_ORIGINITE.get(index, 0)
                if node_originite:
                    add(day, f"{name}·节点", "活动", originite=node_originite)

        # 第 15 天（14 天档期结束后的次日）：春节是正月十五，其余是当月 15 号，
        # 各送 10 高级货币。这一天同时是「每月 15 号复刻」，两笔会叠加。
        prev = _major_window_of(day - _dt.timedelta(days=EVENT_DURATION_DAYS))
        if prev and prev[2] == 1:
            add(day, f"{prev[0]}·收尾", "活动", originite=LEDGER_MAJOR_FINAL_ORIGINITE)

        if day in preview:
            add(day, f"{preview[day]}·前瞻预告", "活动", orundum=LEDGER_PREVIEW_ORUNDUM)

        if day in spring_nodes:
            add(day, f"春节签到·{spring_nodes[day]}", "活动",
                orundum=SPRING_SIGNIN_ORUNDUM_EACH)

    # ---- 合计 ----
    def blank() -> dict:
        return {"orundum": 0.0, "originite": 0, "tickets": 0, "timed": 0}

    total = blank()
    by_cat = {cat: blank() for cat in LEDGER_CATS}
    by_label: dict[str, dict] = {}
    for row in rows:
        for bucket in (total, by_cat[row["cat"]],
                       by_label.setdefault(row["label"], blank())):
            bucket["orundum"] += row["orundum"]
            bucket["originite"] += row["originite"]
            bucket["tickets"] += row["tickets"]
            bucket["timed"] += row["timed"]

    def to_pulls(bucket: dict) -> float:
        return ((bucket["orundum"] + bucket["originite"] * ORIGINITE_TO_ORUNDUM)
                / ORUNDUM_PER_PULL + bucket["tickets"])

    for bucket in [total, *by_cat.values(), *by_label.values()]:
        bucket["pulls"] = to_pulls(bucket)

    # ---- 窗口内跨过的大版本 ----
    majors: list[dict] = []
    for name, begin in starts:
        finish = begin + _dt.timedelta(days=EVENT_DURATION_DAYS - 1)
        if finish < start or begin > end:
            continue
        first, last = max(begin, start), min(finish, end)
        if last < first:
            continue
        lo = (first - begin).days + 1
        hi = (last - begin).days + 1
        timed = sum(LEDGER_MAJOR_DAY1_TIMED if k == 1 else LEDGER_MAJOR_DAILY_TIMED
                    for k in range(lo, hi + 1))
        majors.append({
            "name": name, "begin": begin, "end": finish,
            "days_in_window": hi - lo + 1,
            "orundum": LEDGER_MAJOR_DAY1_ORUNDUM * (hi - lo + 1),
            "originite": sum(LEDGER_MAJOR_NODE_ORIGINITE.get(k, 0) for k in range(lo, hi + 1))
                         + (LEDGER_MAJOR_FINAL_ORIGINITE
                            if start <= begin + _dt.timedelta(days=EVENT_DURATION_DAYS) <= end
                            else 0),
            "timed": timed,
        })

    card_rows = [r for r in rows if r["label"] == "月卡购买"]
    timed_live = major_timed_live(end, start)
    free_pulls = total["pulls"]
    return {
        "start": start, "end": end, "days": days,
        "rows": rows,
        "by_cat": by_cat,
        "by_label": by_label,
        "majors": majors,
        "orundum": total["orundum"],
        "originite": total["originite"],
        "tickets": total["tickets"],
        "timed": total["timed"],
        "orundum_pulls": total["orundum"] / ORUNDUM_PER_PULL,
        "originite_pulls": total["originite"] * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "ticket_pulls": total["tickets"],
        "free_pulls": free_pulls,
        "timed_total": total["timed"],
        "timed_live": timed_live,
        "total_pulls": free_pulls + timed_live,
        "card_months": len(card_rows),
        "card_originite": sum(r["originite"] for r in card_rows),
        "card_cost": len(card_rows) * MONTHLY_CARD_PRICE,
        "card_pulls": sum(r["originite"] for r in card_rows)
                      * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
    }


def period_bonus(start: "_dt.date", end: "_dt.date", *,
                 yellow: int = 0, yellow_base: int = 0, veteran: bool = True,
                 monthly_card: bool = False, craft_jade: bool = False,
                 consumed_this_month: int = 0) -> dict:
    """[start, end] 期间除「现有资源」之外还能到手的抽数 —— 攒抽预测与活动预估的唯一口径。

    现在完全由 `daily_ledger()` 逐日历遍得出，不再有「按天摊薄 × 天数」这种近似。
    返回的字典保留旧键名（originite / reward_* / spring_* / timed_* …），
    但值都来自账本，另外多给一个 `ledger` 供前端做分类明细。
    """
    if end < start:
        start, end = end, start
    led = daily_ledger(start, end, veteran=veteran, monthly_card=monthly_card,
                       craft_jade=craft_jade)
    months = month_refreshes_between(start, end)
    y_plan = yellow_exchange_plan(yellow, months,
                                 consumed_this_month=consumed_this_month)
    y_extra = max(0, y_plan["total_pulls"] - max(0, yellow_base))

    by_cat = led["by_cat"]
    by_label = led["by_label"]
    act = by_cat["活动"]
    income_pulls = sum(by_cat[c]["pulls"] for c in ("日常", "周期", "月度"))
    activity_pulls = act["pulls"]
    timed_raw = led["timed"]
    timed = led["timed_live"]
    pulls = y_extra + led["free_pulls"] + timed

    # 细项（显示用）：大版本货币、春节签到、复刻
    major_orundum = sum(r["orundum"] for k, r in by_label.items() if "·" in k)
    spring = by_label.get("春节签到·除夕", None)
    spring_labels = [k.split("·", 1)[1] for k in by_label if k.startswith("春节签到·")]
    spring_orundum = sum(r["orundum"] for k, r in by_label.items()
                         if k.startswith("春节签到·"))
    rerun = by_label.get("复刻活动", blank_bucket())
    card = monthly_card_plan(start, end) if monthly_card else monthly_card_plan(start, start)

    return {
        "days": (end - start).days,
        "ledger": led,
        "by_cat": by_cat,
        "by_label": by_label,
        "yellow": y_extra,
        "yellow_plan": y_plan,
        "card": card,
        "card_pulls": led["card_pulls"],
        "card_cost": led["card_cost"],
        "card_originite": led["card_originite"],
        "originite": act["originite"] + by_cat["月度"]["originite"]
                     - led["card_originite"],
        "originite_orundum": (act["originite"] + by_cat["月度"]["originite"]
                              - led["card_originite"]) * ORIGINITE_TO_ORUNDUM,
        "originite_pulls": (act["originite"] + by_cat["月度"]["originite"]
                            - led["card_originite"]) * ORIGINITE_TO_ORUNDUM / ORUNDUM_PER_PULL,
        "rerun_pulls": rerun["pulls"],
        "rerun_originite": rerun["originite"],
        "rerun_orundum": rerun["orundum"],
        "spring_count": len(spring_labels),
        "spring_labels": spring_labels,
        "spring_orundum": spring_orundum,
        "spring_pulls": spring_orundum / ORUNDUM_PER_PULL,
        "reward_orundum": major_orundum,
        "reward_pulls": major_orundum / ORUNDUM_PER_PULL,
        "activity_pulls": activity_pulls,
        "timed_pulls": timed,
        "timed_raw": timed_raw,
        "income_pulls": income_pulls,
        "pulls": pulls,
        "orundum": pulls * ORUNDUM_PER_PULL,
    }


def blank_bucket() -> dict:
    """空账本桶（给 by_label.get 的默认值用，避免 KeyError）。"""
    return {"orundum": 0.0, "originite": 0, "tickets": 0, "timed": 0, "pulls": 0.0}


def event_calc(args) -> dict:
    """活动预估 / 目标日算法：从今天逐日走到**目标日**，看能攒多少抽。

    统计截止：官方档期算到活动最后一天（14 天里的赠送、免费单抽都能砸进这期卡池）；
    自定义日期就是那一天本身，不往后推。

    所有收入都由 `daily_ledger()` 一天一天记出来，不再有「货币/天 × 天数」的近似，
    所以「攒 X 抽要多久」和「攒到某天有多少」用的是同一份账，数字不可能打架。

    资源分两类：
      **自由资源**：凭证 + 抽卡货币 + 高级货币 + 兑换凭证 + 日常/周期/月度/活动进账。不会过期。
      **限时资源**：大版本送的限时单抽券，只在当期活动内有效，过期作废。
    """
    today = _dt.date.today()
    ev = resolve_event(getattr(args, "event", "") or "", today)
    event_begin = ev["date"]
    # 目标日不能早于今天：填了过去的日期时按「今天」处理，否则 days 为负、
    # 常规收入会算成负数（以前会直接吐出「-70 抽」）。
    raw_end = ev["end_date"]
    event_end = max(today, raw_end)
    ev["end_date_raw"] = raw_end
    ev["end_date"] = event_end          # 写入钳制后的日期，前端与 CLI 看到的才一致
    ev["clamped"] = event_end != raw_end
    days = (event_end - today).days
    days_to_begin = ev["days"]

    monthly_card = bool(getattr(args, "monthly_card", False))
    craft_jade = bool(getattr(args, "craft_jade", False))
    veteran = not bool(getattr(args, "new_player", False))
    use_originite = not bool(getattr(args, "no_originite", False))
    extra_orundum = int(getattr(args, "extra", 0) or 0)
    profile = getattr(args, "profile", DEFAULT_PLAYER_PROFILE)

    # ---- 现有资源（不含兑换凭证，兑换凭证单独按月处理）----
    r = pulls_from_resources(
        orundum=getattr(args, "orundum", 0), originite=getattr(args, "originite", 0),
        tickets=getattr(args, "tickets", 0), ten_tickets=getattr(args, "ten", 0),
        use_originite=use_originite, originite_keep=getattr(args, "keep", 0),
    )
    base_pulls = r["total_pulls"]

    # ---- 兑换凭证：按「每月最多 258 票 = 38 抽」拆分本月 / 之后各月 ----
    # 本月已经换到某档时，那个档位整段跳过（不再重复发一次）。
    yellow = getattr(args, "yellow", 0)
    stage = getattr(args, "yellow_stage", 10)
    consumed = yellow_stage_to_consumed(stage)
    month_count = month_refreshes_between(today, event_end)
    y_plan = yellow_exchange_plan(yellow, month_count, consumed_this_month=consumed)
    yellow_total = y_plan["total_pulls"]

    # ---- 逐日账本：日常 / 周期 / 月度 / 活动 ----
    led = daily_ledger(today, event_end, veteran=veteran,
                       monthly_card=monthly_card, craft_jade=craft_jade)
    by_cat = led["by_cat"]
    timed_live = led["timed_live"]          # 目标日当天仍然有效的限时券
    timed_total = led["timed"]              # 窗口内一共发了多少张（会过期）
    extra_pulls = extra_orundum / ORUNDUM_PER_PULL

    # ---- 合计 ----
    free_pulls = base_pulls + yellow_total + led["free_pulls"] + extra_pulls
    free_int = int(free_pulls)
    pool_pulls = free_int + timed_live       # 概率 / 井位 / 副产物用这个
    total = free_pulls + timed_total
    total_int = int(total)
    upper = total + VARIANCE_EXTRA_PULLS

    # ---- 每月固定收入（展示用，与账本的「日常/周期/月度」同源）----
    income = income_breakdown(
        monthly_card=monthly_card,
        od_per_run=getattr(args, "od_per_run", DEFAULT_ORUNDUM_PER_RUN),
        signin_tickets=DEFAULT_SIGNIN_TICKETS if getattr(args, "signin", True) else 0,
        green_tickets=DEFAULT_GREEN_TICKETS if getattr(args, "green", True) else 0,
        green_orundum=DEFAULT_GREEN_ORUNDUM if getattr(args, "green", True) else 0,
        maintenance_orundum=DEFAULT_MAINTENANCE_ORUNDUM if getattr(args, "maintenance", True) else 0,
        maintenance_originite=0,
    )

    craft_orundum = (crafted_orundum_per_day(monthly_card) * days) if craft_jade else 0.0
    craft_pulls = craft_orundum / ORUNDUM_PER_PULL

    card_plan = {"months": led["card_months"], "cost": led["card_cost"],
                 "originite": led["card_originite"], "orundum":
                 led["card_originite"] * ORIGINITE_TO_ORUNDUM,
                 "pulls": led["card_pulls"],
                 "labels": monthly_card_plan(today, event_end)["labels"] if monthly_card else [],
                 "price_each": MONTHLY_CARD_PRICE,
                 "originite_each": MONTHLY_CARD_ORIGINITE}

    # ---- 抽卡概率 / 井位 / 副产物 ----
    spark = not getattr(args, "no_spark", False)
    analysis = limited_banner_analysis(pool_pulls, spark=spark)
    analysis_lower = limited_banner_analysis(free_int, spark=spark)
    tiers = spark_tiers(pool_pulls)
    to_spark = CONTRACT_COST_PER_SPARK - pool_pulls
    to_spark_upper = CONTRACT_COST_PER_SPARK - int(pool_pulls + VARIANCE_EXTRA_PULLS)
    can_spark = to_spark <= 0
    can_spark_upper = to_spark_upper <= 0
    is_major_node = _major_window_of(event_end) is not None

    # ---- 分类明细（前端照着渲染，加总 = pool_pulls）----
    breakdown = [
        {"label": "现有资源", "pulls": base_pulls,
         "detail": "抽卡货币 / 高级货币 / 单抽券"},
        {"label": "兑换凭证兑换", "pulls": yellow_total,
         "detail": f"{yellow} 张（本月 {y_plan['now_pulls']}"
                   + (f" + 之后 {y_plan['later_pulls']}" if y_plan["later_pulls"] else "")
                   + "）"},
        {"label": "日常", "pulls": by_cat["日常"]["pulls"],
         "detail": f"{days} 天 × 100 货币"
                   + ("，含月卡每日 200 货币" if monthly_card else "")
                   + (f"，含自造货币 {crafted_orundum_per_day(monthly_card)} 货币/天" if craft_jade else "")},
        {"label": "周期", "pulls": by_cat["周期"]["pulls"],
         "detail": "每周一：周常 500 + 每周副本 1800"},
        {"label": "月度", "pulls": by_cat["月度"]["pulls"],
         "detail": "1 号次级凭证 600+2券 / 17 号签到券 / 7·15·22·30 号维护 200"
                   + (f" / 月卡高级货币 {led['card_originite']} 颗" if led["card_originite"] else "")},
        {"label": "活动", "pulls": by_cat["活动"]["pulls"],
         "detail": "大版本节点 / 每月 15 号复刻 / 开始前 7 天前瞻 / 春节签到"},
    ]
    if extra_orundum:
        breakdown.append({"label": "额外一次性", "pulls": extra_pulls,
                          "detail": f"{extra_orundum:,} 货币"})
    if timed_live:
        breakdown.append({"label": "限时券", "pulls": timed_live,
                          "detail": f"目标日所在档期的限时单抽券（窗口内共发 {timed_total} 张）"})

    # ---- 兼容旧字段 ----
    majors = led["majors"]
    rewards = {
        "orundum": sum(m["orundum"] for m in majors),
        "pulls_from_orundum": sum(m["orundum"] for m in majors) / ORUNDUM_PER_PULL,
        "timed_pulls": timed_total,
        "details": [{"name": m["name"], "begin": m["begin"], "end": m["end"],
                     "days_in_window": m["days_in_window"], "orundum": m["orundum"],
                     "timed_pulls": m["timed"], "full_orundum": EVENT_REWARD_ORUNDUM,
                     "full_timed_pulls": EVENT_REWARD_TIMED_PULLS_TOTAL}
                    for m in majors],
    }
    floating = {"originite": led["originite"], "originite_orundum":
                led["originite"] * ORIGINITE_TO_ORUNDUM,
                "pulls_from_originite": led["originite_pulls"],
                "rows": [], "fuzz": MONTHLY_ESTIMATE_FUZZ, "months": []}
    keep_originite = max(0, getattr(args, "originite", 0) - getattr(args, "keep", 0)) \
        if not use_originite else 0

    return {
        "event": ev,
        "income": income,
        "ledger": led,
        "by_cat": by_cat,
        "breakdown": breakdown,
        "majors": majors,
        "base_pulls": base_pulls,
        "have": base_pulls + y_plan["now_pulls"],
        "gained": sum(by_cat[c]["pulls"] for c in ("日常", "周期", "月度")),
        "rewards": rewards,
        "reward_pulls": rewards["pulls_from_orundum"],
        "floating": floating,
        "float_pulls": led["originite_pulls"],
        "annihilation": {"count": 0, "orundum": 0, "pulls": 0, "nodes": []},
        "ann_orundum": 0,
        "ann_pulls": 0.0,
        "spring_orundum": sum(r["orundum"] for k, r in led["by_label"].items()
                              if k.startswith("春节签到·")),
        "spring_pulls": sum(r["orundum"] for k, r in led["by_label"].items()
                            if k.startswith("春节签到·")) / ORUNDUM_PER_PULL,
        "rerun_orundum": sum(r["orundum"] for k, r in led["by_label"].items()
                             if k == "复刻活动"),
        "rerun_originite": sum(r["originite"] for k, r in led["by_label"].items()
                               if k == "复刻活动"),
        "rerun_pulls": led["by_label"].get("复刻活动", blank_bucket())["pulls"],
        "spark_summary": spark_summary(pool_pulls),
        "spring_signin": {
            "count": len([k for k in led["by_label"] if k.startswith("春节签到·")]),
            "labels": [k.split("·", 1)[1] for k in led["by_label"]
                       if k.startswith("春节签到·")],
            "orundum": sum(r["orundum"] for k, r in led["by_label"].items()
                           if k.startswith("春节签到·")),
            "pulls": sum(r["orundum"] for k, r in led["by_label"].items()
                         if k.startswith("春节签到·")) / ORUNDUM_PER_PULL,
        },
        "variance_pulls": VARIANCE_EXTRA_PULLS,
        "veteran": veteran,
        "craft_jade": craft_jade,
        "craft_orundum": craft_orundum,
        "craft_pulls": craft_pulls,
        "monthly_card": card_plan,
        "card_months": led["card_months"],
        "card_cost": led["card_cost"],
        "card_originite": led["card_originite"],
        "card_pulls": led["card_pulls"],
        # 自由资源明细：卡片按「届时手上还有多少」给，不是「折算掉多少」
        "free_orundum": float(getattr(args, "orundum", 0)) + led["orundum"] + extra_orundum,
        "free_originite": float(led["originite"] + keep_originite),
        "free_tickets_pulls": float(max(0, getattr(args, "tickets", 0))),
        "free_ten_pulls": float(max(0, getattr(args, "ten", 0))) * 10,
        "existing_originite_pulls": r["from_originite"],
        "free_pulls": free_pulls,
        "free_int": free_int,
        "timed_pulls": timed_live,
        "timed_total": timed_total,
        "total": total,
        "total_int": total_int,
        "upper": upper,
        "days": days,
        "days_to_begin": days_to_begin,
        "yellow_info": y_plan["batches"][0] if y_plan["batches"] else yellow_pulls(yellow),
        "yellow_plan": y_plan,
        "yellow_stage": stage,
        "yellow_consumed": consumed,
        "analysis": analysis,
        "analysis_lower": analysis_lower,
        "spark_tiers": tiers,
        "spark_tiers_lower": tiers,
        "spark_tiers_now": tiers,
        "byproducts": pulls_byproducts(pool_pulls, profile=profile),
        "pool_pulls": pool_pulls,
        "pool_extra": timed_live,
        "is_activity": is_major_node,
        "to_spark": to_spark,
        "to_spark_upper": to_spark_upper,
        "can_spark": can_spark,
        "can_spark_upper": can_spark_upper,
        "spark": spark,
    }


# ----------------------------- 模拟 -----------------------------

def simulate(pulls: int, want: int = 1, trials: int = 100_000, seed: int | None = None,
             cfg: GachaConfig = DEFAULT_CONFIG) -> dict:
    """蒙特卡洛模拟 pulls 抽的结果（与 pull_distribution 使用同一套 hazard 规则）。"""
    rng = random.Random(seed)
    hit_count = 0
    total_six = 0
    total_pulls_used = 0
    for _ in range(trials):
        since_six = 0
        six = 0
        for _m in range(pulls):
            if rng.random() < cfg.hazard(since_six):
                six += 1
                since_six = 0
            else:
                since_six += 1
        total_six += six
        total_pulls_used += pulls
        if six >= want:
            hit_count += 1
    return {
        "trials": trials,
        "pulls": pulls,
        "want": want,
        "hit_rate": hit_count / trials,
        "avg_six": total_six / trials,
        "avg_pulls_per_six": (total_pulls_used / total_six) if total_six else float("inf"),
    }


# ----------------------------- 输出工具 -----------------------------

def fmt_table(rows: list[tuple[str, str]], title: str = "") -> str:
    """键值对表格：两栏对齐，键右补空格，冒号对齐。"""
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows)
    lines = []
    if title:
        lines.append(f"【{title}】")
    for k, v in rows:
        lines.append(f"  {k.ljust(width)} : {v}")
    return "\n".join(lines)


# ----------------------------- 排版工具 -----------------------------
# 目标：结论先行、数字对齐、少用长句。所有区块宽度统一为 RULE_WIDTH。
RULE_WIDTH = 62


def rule(char: str = "─", width: int = RULE_WIDTH) -> str:
    return char * width


def banner(title: str, subtitle: str = "") -> str:
    """醒目大标题（用于每个功能最顶部的结论区）。"""
    lines = [rule("=")]
    lines.append(f"  {title}")
    if subtitle:
        lines.append(f"  {subtitle}")
    lines.append(rule("="))
    return "\n".join(lines)


def section(title: str, tag: str = "") -> str:
    """小节标题行，例如「▌资源构成」。"""
    head = f"▌{title}"
    if tag:
        head += f"　（{tag}）"
    return head


def _visual_width(text: str) -> int:
    """粗算显示宽度：全角字符按 2 计，用于命令行里的对齐。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visual_width(text))


def bar(ratio: float, cap: float, width: int = 20) -> str:
    """进度条：ratio / cap 的比例画成 █░ 条（用于「差多少抽到 300 井」这类直观判断）。"""
    if cap <= 0:
        return ""
    filled = int(round(max(0.0, min(1.0, ratio / cap)) * width))
    return "█" * filled + "░" * (width - filled)


def big_number(value: str, unit: str = "") -> str:
    """结论区的主数字，单位跟在后面。"""
    return f"{value}{unit}"


def key_block(rows: list[tuple[str, str]], width: int = 14) -> str:
    """结论区的两栏块：左列固定宽度，右列左对齐，比 fmt_table 更适合放主结论。"""
    return "\n".join(f"  {_pad(k, width)} {v}" for k, v in rows)


def table(header: tuple[str, ...], rows: list[tuple[str, ...]],
          aligns: str | None = None) -> str:
    """真正的多列表格（带分隔线），用于「逐月明细」这类一行多列的数据。

    aligns: 每列对齐方式，'l' 左对齐 / 'r' 右对齐，默认除首列外都右对齐。
    宽度按“显示宽度”算（中文按 2），所以中文列也能对齐。
    """
    if not rows:
        return ""
    cols = len(header)
    aligns = aligns or ("l" + "r" * (cols - 1))
    widths = [_visual_width(h) for h in header]
    for r in rows:
        # 行可以比表头短（例如「无法达成」这种没有全部数据的行），按表头列数截断/补空，
        # 别直接 r[i] 越界 —— 原来这里会抛 IndexError: tuple index out of range
        for i in range(min(cols, len(r))):
            widths[i] = max(widths[i], _visual_width(str(r[i])))

    def render(cells: tuple[str, ...]) -> str:
        cells = tuple(str(c) for c in cells) + ("",) * max(0, cols - len(cells))
        parts = []
        for i, text in enumerate(cells[:cols]):
            gap = " " * max(0, widths[i] - _visual_width(text))
            left = aligns[i] == "l" if i < len(aligns) else i == 0
            parts.append(text + gap if left else gap + text)
        return "  " + "  ".join(parts).rstrip()

    lines = [render(header)]
    lines.append("  " + "  ".join("─" * w for w in widths))
    for r in rows:
        lines.append(render(r))
    return "\n".join(lines)


def note(lines: str | list[str], indent: str = "  ") -> str:
    """小字说明：自动加前缀，避免正文里出现一长串没有层次的句子。"""
    if isinstance(lines, str):
        lines = [lines]
    return "\n".join(f"{indent}· {line}" for line in lines)


def warn(lines: str | list[str], indent: str = "  ") -> str:
    """⚠️ 免责 / 口径提醒。"""
    if isinstance(lines, str):
        lines = [lines]
    return "\n".join(f"{indent}⚠️ {line}" for line in lines)


def _ticket_detail(tickets: int, ten: int) -> str:
    """凭证明细：0 的那一项不显示，避免「5 张 / 0 张」这种废话。"""
    parts = []
    if tickets:
        parts.append(f"单抽券 {tickets} 张")
    if ten:
        parts.append(f"十连券 {ten} 张")
    return " + ".join(parts) if parts else "无"



def report_resources(orundum: int, originite: int, tickets: int, ten_tickets: int,
                     use_originite: bool, originite_keep: int,
                     current_pity: int, cfg: GachaConfig, yellow: int = 0,
                     yellow_stage: int = 10, craft_jade: bool = False,
                     monthly_card: bool = False) -> str:
    """资源折算报表。

    yellow_stage：本月兑换凭证已经换到哪个档位（填档位对应的票数，如 10 / 18 / 40 / 70 / 120）。
      默认 10 表示本月还没换过，从第 1 档开始领。
    craft_jade：是否把自造货币收入也算进去（月卡党 330 货币/天，无月卡 250 货币/天）。
    """
    r = pulls_from_resources(orundum, originite, tickets, ten_tickets, use_originite, originite_keep)
    stage_state = yellow_month_state(yellow, consumed_this_month=yellow_stage_to_consumed(yellow_stage))
    y_pulls_total = stage_state["claim_pulls"]
    total = r["total_pulls"] + y_pulls_total

    # ============ 一、结论（放最前面） ============
    ev = expected_pulls(cfg)
    lines = [banner("资源折算 · 我现在能抽多少？")]
    lines.append(key_block([
        ("合计可抽", f"{total} 抽"),
        ("6 星期望", f"{total / ev:.2f} 个（平均 {ev:.2f} 抽/个）" if total else "—"),
    ]))
    if total:
        lines.append(key_block([
            ("至少 1 个", f"{p_at_least_six(total, 1, cfg) * 100:5.1f}%"),
            ("至少 2 个", f"{p_at_least_six(total, 2, cfg) * 100:5.1f}%"),
            ("至少 3 个", f"{p_at_least_six(total, 3, cfg) * 100:5.1f}%"),
        ]))
    if current_pity:
        left = max(0, cfg.hard_pity - current_pity)
        lines.append(f"  已垫 {current_pity} 抽，距硬保底还剩 {left} 抽。")
    lines.append("")

    # ============ 二、抽数从哪来 ============
    rows: list[tuple[str, str, str]] = [
        ("抽卡货币", f"{orundum:,} 货币",
         f"{orundum // ORUNDUM_PER_PULL} 抽"
         + (f"（余 {orundum % ORUNDUM_PER_PULL} 货币）" if orundum % ORUNDUM_PER_PULL else "")),
        ("高级货币", f"{r['originite_used']} 颗 × {ORIGINITE_TO_ORUNDUM}",
         f"{r['from_originite']} 抽"),
        ("凭证 / 十连券", _ticket_detail(tickets, ten_tickets), f"{r['from_tickets']} 抽"),
    ]
    if yellow:
        rows.append(("兑换凭证（本月可领）", f"{yellow} 票 → {y_pulls_total} 抽",
                     f"{y_pulls_total} 抽"))
    rows.append(("合计", "—", f"{total} 抽"))
    lines.append(section("抽数从哪来"))
    lines.append(table(("资源", "明细", "折合"), rows,
                       "l" + "l" * (len(rows[0]) - 2) + "r"))
    lines.append("")

    if yellow:
        # 兑换凭证这一块单独讲清楚：本月能领什么、卡在哪、剩下的要等几个月
        claimed = stage_state["claimed"]
        detail = " + ".join(f"{c}票→{g}抽" for c, g in claimed) if claimed else "本月没有可领的档位"
        lines.append(section("兑换凭证怎么换"))
        lines.append(key_block([
            ("本月可领", f"{y_pulls_total} 抽（{detail}）"),
            ("本月已用", f"{stage_state['month_spent']} / {YELLOW_MONTHLY_LIMIT} 票"),
        ], width=10))
        if stage_state["block_reason"] == "month_limit":
            lines.append(f"  ⚠️ 本月额度已满，剩下 {stage_state['capacity_left']} 票要等 1 号 4:00 刷新。")
        elif stage_state["block_reason"] == "no_tickets":
            lines.append("  ⚠️ 票数不足下一档，本月换不到更多。")
        full = yellow_exchange_until_done(
            yellow, consumed_this_month=yellow_stage_to_consumed(yellow_stage))
        if full["months_needed"] > 1 or full["leftover"]:
            per_month = "+".join(str(b["pulls"]) for b in full["batches"])
            tail = f"余 {full['leftover']} 张换不动" if full["leftover"] else "刚好换完"
            lines.append(f"  全部换完可得 {full['total_pulls']} 抽（{per_month}），"
                         f"需要 {full['months_needed']} 次刷新，{tail}。")
        elif y_pulls_total >= YELLOW_FULL_PULLS and not full["leftover"]:
            lines.append(f"  已换满：{YELLOW_FULL_COST} 票 = {YELLOW_FULL_PULLS} 抽。")
        lines.append("")

    if craft_jade:
        per_day = crafted_orundum_per_day(monthly_card)
        lines.append(section("自造货币（可选）"))
        lines.append(f"  {per_day} 货币/天 ≈ {per_day / ORUNDUM_PER_PULL:.2f} 抽/天；"
                     f"{craft_jade_detail(monthly_card)}")
        lines.append("  注意：自造货币占用理智，拿去做别的就吃不到这部分。")
        lines.append("")

    lines.append(f"  折算后剩余：{r['leftover_orundum']} 货币")

    # ============ 三、副产物 ============
    if total > 0:
        lines.append("")
        bp = pulls_byproducts(total)
        lines.append(section("这些抽砸下去能回多少票"))
        lines.append(table(("稀有度", "预计数量", "回收"), [
            ("3★", f"{bp['count_3']:.1f} 个", f"次级凭证 {bp['green']:.0f} 张"),
            ("4★", f"{bp['count_4']:.1f} 个", f"兑换凭证 {bp['yellow_full']['four']:.1f} 张"),
            ("5★", f"{bp['count_5']:.1f} 个", f"兑换凭证 {bp['yellow_full']['five']:.1f} 张"),
            ("6★", f"{bp['count_6']:.2f} 个", f"兑换凭证 {bp['yellow_full']['six']:.1f} 张"),
            ("合计", "—", f"兑换凭证 {bp['yellow_full_total']:.1f} / 次级凭证 {bp['green']:.0f}"),
        ], "lrr"))
        lines.append(f"  {byproduct_value_text(bp['yellow_full_total'])}")

    return "\n".join(lines)


def report_income(income: dict, title: str = "每月稳定收入") -> str:
    """收入明细：按「每天 / 每月」两栏排，最后给折算结果。

    只保留「值 + 单位」，不再逐行写「货币/月」这种重复后缀，数字对齐更好扫。
    """
    rows: list[tuple[str, str, str, str]] = []
    for name, value in income["per_day"]:
        rows.append(("每天", name, f"{value:g}", "货币"))
    rows.append(("小计", "日收入", f"{income['daily_total']:.1f}", "货币/天"))
    for name, value, _text in income["monthly"]:
        rows.append(("每月", name, f"{value:,.0f}", "货币"))
    rows.append(("小计", "月收入", f"{income['monthly_od']:,.0f}", "货币/月"))

    table_text = table(("口径", "项目", "数值", "单位"), rows, "llrr")
    tail = (f"  ≈ {income['monthly_od'] / ORUNDUM_PER_PULL:.1f} 抽/月"
            f"（{income['daily_pulls']:.3f} 抽/天）")
    if income.get("yellow_pulls"):
        tail += f"\n  · 兑换凭证 {income['yellow_pulls']} 抽属于一次性资源，已计入现有资源，不算月收入"
    return f"{section(title)}\n{table_text}\n{tail}"


# ----------------------------- 命令行 -----------------------------

def positive_int(text: str) -> int:
    try:
        value = int(str(text).strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要整数，收到：{text!r}")
    if value < 0:
        raise argparse.ArgumentTypeError("数值不能为负")
    return value


def cmd_count(args) -> int:
    cfg = GachaConfig()
    print(report_resources(
        orundum=args.orundum,
        originite=args.originite,
        tickets=args.tickets,
        ten_tickets=args.ten,
        use_originite=not args.no_originite,
        originite_keep=args.keep,
        current_pity=args.pity,
        cfg=cfg,
        yellow=args.yellow,
        yellow_stage=args.yellow_stage,
        craft_jade=bool(getattr(args, "craft_jade", False)),
        monthly_card=bool(getattr(args, "monthly_card", False)),
    ))
    if getattr(args, "craft_jade", False):
        print()
        print(f"  自造货币口径：{craft_jade_detail(bool(getattr(args, 'monthly_card', False)))}")
        print("  说明：自造货币要占用理智；如果你把理智拿去做别的，这部分收益就吃不到。")
    return 0


def income_block(income: dict) -> str:
    """收入明细 + 口径说明（只保留非显然的几条），返回字符串以便参与排版。"""
    return "\n".join([
        report_income(income),
        note([
            "周任务 / 每周副本按每月 4 周计：500×4 = 2,000、1800×4 = 7,200 货币",
            f"维护补偿 {DEFAULT_MAINTENANCE_ORUNDUM} 货币/月"
            f"（{len(LEDGER_FLASH_DAYS)} 次 × {LEDGER_FLASH_ORUNDUM} 货币，落在每月 7/15/22/30 号）",
            "「更新补偿 5 高级货币」不再摊进月收入 —— 它绑定在每个大版本开服那天",
            f"兑换凭证每月上限 {YELLOW_MONTHLY_LIMIT} 票 = {YELLOW_FULL_PULLS} 抽，"
            f"超出部分要等次月刷新，所以单独算一次性资源",
        ]),
    ])


def _show_income(income: dict) -> None:
    print(income_block(income))
    print()


def cmd_rate(args) -> int:
    r = pulls_from_resources(
        orundum=args.orundum, originite=args.originite,
        tickets=args.tickets, ten_tickets=args.ten,
        use_originite=not args.no_originite, originite_keep=args.keep,
    )
    yellow = getattr(args, "yellow", 0)
    today = _dt.date.today()
    veteran = not bool(getattr(args, "new_player", False))
    consumed = yellow_stage_to_consumed(getattr(args, "yellow_stage", 10))
    y_plan = yellow_exchange_plan(yellow, 0, consumed_this_month=consumed)
    current = r["total_pulls"] + y_plan["now_pulls"]
    out: list[str] = []

    income = income_breakdown(monthly_card=args.monthly_card)
    craft_jade = bool(getattr(args, "craft_jade", False))
    craft_per_day = crafted_orundum_per_day(args.monthly_card) if craft_jade else 0
    daily_total = income["daily_total"] + craft_per_day
    use_card = bool(getattr(args, "monthly_card", False))
    extra = int(getattr(args, "extra", 0) or 0)

    # ============ 一、结论 ============
    out.append(banner("攒抽预测 · 攒到目标还要多久？"))
    out.append(key_block([
        ("现在能抽", f"{current} 抽"),
        ("每天收入", f"{daily_total:.0f} 货币/天 ≈ {daily_total / ORUNDUM_PER_PULL:.2f} 抽/天"),
        ("每月固定", f"{income['monthly_od']:,.0f} 货币/月 ≈ "
                     f"{income['monthly_od'] / ORUNDUM_PER_PULL:.1f} 抽/月"),
    ], width=10))
    if yellow:
        out.append(f"  兑换凭证 {yellow} 张：本月可换 {y_plan['now_pulls']} 抽"
                   + (f"，还有 {y_plan['later_pulls']} 抽要等次月"
                      if y_plan["later_pulls"] else ""))
        if y_plan["leftover"]:
            out.append(f"  另有 {y_plan['leftover']} 张兑换凭证这个月换不动（票不会作废）")
    out.append("")

    # ============ 二、收入构成 ============
    out.append(income_block(income))
    if craft_jade:
        out.append(f"  + 自造货币 {craft_per_day} 货币/天"
                   f"（≈ {craft_per_day / ORUNDUM_PER_PULL:.2f} 抽/天）"
                   f"→ 合计 {daily_total:.0f} 货币/天")
    out.append(note("上面只是「每月固定」那几项；每月 15 号复刻、大版本赠送、"
                    "春节签到等一次性收益在下面的目标明细里逐笔列出。"))
    out.append("")

    # ============ 三、各目标要多久 ============
    # 逐日累加（daily_ledger），加到「现有 + 期间累计 ≥ 目标」那一天为止
    target_rows = []
    detail_rows = []
    for target in args.target:
        days = first_day_reaching(current, target, extra_orundum=extra,
                                  yellow=yellow, yellow_base=y_plan["now_pulls"],
                                  veteran=veteran, monthly_card=use_card,
                                  craft_jade=craft_jade, consumed_this_month=consumed,
                                  start=today)
        if days is None:
            target_rows.append((f"{target} 抽", "无法达成", "—", "—", "—"))
            continue
        if days == 0:
            target_rows.append((f"{target} 抽", "已达成", "—", "—", f"现有 {current} 抽"))
            continue
        date = today + _dt.timedelta(days=days)
        bonus = period_bonus(today, date, yellow=yellow, yellow_base=y_plan["now_pulls"],
                             veteran=veteran, monthly_card=use_card,
                             craft_jade=craft_jade, consumed_this_month=consumed)
        need_orundum = max(0, (target - current) * ORUNDUM_PER_PULL - extra)
        total_have = current + int(round(bonus["pulls"]))
        target_rows.append((
            f"{target} 抽", f"{days} 天", date.strftime("%Y-%m-%d"),
            f"{need_orundum:,.0f} 货币", f"{total_have} 抽",
        ))
        detail_rows.append((target, current, bonus, total_have, date, need_orundum))

    out.append(section("各目标还要多久"))
    out.append(table(("目标", "还需", "预计达成", "缺口", "届时可投入"),
                     target_rows, "rlrlr"))
    out.append(note("「届时可投入」= 现有 + 期间账本上所有进账（含复刻、大版本、限时券、兑换凭证再兑换）"))
    out.append("")

    # ---- 每个目标下面挂一张「这些抽从哪来」的明细 ----
    for target, base, bonus, have, date, need_orundum in detail_rows:
        out.append(f"  ▸ 目标 {target} 抽（{date.strftime('%Y-%m-%d')} 达成）")
        rows = [("现有资源", f"{base} 抽")]
        by_label = bonus["by_label"]
        items = [(k, v) for k, v in by_label.items() if v["pulls"] > 0]
        for label, v in sorted(items, key=lambda kv: -kv[1]["pulls"])[:9]:
            bits = []
            if v["orundum"]:
                bits.append(f"{v['orundum']:,.0f} 货币")
            if v["originite"]:
                bits.append(f"{v['originite']} 颗高级货币")
            if v["tickets"]:
                bits.append(f"{v['tickets']} 张券")
            rows.append((label, " + ".join(bits) + f"（{v['pulls']:.2f} 抽）"))
        if len(items) > 9:
            rest = sum(v["pulls"] for _k, v in sorted(items, key=lambda kv: -kv[1]["pulls"])[9:])
            rows.append(("其它零项", f"{rest:.2f} 抽"))
        if bonus["yellow"]:
            rows.append(("兑换凭证再兑换", f"{bonus['yellow']} 抽（每月 1 日刷新）"))
        if extra:
            rows.append(("额外一次性收入", f"{extra:,} 货币（{extra / ORUNDUM_PER_PULL:.1f} 抽）"))
        if bonus["timed_pulls"]:
            rows.append(("限时单抽券", f"{bonus['timed_pulls']} 张（当天所在档期，活动结束作废）"))
        rows.append(("合计可投入", f"{have} 抽 / 目标 {target} 抽"))
        out.append(table(("来源", "换算"), rows, "lr"))
        out.append("")

    # ============ 四、一年尺度 ============
    cut = today + _dt.timedelta(days=365)
    horizon = period_bonus(today, cut, yellow=yellow, yellow_base=y_plan["now_pulls"],
                           veteran=veteran, monthly_card=use_card,
                           craft_jade=craft_jade, consumed_this_month=consumed)
    extra_notes = []
    if yellow:
        extra_notes.append(f"往后每月兑换凭证还能再换 {horizon['yellow']} 抽"
                           f"（每月上限 {YELLOW_FULL_PULLS} 抽）")
    extra_notes.append(
        f"未来一年账本上还能进 {horizon['pulls']:.0f} 抽："
        f"日常 {horizon['by_cat']['日常']['pulls']:.0f} / "
        f"周期 {horizon['by_cat']['周期']['pulls']:.0f} / "
        f"月度 {horizon['by_cat']['月度']['pulls']:.0f} / "
        f"活动 {horizon['by_cat']['活动']['pulls']:.0f}")
    if horizon["timed_pulls"]:
        extra_notes.append(f"其中限时券 {horizon['timed_pulls']} 张只在当期活动内有效")
    if extra_notes:
        out.append(section("未来一年额外进账"))
        out.append(note(extra_notes))
        out.append("")

    # ============ 五、井位与副产物 ============
    total_pulls = int(round(current + horizon["pulls"]))
    if total_pulls > 0:
        out.append(_spark_table(total_pulls, f"未来一年约 {total_pulls} 抽全砸一个池"))
        out.append("")
        out.append(_byproduct_table(total_pulls))
        out.append("")

    # ============ 六、口径提醒 ============
    out.append(warn([
        "天数按逐日账本一天一天累加得出，不是「货币/天 × 天数」的估算",
        "每月 15 号按一次复刻算（无历史规律，可能多也可能少）",
        "大版本按 5/1、8/1、11/1、正月初一 各 14 天算，实际排期以官方公告为准",
    ]))

    print("\n".join(out))
    return 0


def _spark_table(pulls: int, title: str = "") -> str:
    """井位递减：把 X 抽砸进一个限定池能换到几个角色。

    统一走 `spark_summary()`（前端也用这一个），避免 CLI 和网页在「刚好 300 抽」上
    给出两个不同的数字。
    """
    s = spark_summary(pulls)
    head = section(title or f"这 {pulls} 抽全砸一个池能换什么")
    body = table(("井位", "可换", "兑换点数"), [
        ("300 · 当期限定 6★", f"{s['tier300']} 个", "300/个"),
        ("200 · 解限旧限定", f"{s['tier200']} 个", "200/个"),
        ("75 · 当期 UP 5★", f"{s['tier75']} 个", "75/个"),
    ], "lrr")
    gift = ("含满 300 抽赠送的 1 个" if s["gift"]
            else "不满 300 抽，没有赠送")
    return (f"{head}\n{body}\n"
            f"  共可换 {s['total_operators']} 个角色，剩余兑换点数 {s['left']}"
            f"（{gift}）")


def cmd_event(args) -> int:
    res = event_calc(args)
    ev = res["event"]
    an = res["analysis"]
    today = ev["today"]
    n = res["total_int"]
    upper = int(res["upper"])
    last_day = ev["end_date"]
    free_int = res["free_int"]
    pool_pulls = res["pool_pulls"]
    timed = res["timed_pulls"]
    led = res["ledger"]

    # ============ 一、决策结论 ============
    gap = CONTRACT_COST_PER_SPARK - pool_pulls
    is_target_day = bool(ev.get("is_target"))
    out: list[str] = [banner(f"抽数预估 · {ev['name']}",
                             f"目标：攒到 {last_day.strftime('%Y-%m-%d')}"
                             + ("（目标日）" if is_target_day else "（活动最后一天）"))]
    head = [
        ("本期可抽", f"{pool_pulls} 抽"
                     + (f"（乐观 {pool_pulls + res['variance_pulls']} 抽）"
                        if res["variance_pulls"] else "")),
        ("≈ 井位", f"{min(pool_pulls, CONTRACT_COST_PER_SPARK)} / {CONTRACT_COST_PER_SPARK} "
                   f"{bar(pool_pulls, CONTRACT_COST_PER_SPARK)}"),
        ("至少 1 限定", f"{an['p_limited'] * 100:.1f}%"
                        + ("（井保证）" if an["spark_by_gift"] else "")),
    ]
    if res["card_months"]:
        head.append(("月卡要买",
                     f"{res['card_months']} 张 = {res['card_cost']} 元"
                     f"（立得 {res['card_originite']} 颗高级货币 ≈ {res['card_pulls']:.1f} 抽）"))
    out.append(key_block(head, width=10))

    if res["can_spark"]:
        out.append("  ✅ 够吃井 → 直接下池：抽满 300 换井 + 领 300 抽赠送，当期限定必得")
        out.append(f"     提前出了就收手，能省下 {pool_pulls - CONTRACT_COST_PER_SPARK} 抽给下期")
    elif res["can_spark_upper"]:
        out.append(f"  ⚠️ 卡在井线上：保底差 {gap} 抽，算上浮动有机会够 300")
        out.append("     按「能满井」准备，中途出了当期就收手")
    elif an["p_limited"] >= 0.75:
        out.append(f"  ⚠️ 够不到井（差 {gap} 抽），但把握 {an['p_limited'] * 100:.1f}% 已经不低")
        out.append("     可以下池，但要接受歪掉：歪了就等复刻，别硬追")
    elif an["p_limited"] >= 0.5:
        out.append(f"  ⚠️ 够不到井（差 {gap} 抽），把握 {an['p_limited'] * 100:.1f}% 刚过半")
        out.append("     谨慎下池；这个限定很重要的话，继续攒到能吃井再抽最稳")
    else:
        out.append(f"  ⛔ 够不到井（差 {gap} 抽），把握只有 {an['p_limited'] * 100:.1f}%")
        out.append(f"     建议继续攒到 {CONTRACT_COST_PER_SPARK} 抽吃井，或接受抽不到等复刻")
    out.append("")

    # ============ 二、时间线 ============
    out.append(section("时间线"))
    if ev.get("clamped"):
        out.append(f"  ⚠ 目标日 {ev['end_date_raw'].strftime('%Y-%m-%d')} 早于今天，"
                   f"按今天统计")
    if is_target_day:
        out.append(f"  今天 {today.strftime('%m-%d')} → 目标日 {last_day.strftime('%m-%d')}"
                   f"（{res['days']} 天后）")
    elif today > ev["date"]:
        nth = (today - ev["date"]).days + 1
        out.append(f"  今天 {today.strftime('%m-%d')}：{ev['name']} 进行中（第 {nth} 天）"
                   f"→ 结束 {last_day.strftime('%m-%d')}（还有 {res['days']} 天）")
    else:
        out.append(f"  今天 {today.strftime('%m-%d')} → 开启 {ev['date'].strftime('%m-%d')}"
                   f"（{res['days_to_begin']} 天后）→ 结束 {last_day.strftime('%m-%d')}"
                   f"（{res['days']} 天后，共 {EVENT_DURATION_DAYS} 天）")
    out.append(f"  逐日账本走了 {res['days']} 天，跨 {res['yellow_plan']['months_available']} 次兑换凭证刷新")
    out.append("")

    # ============ 三、抽数从哪来（按账本分类） ============
    yp = res["yellow_plan"]
    out.append(section("抽数从哪来", "自由资源 = 能用；限时资源 = 过期作废"))
    rows: list[tuple[str, str, str]] = [
        ("现有资源", "抽卡货币 / 高级货币 / 单抽券", f"{res['base_pulls']} 抽"),
        ("兑换凭证", f"{args.yellow} 张（本月 {yp['now_pulls']}"
                 + (f" + 之后 {yp['later_pulls']}" if yp["later_pulls"] else "") + "）",
         f"{yp['total_pulls']} 抽"),
    ]
    cat_text = {
        "日常": f"{res['days']} 天 × 100 货币"
                + ("＋月卡 200/天" if res["monthly_card"]["months"] or args.monthly_card else "")
                + ("＋自造货币" if res["craft_jade"] else ""),
        "周期": "每周一：周常 500 + 每周副本 1800",
        "月度": "1 号次级凭证 600+2券 / 17 号签到券 / 7·15·22·30 维护 200"
                + (f" / 月卡高级货币 {led['card_originite']} 颗" if led["card_originite"] else ""),
        "活动": "大版本节点 / 每月 15 号复刻 / 前瞻预告 / 春节签到",
    }
    for cat in LEDGER_CATS:
        v = res["by_cat"][cat]
        if v["pulls"] <= 0:
            continue
        rows.append((f"期间{cat}", cat_text[cat], f"{v['pulls']:.1f} 抽"))
    rows.append(("自由资源", "不会过期，任意卡池", f"{free_int} 抽"))
    if timed:
        rows.append(("限时券", f"目标日所在档期还剩 {timed} 张（窗口内共发 {res['timed_total']} 张）",
                     f"{timed} 抽"))
    rows.append(("合计", f"乐观上界 {upper} 抽" if upper > n else "—", f"{n} 抽"))
    out.append(table(("来源", "明细", "折合"), rows, "llr"))
    out.append(note("这张表由逐日账本直接汇总：上面每类相加 = 自由资源，再加限时券 = 合计"))
    out.append("")

    out.append(income_block(res["income"]))
    out.append("")

    # ============ 四、逐笔明细（按名目聚合） ============
    detail = [(k, v) for k, v in led["by_label"].items() if v["pulls"] > 0 or v["timed"]]
    if detail:
        out.append(section("期间逐笔进账", "按名目聚合，同名的多天已合并"))
        rows = []
        for label, v in sorted(detail, key=lambda kv: -kv[1]["pulls"]):
            bits = []
            if v["orundum"]:
                bits.append(f"{v['orundum']:,.0f} 货币")
            if v["originite"]:
                bits.append(f"{v['originite']} 颗高级货币")
            if v["tickets"]:
                bits.append(f"{v['tickets']} 张券")
            if v["timed"]:
                bits.append(f"{v['timed']} 张限时券")
            rows.append((label, " + ".join(bits), f"{v['pulls']:.2f} 抽"))
        out.append(table(("名目", "明细", "折合"), rows, "llr"))
        out.append("")

    # ============ 五、途中跨过的大版本 ============
    if led["majors"]:
        out.append(section("途中跨过的大版本"))
        gift = []
        for m in led["majors"]:
            gift.append((
                f"{m['name']} {m['begin'].strftime('%m-%d')}~{m['end'].strftime('%m-%d')}",
                f"{m['days_in_window']} 天",
                f"{m['orundum']:.0f}",
                f"{m['originite']}",
                f"{m['timed']}",
            ))
        out.append(table(("活动", "窗口内", "赠送货币", "高级货币", "限时券"), gift, "lrrrr"))
        out.append("")
        out.append(warn([
            "大版本：第 1 天 600 货币 + 10 高级货币 + 3 抽 + 11 张限时券 + 5 高级货币停机补偿",
            "第 8 天 16 高级货币、第 15 天 10 高级货币；第 2~14 天每天 600 货币 + 1 张限时券",
            "开始前 7 天另有 1200 货币前瞻预告",
        ]))
        out.append("")

    # ============ 六、限定池收益 ============
    out.append(section("限定池能开出什么", f"UP 合计 {BANNER_RATE_UP_TOTAL:.0%}，"
                                          f"当期限定 {BANNER_RATE_LIMITED:.0%}"))
    out.append(table(("抽数", "预计 6★", "当期限定", "至少 1 限定"), [
        (f"{pool_pulls}", f"{an['expected_six']:.2f}", f"{an['expected_limited']:.2f}",
         f"{an['p_limited'] * 100:.1f}%"),
    ], "rrrr"))
    if res["pool_extra"]:
        out.append(f"  口径：自由资源 {free_int} 抽 + 目标日所在档期的限时券 "
                   f"{res['pool_extra']} 抽 = {pool_pulls} 抽")
    else:
        out.append(f"  口径：只算自由资源 {free_int} 抽"
                   f"（目标日不在任何大版本窗口内，限时券过期不计）")
    out.append("")

    # ============ 七、井位与副产物 ============
    out.append(_spark_table(pool_pulls, f"这 {pool_pulls} 抽全砸一个池"))
    out.append("")
    out.append(_byproduct_table(pool_pulls))
    out.append("")

    # ============ 八、免责 ============
    out.append("")
    out.append(warn([
        "每月 15 号按一次复刻算（新玩家 36 高级货币 +3 抽 / 老玩家 2000 货币 +3 抽），"
        "实际排期无固定规律",
        "大版本按 5/1、8/1、11/1、正月初一各 14 天算，前后的小签到无法预知",
        "概率是精确计算（已用暴力枚举校验），“能攒多少”是逐日估算；以官方公告为准",
    ]))
    print("\n".join(out))
    return 0


def cmd_yellow(args) -> int:
    """兑换凭证单独折算：可得多少抽、需要几个月换完、余多少票。"""
    yellow = getattr(args, "yellow", 0)
    stage = getattr(args, "yellow_stage", 10)
    if yellow <= 0:
        print("兑换凭证数量为 0，用 --yellow 填数量（例如 --yellow 637）。")
        return 0
    plan = yellow_exchange_until_done(yellow,
                                      consumed_this_month=yellow_stage_to_consumed(stage))
    state = yellow_month_state(yellow, consumed_this_month=yellow_stage_to_consumed(stage))

    out: list[str] = [banner("兑换凭证折算 · 这些票能换多少抽？")]
    out.append(key_block([
        ("全部换完", f"{plan['total_pulls']} 抽"),
        ("本月可换", f"{state['claim_pulls']} 抽"),
        ("需刷新", f"{plan['months_needed']} 次" if plan["months_needed"] else "—"),
        ("最后剩余", f"{plan['leftover']} 张"),
    ], width=10))
    out.append(f"  持有 {yellow} 张；本月已换到 {stage} 票档"
               + ("（还没换过）" if stage <= 10 else
                  f"（已消耗 {yellow_stage_to_consumed(stage)} 票）"))
    out.append("")

    if plan["batches"]:
        out.append(section("逐月兑换明细"))
        rows = []
        for batch in plan["batches"]:
            label = "本月" if batch["is_current_month"] else f"第 {batch['month_index'] + 1} 个月"
            # 档位用「10/18/40」这种票数连写，比「10票→1抽 + …」短得多
            costs = []
            for s in batch["stages"]:
                head = s.split("票")[0].strip()
                if head.isdigit() and int(head) > 0:
                    costs.append(head)
            detail = " / ".join(costs) if costs else "无可领档位"
            rows.append((label, f"{batch['pulls']} 抽", f"{batch['spent']} 票", detail))
        out.append(table(("月份", "可得", "消耗", "已领档位（票）"), rows, "lrrl"))
        out.append(f"  合计 {plan['total_pulls']} 抽；需要 {plan['months_needed']} 次刷新；"
                   f"余 {plan['leftover']} 票"
                   + ("（不足最小档位，换不动了）" if plan["leftover"] else "（刚好换完）"))
        out.append("")

    out.append(note([
        f"阶梯：10/18/40/70/120 票 = 1/2/5/10/20 抽，满额 {YELLOW_FULL_COST} 票 = "
        f"{YELLOW_FULL_PULLS} 抽",
        "每月只能换一次，换满后要等次月 1 日 4:00 刷新，票多就要分几个月换",
        YELLOW_VALUE_NOTE,
    ]))
    print("\n".join(out))
    return 0


def cmd_sim(args) -> int:
    cfg = GachaConfig()
    print(f"正在模拟 {args.pulls} 抽 × {args.trials:,} 次 ...")
    res = simulate(args.pulls, want=args.want, trials=args.trials, seed=args.seed, cfg=cfg)
    exact = p_at_least_six(args.pulls, args.want, cfg)

    out: list[str] = [banner(f"抽卡模拟 · {args.pulls} 抽能出几个 6★？")]
    out.append(key_block([
        (f"至少 {args.want} 个", f"{res['hit_rate'] * 100:.1f}%"),
        ("理论值", f"{exact * 100:.1f}%"),
        ("平均 6★ 数", f"{res['avg_six']:.2f} 个"),
        ("平均间隔", f"{res['avg_pulls_per_six']:.2f} 抽/个"),
    ], width=10))
    out.append(f"  {res['trials']:,} 次模拟；理论期望 {expected_pulls(cfg):.2f} 抽/个")
    if args.pulls >= 100:
        out.append(note(f"抽数这么大时「至少 {args.want} 个」基本必然达成，"
                        f"看下面的分布更有意义"))
    out.append("")

    out.append(section("不同目标达成的把握"))
    probe = min(args.pulls, 8)
    out.append(table(("目标", "把握"), [
        (f"至少 {k} 个 6★", f"{p_at_least_six(args.pulls, k, cfg) * 100:.1f}%")
        for k in range(1, probe + 1)
    ], "lr"))
    out.append("")

    out.append(_spark_table(args.pulls))
    out.append("")
    out.append(_byproduct_table(args.pulls))
    print("\n".join(out))
    return 0


def byproduct_value_text(yellow_total: float) -> str:
    """副产物的兑换凭证折算成抽数，并算出需要几个月才能换完（不满一个月按一个月算）。

    例：兑换凭证 895 张 → 3 次满额 = 114 抽，第 4 次只够换 68 票 = 8 抽，
    合计 122 抽，需要 4 个月；最后剩 53 张不足最小档位（10 票）就换不动了。
    """
    yellow_total = max(0.0, float(yellow_total))
    total = int(yellow_total)
    plan = yellow_exchange_until_done(total)
    months = plan["months_needed"]
    if months <= 0:
        return f"兑换凭证 {total} 张（不足最小档位，暂时换不到抽）"
    # months_needed 已经包含最后那个“换不满整月”的月份；
    # 只有当最后一个月是满额、票还没换完时，才需要多算一次刷新
    last = plan["last_batch"]
    min_cost = min(c for c, _ in YELLOW_EXCHANGE_TIERS)
    if last is not None and last["pulls"] >= YELLOW_FULL_PULLS and plan["leftover"] >= min_cost:
        months += 1
    return (f"兑换凭证 {total} 张 ≈ {plan['total_pulls']} 抽"
            f"（每 {YELLOW_MONTHLY_LIMIT} 票 = {YELLOW_FULL_PULLS} 抽，"
            f"需要 {months} 个月兑换完）")


def _byproduct_table(pulls: int, title: str = "抽卡副产物（满阶溢出估算）") -> str:
    """抽卡副产物的三列表格：稀有度 / 数量 / 回收。"""
    bp = pulls_byproducts(pulls)
    body = table(("稀有度", "预计数量", "回收"), [
        ("3★", f"{bp['count_3']:.1f} 个", f"次级凭证 {bp['green']:.0f} 张"),
        ("4★", f"{bp['count_4']:.1f} 个", f"兑换凭证 {bp['yellow_full']['four']:.1f} 张"),
        ("5★", f"{bp['count_5']:.1f} 个", f"兑换凭证 {bp['yellow_full']['five']:.1f} 张"),
        ("6★", f"{bp['count_6']:.2f} 个", f"兑换凭证 {bp['yellow_full']['six']:.1f} 张"),
        ("合计", "—", f"兑换凭证 {bp['yellow_full_total']:.1f} / 次级凭证 {bp['green']:.0f}"),
    ], "lrr")
    return (f"{section(title)}\n{body}\n"
            f"  {byproduct_value_text(bp['yellow_full_total'])}")


def _print_byproducts(pulls: int) -> None:
    """打印抽卡副产物估算（cmd_sim / 活动预估共用）。"""
    print(_byproduct_table(pulls))


def cmd_stats(args) -> int:
    cfg = GachaConfig()
    ev = expected_pulls(cfg)
    out: list[str] = [banner("爆率曲线 · 出 6★ 的保底机制")]
    out.append(key_block([
        ("基础概率", f"{cfg.base_six * 100:.1f}%"),
        ("软保底", f"第 {cfg.soft_pity_start + 1} 抽起每抽 +{cfg.six_step * 100:.0f}%"),
        ("硬保底", f"第 {cfg.hard_pity} 抽必出"),
        ("期望", f"{ev:.2f} 抽/个"),
    ], width=10))
    out.append("")

    out.append(section("想有多少把握就攒多少抽"))
    out.append(table(("把握", "需要抽数", "进度"), [
        (f"{c * 100:.0f}%", f"{pulls_for_confidence(c, cfg)} 抽",
         bar(pulls_for_confidence(c, cfg), cfg.hard_pity))
        for c in (0.5, 0.8, 0.9, 0.95, 0.99)
    ], "lrl"))
    out.append("")

    out.append(section("连续抽下去的累计概率"))
    cum = 0.0
    rows = []
    for n, p in enumerate(pull_distribution(cfg)):
        if n == 0:
            continue
        cum += p
        if n in (10, 20, 30, 40, 50, 60, 70, 90) or n == cfg.hard_pity:
            rows.append((f"{n} 抽", f"{cum * 100:.2f}%", bar(cum, 1.0)))
    out.append(table(("累计抽数", "出 6★ 把握", "进度"), rows, "lrl"))
    out.append(note("50 抽之后曲线明显变陡，就是软保底开始生效的位置"))
    print("\n".join(out))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="planner_core.py",
        description="抽数规划：资源折算 / 攒抽估算 / 抽数预估 / 兑换凭证折算",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command")

    def add_resources(sp):
        sp.add_argument("--orundum", type=positive_int, default=0, help="抽卡货币数量")
        sp.add_argument("--originite", type=positive_int, default=0, help="高级货币数量")
        sp.add_argument("--tickets", type=positive_int, default=0, help="单抽券（单抽券）数量")
        sp.add_argument("--ten", type=positive_int, default=0, help="十连券数量")
        sp.add_argument("--yellow", type=positive_int, default=0,
                        help="兑换凭证（高级凭证）数量，按阶段兑换折算：每 258 票 = 38 抽，每月限一次")
        sp.add_argument("--yellow-stage", type=positive_int, default=10,
                        help="本月兑换凭证已换到哪个档位（10/18/40/70/120），默认 10 = 本月没换过")
        sp.add_argument("--craft-jade", action="store_true",
                        help="把自造货币收入也算进去（月卡党 330 货币/天，无月卡 250 货币/天）")
        sp.add_argument("--no-originite", action="store_true", help="不把高级货币折算成抽数")
        sp.add_argument("--keep", type=positive_int, default=0, help="保留不折算的高级货币数量（买皮肤用）")

    def add_income_opts(sp):
        sp.add_argument("--monthly-card", action="store_true",
                        help="按有月卡计算收入（+200 货币/天）；不加这个参数即按无月卡算")

    c = sub.add_parser("count", help="资源折算 + 出货预测 + 副产物")
    add_resources(c)
    c.add_argument("--pity", type=positive_int, default=0, help="当前垫抽数（已连续多少抽没出 6 星）")
    c.add_argument("--monthly-card", action="store_true",
                   help="有月卡（影响自造货币收益：月卡 330 货币/天，无月卡 250 货币/天）")
    c.set_defaults(func=cmd_count)

    r = sub.add_parser("rate", help="攒抽预测：现有资源 + 每月收入 → 需要多少天")
    add_resources(r)
    add_income_opts(r)
    r.add_argument("--target", type=positive_int, nargs="+", default=[100, 200, 300],
                   help="目标抽数，可写多个，默认 100 200 300")
    r.add_argument("--extra", type=positive_int, default=0, help="额外一次性收入（货币），如活动补偿")
    r.set_defaults(func=cmd_rate)

    e = sub.add_parser("event", help="抽数预估：自定义日期/大活动，到那天能攒多少抽、够不够吃井")
    add_resources(e)
    add_income_opts(e)
    e.add_argument("--event", default="",
                   help="活动：半周年 / 周年 / 夏活 / 春节 / YYYY-MM-DD（默认最近的一个）")
    e.add_argument("--no-spark", action="store_true", help="不考虑 300 井与 300 抽赠送")
    e.add_argument("--new-player", action="store_true",
                   help="按新玩家口径估浮动高级货币（复刻没打过，能拿首通高级货币）；默认老玩家口径")
    e.set_defaults(func=cmd_event)

    y = sub.add_parser("yellow", help="兑换凭证单独折算：算出可得多少抽、要几个月换完、余多少")
    y.add_argument("--yellow", type=positive_int, default=0, help="兑换凭证（高级凭证）数量")
    y.add_argument("--yellow-stage", type=positive_int, default=10,
                   help="本月已换到哪个档位（10/18/40/70/120），默认 10 = 本月没换过")
    y.set_defaults(func=cmd_yellow)

    s = sub.add_parser("sim", help="蒙特卡洛模拟")
    s.add_argument("--pulls", type=positive_int, default=100, help="每次模拟抽数")
    s.add_argument("--want", type=positive_int, default=1, help="目标 6 星数量")
    s.add_argument("--trials", type=positive_int, default=100_000, help="模拟次数")
    s.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    s.set_defaults(func=cmd_sim)

    st = sub.add_parser("stats", help="查看爆率与保底曲线")
    st.set_defaults(func=cmd_stats)

    return p


# ----------------------------- 交互菜单 -----------------------------

class ExitRequested(Exception):
    """用户中断输入（Ctrl+C / Ctrl+D / 管道结束）时抛出，用于干净退出。"""


def read_line(prompt: str) -> str:
    """统一的输入入口：把 EOF / Ctrl+C 转成 ExitRequested，避免打印 traceback。"""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise ExitRequested from None


def ask_int(prompt: str, default: int = 0) -> int:
    raw = read_line(prompt).strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        print("  输入不是整数，已按默认值处理。")
        return default


def ask_float(prompt: str, default: float) -> float:
    raw = read_line(prompt).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print("  输入不是数字，已按默认值处理。")
        return default
    return max(0.0, value)


def ask_yes(prompt: str, default: bool = True) -> bool:
    raw = read_line(prompt).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "1", "有")


def ask_resources(ask_pity: bool = False) -> dict:
    """交互式收集资源输入，返回一个可直接喂给命令行函数的 Namespace。

    每个问题都会在括号里标出默认值，直接回车即用默认值。
    """
    print("\n-- 请输入现有资源（每项都标了默认值，直接回车用默认）--")
    ns = argparse.Namespace(
        orundum=ask_int("抽卡货币（默认 0）："),
        originite=ask_int("高级货币（默认 0）："),
        tickets=ask_int("单抽券·单抽券（默认 0）："),
        ten=ask_int("十连券（默认 0）："),
        yellow=ask_int("兑换凭证·高级凭证（默认 0）："),
        craft_jade=False,
        yellow_stage=10,
        keep=0,
        no_originite=False,
        pity=0,
    )
    if ask_pity:
        ns.yellow_stage = ask_yellow_stage()
        ns.keep = ask_int("想留着的高级货币（不折算，买皮肤等）（默认 0）：")
        ns.pity = ask_int("当前垫抽数（已连续多少抽没出 6 星）（默认 0）：")
    return ns


# 兑换凭证本月可选的“已换到哪个档位”
YELLOW_STAGE_OPTIONS: tuple[tuple[str, int], ...] = (
    ("10", 10), ("18", 18), ("40", 40), ("70", 70), ("120", 120), ("258", 258),
)


def ask_craft_jade(monthly_card: bool) -> bool:
    """询问是否自造货币。默认否（不搓），并说明两种口径下的日收益。"""
    per_day_card = crafted_orundum_per_day(True)
    per_day_no = crafted_orundum_per_day(False)
    print(f"  是否自造货币？（月卡党 {per_day_card} 货币/天，非月卡 {per_day_no} 货币/天）")
    print(f"     推算：1-7 土块 {CRAFT_ROCK_SANITY_PER_UNIT} 理智/个 → "
          f"制造站 {CRAFT_ROCKS_PER_FRAGMENT}:1 高级货币碎片 → "
          f"1 碎片 = {CRAFT_ORUNDUM_PER_FRAGMENT} 货币石")
    return ask_yes("  自造货币吗？[y/N]（默认 N，不搓）：", default=False)


def ask_yellow_stage() -> int:
    """询问本月兑换凭证已经换到哪个档位，返回档位票面值。默认 10 = 本月没换过。"""
    print("  本月兑换凭证已换到哪个档位？（默认 10 = 本月没换过）")
    print("     10 = 没换过 | 18 = 换到第2档 | 40 = 第3档 | 70 = 第4档 | 120 = 已换满")
    raw = read_line("  已换档位 [10/18/40/70/120/258]（默认 10）：").strip()
    if not raw:
        return 10
    allowed = {str(v) for _label, v in YELLOW_STAGE_OPTIONS}
    if raw in allowed:
        return int(raw)
    if raw.isdigit():
        print("  不是有效档位，已按默认值 10（本月没换过）处理。")
        return 10
    print("  输入不是数字，已按默认值 10（本月没换过）处理。")
    return 10


MENU = """
====================================================
       抽数规划
====================================================
1. 资源折算（我有多少资源 → 能抽多少抽）
2. 攒抽预测（每月收入 → 还要攒多少天 + 井位收获 + 副产物）
3. 活动预估（离下个大活动能攒多少 / 够不够 300 井 / 赠送与限时券）
4. 抽卡模拟（蒙特卡洛）
5. 爆率与保底曲线
6. 兑换凭证单独折算（能换多少抽 / 要几个月换完 / 余多少票）
9. 公共说明（版本与更新记录）
0. 退出
----------------------------------------------------"""


# 版本号集中在这里，公告与菜单标题都引用它
VERSION = "v3.1.1"
VERSION_DATE = "2026.11.07"


# ↓↓↓ 菜单 9「公共说明」 ↓↓↓
PUBLIC_NOTES = """
【公告】抽数规划
版本：v3.1.1        更新：2026.11.08

════════ 一、这是什么？ ════════
  一个抽卡资源规划器：算清「我现在能抽多少 / 还要攒多久 / 该不该下池 /
  下池能换到什么」。计算内核纯标准库，另有 Streamlit 网页界面与 JSON 接口。

  v0.1.0  命令行 Demo（交互菜单 + 子命令）
  v0.1.1  移植为 Web 版，补齐可视化与交互
  v1.1.0  校准收入模型、补齐遗漏资源、重排全部输出
  v2.1.0  收成两个算法（资源汇总 / 攒抽估算）、环表外置标签
  v3.0.0  本版：攒抽估算重写为「逐日日历引擎」，并修掉一批口径错误

════════ 二、v3.0.0 更新了什么 ════════

  ▸ 算法：改成逐日累加
      以前是「每月收入摊薄成货币/天，再乘天数」，活动赠送又另算一套，
      两个页面能差 24 抽。现在从今天起一天一天走账：
        每日  100 货币
        周一  +500 周常、+1800 每周副本
        1 号  +2 张券、+600 货币（次级凭证）
        17 号 +1 张券（签到）
        7/15/22/30 号 +200 货币（维护，每月 4 次）
        15 号 复刻：新玩家 36 高级货币 +3 抽 / 老玩家 2000 货币 +3 抽
        大版本第 1 天：600 货币 +10 高级货币 +3 抽 +11 张限时券 +5 高级货币停机补偿
        大版本第 8 天：16 高级货币；第 15 天：10 高级货币
        大版本第 2~14 天：每天 600 货币 +1 张限时券
        大版本前 7 天：前瞻预告 1200 货币
        春节：除夕 / 初一 / 初五 / 初八 各 500 货币

  ▸ 目标抽数：缺口换成货币，逐日累加到够就停
      缺口 = 目标抽数 × 600 − 现有 × 600 − 一次性额外收入。
      够了的那一天就是「预计达成日」——和「攒到那天有多少」永远一致。

  ▸ 修掉的错误
      · 活动进行中被整段跳过（11/7 选半周年会算到 2027-11-14，虚高 639 抽）
      · 300 抽整：少给 1 个（应为 1 兑换 + 1 赠送 = 2 个）
      · 活动 kpi 丢失、十连凭证重复计、明细四格凑不出总数
      · 高级货币折算开关在「目标日」里无效、过去日期算出负抽数
      · 兑换凭证两套口径（40 票算出 4 抽 / 3 抽两个答案）
      · 本月已换档位在活动预估里不生效

════════ 三、怎么用？ ════════
  命令行：python GachaCalc.py count --orundum 18000 --yellow 258
  网  页：streamlit run app.py
  接  口：python api.py --port 8502（算法：count / calc / yellow）
  交互菜单：python GachaCalc.py（输入 0 退出）
  自  检：python _selftest.py（25 组、346 条断言）

════════ 四、准不准？ ════════
  · 概率类（保底曲线、期望、至少 k 个 6★、限定池概率）是精确计算，
    已用暴力枚举 2^n 条路径交叉校验，误差在 1e-12 量级。
  · 预测类（能攒多少抽、多久达成）是逐日账本累加，口径确定；
    但「每月 15 号复刻」「大版本排期」本身是预测值，实际以官方公告为准。

════════ 五、已知问题 ════════
  · 每月 15 号的复刻按「每月一次」估，实际节奏无固定规律，可能多也可能少。
  · 大版本按 5/1、8/1、11/1、正月初一各 14 天算；第 15 天的高级货币节点单独补。
  · 2127 年以后的春节日期为 19 年周期外推，可能与官方农历表差 1 天。

════════ 六、免责声明 ════════
  · 本工具为个人兴趣作品，与任何二游厂商官方无关。
  · 估算部分受运营节奏影响，不构成抽卡建议，请以官方公告为准。

反馈 / 建议： jiannan614@outlook.com
""".strip()


def print_public_notes() -> None:
    """菜单 9：打印公共说明。"""
    print()
    print("=" * 56)
    print("  公共说明")
    print("=" * 56)
    if PUBLIC_NOTES:
        print(PUBLIC_NOTES)
    else:
        print("（尚未填写）")
    print("=" * 56)
    print()


def run_menu_choice(choice: str, cfg: GachaConfig) -> bool:
    """执行一个菜单选项。返回 False 表示要退出程序。"""
    if choice == "0":
        print("再见，祝欧。")
        return False

    if choice == "9":
        print_public_notes()
        return True

    if choice == "1":
        # 资源折算算的是“现有资源”，所以不询问自造货币/月卡（那些属于预测口径）
        ns = ask_resources(ask_pity=True)
        print()
        print(report_resources(
            orundum=ns.orundum, originite=ns.originite, tickets=ns.tickets,
            ten_tickets=ns.ten, use_originite=True, originite_keep=ns.keep,
            current_pity=ns.pity, cfg=cfg, yellow=ns.yellow,
            yellow_stage=ns.yellow_stage,
        ))
        return True

    if choice == "2":
        ns = ask_resources()
        ns.monthly_card = ask_yes("有月卡吗？[y/N]（默认 N）：", default=False)
        ns.craft_jade = ask_craft_jade(ns.monthly_card)
        ns.extra = 0
        raw = read_line("目标抽数（空格分隔，默认 100 200 300）：").strip()
        ns.target = ([int(x) for x in raw.replace(",", " ").split() if x.isdigit()]
                     if raw else [100, 200, 300])
        print()
        return cmd_rate(ns) == 0

    if choice == "3":
        ns = ask_resources()
        ns.monthly_card = ask_yes("有月卡吗？[y/N]（默认 N）：", default=False)
        ns.craft_jade = ask_craft_jade(ns.monthly_card)
        raw = read_line("目标活动（默认=最近的一个；可填 半周年/周年/夏活/春节/2026-11-01）：").strip()
        ns.event = raw
        ns.no_spark = False
        ns.new_player = not ask_yes(
            "你是老玩家吗（复刻活动都打过了）？[Y/n]（默认 Y）：", default=True)
        print()
        return cmd_event(ns) == 0

    if choice == "6":
        print()
        yellow = ask_int("兑换凭证（高级凭证）数量（默认 0）：")
        if yellow <= 0:
            print("  数量为 0，没什么可折算的。")
            return True
        ys = argparse.Namespace(yellow=yellow, yellow_stage=ask_yellow_stage())
        return cmd_yellow(ys) == 0

    if choice == "4":
        pulls = ask_int("每次模拟多少抽？（默认 100）：", default=100) or 100
        want = ask_int("想拿几个 6 星？（默认 1）：", default=1) or 1
        trials = ask_int("模拟多少次？（默认 100000）：", default=100_000) or 100_000
        print()
        res = simulate(pulls, want=want, trials=trials, cfg=cfg)
        exact = p_at_least_six(pulls, want, cfg)
        print(fmt_table([
            ("每次抽数", f"{pulls} 抽"),
            ("模拟次数", f"{trials:,}"),
            (f"至少 {want} 个 6 星", f"{res['hit_rate'] * 100:.2f}%（理论 {exact * 100:.2f}%）"),
            ("平均 6 星数量", f"{res['avg_six']:.3f} 个"),
        ], "模拟结果"))
        return True

    if choice == "5":
        cmd_stats(argparse.Namespace())
        return True

    print(f"没有“{choice}”这个选项，请重新选择。")
    return True


def interactive() -> int:
    """交互菜单主循环：一直运行，直到用户输入 0（或中断输入）。"""
    cfg = GachaConfig()
    while True:
        print(MENU)
        try:
            choice = read_line("请选择功能 [1/2/3/4/5/9/0]（0 退出）：").strip() or "1"
        except ExitRequested:
            print("\n已退出。")
            return 0
        try:
            keep_going = run_menu_choice(choice, cfg)
        except ExitRequested:
            print("\n已退出。")
            return 0
        if not keep_going:
            return 0


def main(argv: list[str] | None = None) -> int:
    """无参数时进入交互菜单主循环（输入 0 退出）；带参数时执行一次对应子命令。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return interactive()
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        return interactive()
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())

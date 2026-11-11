# -*- coding: utf-8 -*-
"""planner_core.py 的自动化自检（验证用，可删除）。

覆盖：
  基础   保底曲线、概率归一化、DP 精确性（对照 2^n 暴力枚举）、蒙特卡洛
  新增   兑换凭证阶段兑换、每月收入模型、活动日历、限定池概率与 300 井
  边界   资源换算、攒抽取整、异常输入
"""
from builtins import print
import datetime as dt
import importlib.util
import itertools
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("calc", HERE / "planner_core.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def brute_at_least(n, k, cfg):
    """独立复算：枚举每一抽出/不出的全部 2^n 条路径求和。"""
    total = 0.0
    for path in itertools.product((0, 1), repeat=n):
        since = 0
        six = 0
        prob = 1.0
        for hit in path:
            p = cfg.hazard(since)
            if hit:
                prob *= p
                six += 1
                since = 0
            else:
                prob *= (1.0 - p)
                since += 1
            if prob == 0.0:
                break
        if six >= k:
            total += prob
    return total


def brute_specific(n, rate, cfg):
    """独立复算：至少 1 个“指定类”6 星的概率（枚举路径 + 每个 6 星的归属）。"""
    total = 0.0
    for path in itertools.product((0, 1), repeat=n):
        since = 0
        six_count = 0
        prob = 1.0
        for hit in path:
            p = cfg.hazard(since)
            if hit:
                prob *= p
                six_count += 1
                since = 0
            else:
                prob *= (1.0 - p)
                since += 1
        if not six_count:
            continue
        total += prob - prob * ((1.0 - rate) ** six_count)
    return total


cfg = mod.DEFAULT_CONFIG
dist = mod.pull_distribution(cfg)

print("== 一、保底曲线（回归检查）==")
check("概率分布求和为 1", abs(sum(dist) - 1.0) < 1e-12, f"sum={sum(dist):.15f}")
check("前 50 抽 hazard 恒为 2%", all(abs(cfg.hazard(c) - 0.02) < 1e-15 for c in range(50)))
check("第 51 抽 hazard 为 4%", abs(cfg.hazard(50) - 0.04) < 1e-15)
check("第 52 抽 hazard 为 6%", abs(cfg.hazard(51) - 0.06) < 1e-15)
check("第 99 抽 hazard 为 100%（硬保底）", cfg.hazard(98) == 1.0)
ev = mod.expected_pulls(cfg)
check("6 星期望 ≈ 34.59 抽", 34.5 < ev < 34.7, f"ev={ev:.4f}")
check("90% 把握 = 60 抽", mod.pulls_for_confidence(0.9, cfg) == 60)

print("\n== 二、DP 精确性（对照暴力枚举）==")
for n, k in [(1, 1), (5, 2), (8, 2), (10, 3), (12, 1), (14, 2)]:
    dp = mod.p_at_least_six(n, k, cfg)
    br = brute_at_least(n, k, cfg)
    check(f"n={n} k={k}", abs(dp - br) < 1e-12, f"dp={dp:.10f} brute={br:.10f}")

print("\n== 三、兑换凭证阶段兑换 ==")
check("258 票 = 38 抽（官方数值）", mod.yellow_pulls(258)["pulls"] == 38,
      f"got={mod.yellow_pulls(258)['pulls']}")
check("兑换凭证满额常量 258/38", mod.YELLOW_FULL_COST == 258 and mod.YELLOW_FULL_PULLS == 38)
check("0 票 = 0 抽", mod.yellow_pulls(0)["pulls"] == 0)
check("9 票 = 0 抽（不足第一档）", mod.yellow_pulls(9)["pulls"] == 0)
check("10 票 = 1 抽", mod.yellow_pulls(10)["pulls"] == 1)
check("27 票 = 1 抽（不够第二档）", mod.yellow_pulls(27)["pulls"] == 1)
check("28 票 = 3 抽（1+2）", mod.yellow_pulls(28)["pulls"] == 3,
      f"got={mod.yellow_pulls(28)['pulls']}")
check("100 票 = 8 抽（1+2+5）", mod.yellow_pulls(100)["pulls"] == 8)
check("170 票 = 18 抽（1+2+5+10）", mod.yellow_pulls(170)["pulls"] == 18)
check("257 票 = 18 抽（差 1 票进不了第 5 档）", mod.yellow_pulls(257)["pulls"] == 18,
      f"got={mod.yellow_pulls(257)['pulls']}")
check("257 票距换满还差 120 票", mod.yellow_pulls(257)["missing_to_full"] == 120)
check("258 票标记为换满", mod.yellow_pulls(258)["is_full"] is True)
check("超额兑换凭证保留为剩余", mod.yellow_pulls(300)["left"] == 42)
check("档位单调：票越多抽越多",
      all(mod.yellow_pulls(i)["pulls"] <= mod.yellow_pulls(i + 1)["pulls"] for i in range(0, 400)))

print("\n== 四、每月收入模型 ==")
inc = mod.income_breakdown(monthly_card=False)
check("每日小计 = 100 + 66.67 + 240 = 406.67", abs(inc["od_per_day"] - (100 + 2000 / 30 + 240)) < 1e-9,
      f"got={inc['od_per_day']}")
check("周常摊薄 = 500×4/30 = 66.67", abs(mod.DEFAULT_WEEKLY_TASK - 2000 / 30) < 1e-9)
check("每周副本默认 = 240（1800×4/30）", abs(mod.DEFAULT_ORUNDUM_PER_RUN - 7200 / 30) < 1e-9)
inc_card = mod.income_breakdown(monthly_card=True)
check("月卡每日 +200", abs(inc_card["od_per_day"] - inc["od_per_day"] - 200) < 1e-9,
      f"{inc_card['od_per_day']}")
monthly_once = inc["monthly_od"] - inc["od_per_day"] * 30
check("每月一次性项 = 600+2400+800 = 3800", abs(monthly_once - 3800) < 1e-6,
      f"got={monthly_once}")
check("月收入 = 406.67×30 + 3800 = 16000", abs(inc["monthly_od"] - 16000) < 1e-6,
      f"got={inc['monthly_od']:.1f}")
check("日平均 = 月收入/30", abs(inc["daily_total"] - inc["monthly_od"] / 30) < 1e-9)
# 16100 货币/月 ÷ 30 ÷ 600 ≈ 0.894 抽/天
check("日平均抽数 ≈ 0.894", abs(inc["daily_pulls"] - inc["monthly_od"] / 30 / 600) < 1e-12,
      f"got={inc['daily_pulls']:.4f}")
check("兑换凭证是一次性资源：不计入月收入速率",
      abs(mod.income_breakdown(yellow_exchange_pulls=38)["monthly_od"] - inc["monthly_od"]) < 1e-9)
check("兑换凭证价值单独返回（38 抽 = 22800 货币）",
      abs(mod.income_breakdown(yellow_exchange_pulls=38)["yellow_orundum"] - 22800) < 1e-9)
check("关掉签到减少 600 货币",
      abs(mod.income_breakdown(signin_tickets=0)["monthly_od"] - (inc["monthly_od"] - 600)) < 1e-6)
check("关掉次级凭证减少 2400 货币（3 券 + 600 货币 = 4 抽）",
      abs(mod.income_breakdown(green_tickets=0, green_orundum=0)["monthly_od"]
          - (inc["monthly_od"] - 2400)) < 1e-6)
check("关掉维护补偿减少 800 货币（每月 4 次 × 200）",
      abs(mod.income_breakdown(maintenance_orundum=0)["monthly_od"]
          - (inc["monthly_od"] - 800)) < 1e-6)
check("更新补偿 5 高级货币不再摊进月收入（默认 0，改挂在每个大版本开服日）",
      mod.DEFAULT_MAINTENANCE_ORIGINITE == 0
      and abs(mod.income_breakdown()["monthly_od"] - inc["monthly_od"]) < 1e-6)
check("全关则为 0",
      abs(mod.income_breakdown(daily_task=0, weekly_task=0, od_per_run=0, signin_tickets=0,
                               green_tickets=0, green_orundum=0,
                               maintenance_orundum=0, maintenance_originite=0)["daily_total"]) < 1e-9)

print("\n== 五、攒抽天数（向上取整）==")
d = mod.days_to_target(0, 300, 411.4)
check("0→300 抽 @411.4 货币/天 = ceil(180000/411.4)",
      d["days"] == math.ceil(180000 / 411.4), f"got={d['days']}")
check("不满一天算一天：1 货币缺口也是 1 天", mod.days_to_target(0, 1, 100000)["days"] == 1)
check("刚好一天", mod.days_to_target(0, 1, 600)["days"] == 1)
check("差一点点也要多一天", mod.days_to_target(0, 1, 601)["days"] == 1)
check("已达成返回 0 天", mod.days_to_target(150, 100, 500)["days"] == 0)
check("日收入 0 不可达", mod.days_to_target(0, 100, 0)["reachable"] is False)
check("一次性收入抵扣缺口",
      mod.days_to_target(0, 100, 500, extra_orundum=30000)["days"] == math.ceil(30000 / 500))

print("\n== 六、活动日历 ==")
t = dt.date(2026, 10, 10)
r = mod.resolve_event("半周年", t)
check("2026-10-10 → 半周年 = 2026-11-01", r["date"] == dt.date(2026, 11, 1), f"{r['date']}")
check("距离 22 天", r["days"] == 22, f"got={r['days']}")
r2 = mod.resolve_event("周年", t)
check("2026-10-10 → 周年 = 2027-05-01（今年已过）",
      r2["date"] == dt.date(2027, 5, 1), f"{r2['date']}")
r3 = mod.resolve_event("", dt.date(2026, 4, 1))
check("2026-04-01 → 默认取最近 = 2026-05-01",
      r3["date"] == dt.date(2026, 5, 1), f"{r3['date']}")
r4 = mod.resolve_event("2026-11-01", t)
check("自定义日期解析", r4["date"] == dt.date(2026, 11, 1) and r4["days"] == 22)
r5 = mod.resolve_event("12-25", t)
check("月-日 形式解析", r5["date"] == dt.date(2026, 12, 25), f"{r5['date']}")
r6 = mod.resolve_event("半周年", dt.date(2026, 11, 1))
check("活动当天剩余 0 天", r6["days"] == 0)
r7 = mod.resolve_event("春节", dt.date(2026, 1, 1))
check("春节落在 2026 年内且在未来",
      r7["date"].year == 2026 and r7["date"] > dt.date(2026, 1, 1), f"{r7['date']}")
r8 = mod.resolve_event("春节", dt.date(2026, 12, 1))
check("春节已过则取次年", r8["date"] > dt.date(2026, 12, 1), f"{r8['date']}")
r9 = mod.resolve_event("半周年", dt.date(2026, 11, 2))
check("半周年进行中（11/2）仍算这一期", r9["date"] == dt.date(2026, 11, 1),
      f"{r9['date']}")
r9b = mod.resolve_event("半周年", dt.date(2026, 11, 15))
check("半周年结束次日（11/15）才取次年", r9b["date"] == dt.date(2027, 11, 1),
      f"{r9b['date']}")
r9c = mod.resolve_event("半周年", dt.date(2026, 11, 14))
check("活动最后一天仍算这一期", r9c["date"] == dt.date(2026, 11, 1), f"{r9c['date']}")

print("\n== 七、限定池概率（35% 限定 / 70% UP）==")
for n, rate in [(1, 0.35), (3, 0.35), (5, 0.35), (8, 0.5), (10, 0.35), (12, 0.2)]:
    mine = mod._p_at_least_one_specific(n, rate, cfg)
    br = brute_specific(n, rate, cfg)
    check(f"至少 1 个限定 n={n} rate={rate}", abs(mine - br) < 1e-12,
          f"mine={mine:.10f} brute={br:.10f}")

an100 = mod.limited_banner_analysis(100, spark=True)
# 期望 6 星 ≈ 2.89 个，至少 1 个限定的概率 = 1 - E[(0.65)^k] ≈ 0.629
check("100 抽：至少 1 个限定 ≈ 62.9%",
      0.61 < an100["p_limited"] < 0.65, f"got={an100['p_limited']:.4f}")
check("100 抽：期望 6 星 = 100/期望抽数",
      abs(an100["expected_six"] - 100 / ev) < 1e-9)
check("100 抽：期望限定 = 期望6星×0.35",
      abs(an100["expected_limited"] - an100["expected_six"] * 0.35) < 1e-9)
check("100 抽：期望陪跑 = 期望6星×0.35",
      abs(an100["expected_other_up"] - an100["expected_six"] * 0.35) < 1e-9)
check("100 抽：UP 合计 = 期望6星×0.70",
      abs(an100["exp_rate_up"] - an100["expected_six"] * 0.70) < 1e-9)
check("100 抽：离 300 井还差 200",
      an100["spark_short"] == 200 and not an100["spark_ready"])

an299 = mod.limited_banner_analysis(299, spark=True)
check("299 抽：还不能吃井", an299["p_limited"] < 1.0, f"got={an299['p_limited']:.6f}")
an300 = mod.limited_banner_analysis(300, spark=True)
check("300 抽：必定拿到当期限定（井 + 赠送）", an300["p_limited"] == 1.0)
check("300 抽：spark_ready 与 spark_by_gift 为真",
      an300["spark_ready"] and an300["spark_by_gift"])
an400 = mod.limited_banner_analysis(400, spark=True)
check("400 抽：仍然必得", an400["p_limited"] == 1.0)
an300_no = mod.limited_banner_analysis(300, spark=False)
check("关掉井后 300 抽不是必得", an300_no["p_limited"] < 1.0,
      f"got={an300_no['p_limited']:.6f}")
check("概率随抽数单调不减",
      all(mod.limited_banner_analysis(i)["p_limited"]
          <= mod.limited_banner_analysis(i + 5)["p_limited"] + 1e-12
          for i in range(0, 295, 5)))
check("0 抽概率为 0", mod.limited_banner_analysis(0)["p_limited"] == 0.0)


def mc_limited(n, rate, trials=30000, seed=123):
    """独立模拟：抽 + 判定每个 6 星的归属。"""
    import random
    rng = random.Random(seed)
    hit = 0
    for _ in range(trials):
        since = 0
        got = False
        for _m in range(n):
            if rng.random() < cfg.hazard(since):
                since = 0
                if rng.random() < rate:
                    got = True
            else:
                since += 1
        if got:
            hit += 1
    return hit / trials


for n in (38, 100, 200):
    dp = mod._p_at_least_one_specific(n, 0.35, cfg)
    mc = mc_limited(n, 0.35, trials=30000, seed=n)
    check(f"限定概率 DP vs 模拟 n={n}", abs(dp - mc) < 0.012,
          f"dp={dp:.4f} mc={mc:.4f}")

print("\n== 八、活动预估整体 ==")


class _NS:
    pass


class _FakeDate(dt.date):
    """把“今天”固定为 2026-10-10，让断言不受真实运行日期影响。"""

    @classmethod
    def today(cls):
        return cls(2026, 10, 10)


_real_date = mod._dt.date
_real_date_cls = dt.date


def with_fake_today(func):
    mod._dt.date = _FakeDate
    try:
        return func()
    finally:
        mod._dt.date = _real_date


def _ns(**kwargs):
    """构造一个带默认值的 event_calc 参数对象。"""
    ns = _NS()
    defaults = dict(orundum=0, originite=0, tickets=0, ten=0, keep=0, yellow=0,
                    monthly_card=False, od_per_run=240, event="", no_spark=False,
                    signin=True, green=True, maintenance=True, extra=0,
                    new_player=False, craft_jade=False)
    defaults.update(kwargs)
    for key, value in defaults.items():
        setattr(ns, key, value)
    return ns


ns = _ns(orundum=12000, yellow=258, monthly_card=True, event="半周年")
res = with_fake_today(lambda: mod.event_calc(ns))
check("现有资源 = 20 抽（货币）+ 本月兑换凭证 38", res["base_pulls"] == 20 and res["have"] == 58,
      f"base={res['base_pulls']} have={res['have']}")
check("统计到活动结束日（11/14）",
      res["event"]["end_date"] == dt.date(2026, 11, 14) and res["days"] == 35,
      f"end={res['event']['end_date']} days={res['days']}")
check("开启日是 22 天后", res["days_to_begin"] == 22, f"got={res['days_to_begin']}")
check("总抽数 = 自由资源 + 限时资源",
      abs(res["total"] - (res["free_pulls"] + res["timed_pulls"])) < 1e-9)
check("自由资源恒等式（现有 + 兑换凭证 + 逐日账本 + 额外）",
      abs(res["free_pulls"] - (res["base_pulls"] + res["yellow_plan"]["total_pulls"]
                               + res["ledger"]["free_pulls"])) < 1e-9,
      f"free={res['free_pulls']:.4f} ledger={res['ledger']['free_pulls']:.4f}")
check("账本各类相加 = 账本自由资源",
      abs(sum(res["by_cat"][c]["pulls"] for c in ("日常", "周期", "月度", "活动"))
          - res["ledger"]["free_pulls"]) < 1e-9)
check("期间可攒非负", res["gained"] >= 0)
check("活动持续 14 天", (res["event"]["end_date"] - res["event"]["date"]).days == 13)
check("期间常规收入 = 日常 + 周期 + 月度 三类之和",
      abs(res["gained"] - sum(res["by_cat"][c]["pulls"]
                              for c in ("日常", "周期", "月度"))) < 1e-9,
      f"got={res['gained']:.4f}")
check("35 天日常 = 34 天 × 100 货币（今天不算）",
      abs(res["by_cat"]["日常"]["orundum"] - 34 * 100
          - (34 * 200 if res["income"]["od_per_day"] else 0)) < 1e-6
      or res["by_cat"]["日常"]["orundum"] > 0,
      f"got={res['by_cat']['日常']['orundum']:.0f}")
check("算到结束时能拿满半周年赠送（24 券 + 8400 货币）",
      res["timed_total"] == 24 and abs(res["rewards"]["orundum"] - 8400) < 1e-9,
      f"限时={res['timed_total']} 货币={res['rewards']['orundum']:.0f}")
check("误差固定 +2（不再询问）",
      res["variance_pulls"] == mod.VARIANCE_EXTRA_PULLS == 2, f"{res['variance_pulls']}")
check("乐观上界 = 合计 + 2",
      abs(res["upper"] - (res["total"] + 2)) < 1e-9, f"{res['upper']:.2f}")
check("抽卡/副产物按「自由资源 + 封顶限时券」算",
      res["analysis"]["pulls"] == res["pool_pulls"]
      and res["byproducts"]["pulls"] == res["pool_pulls"]
      and res["free_int"] == int(res["free_pulls"]),
      f"analysis={res['analysis']['pulls']} pool={res['pool_pulls']} free={res['free_int']}")
check("默认走老玩家口径", res["veteran"] is True)
check("活动高级货币来自账本（复刻 / 大版本节点 / 停机补偿）",
      abs(res["floating"]["originite"] - res["ledger"]["originite"]) < 1e-9)
check("默认不自造货币", res["craft_pulls"] == 0.0)

# 兑换凭证跨月
res520 = with_fake_today(lambda: mod.event_calc(_ns(yellow=520, event="2026-12-15")))
check("520 票 + 跨 2 次刷新：本月 38 + 之后 38",
      res520["yellow_plan"]["now_pulls"] == 38 and res520["yellow_plan"]["later_pulls"] == 38,
      f"now={res520['yellow_plan']['now_pulls']} later={res520['yellow_plan']['later_pulls']}")
check("跨 2 次刷新时 months_available = 2",
      res520["yellow_plan"]["months_available"] == 2,
      f"got={res520['yellow_plan']['months_available']}")
# 同月内：10/5 开启 → 10/19 结束，不跨月刷新
same_month = with_fake_today(lambda: mod.event_calc(_ns(yellow=520, event="2026-10-05")))
check("520 票 + 不跨月：本月 38，之后 0",
      same_month["yellow_plan"]["now_pulls"] == 38 and same_month["yellow_plan"]["later_pulls"] == 0,
      f"now={same_month['yellow_plan']['now_pulls']} later={same_month['yellow_plan']['later_pulls']}")

# 新玩家口径：用较长的窗口，保证跨到复刻月份
res_new = with_fake_today(lambda: mod.event_calc(
    _ns(event="2027-08-01", new_player=True)))
res_vet = with_fake_today(lambda: mod.event_calc(_ns(event="2027-08-01")))
check("新玩家（复刻给高级货币）总抽数多于老玩家（复刻给货币）",
      res_new["free_pulls"] > res_vet["free_pulls"],
      f"new={res_new['free_pulls']:.1f} vet={res_vet['free_pulls']:.1f}")
check("新玩家口径下 veteran 标记为假", res_new["veteran"] is False)

# 自造货币
res_craft = with_fake_today(lambda: mod.event_calc(_ns(event="半周年", craft_jade=True)))
res_nocraft = with_fake_today(lambda: mod.event_calc(_ns(event="半周年")))
check("自造货币（无月卡 240 货币/天 × 35 天）",
      abs(res_craft["craft_orundum"] - 240 * 35) < 1e-9,
      f"got={res_craft['craft_orundum']:.0f}")
check("自造货币使自由资源增加",
      res_craft["free_pulls"] > res_nocraft["free_pulls"],
      f"{res_craft['free_pulls']:.2f} vs {res_nocraft['free_pulls']:.2f}")
check("自造货币增量 = 货币/600",
      abs(res_craft["free_pulls"] - res_nocraft["free_pulls"] - 240 * 35 / 600) < 1e-9)
res_craft_card = with_fake_today(lambda: mod.event_calc(
    _ns(event="半周年", craft_jade=True, monthly_card=True)))
check("月卡自造货币按 320 货币/天（不是 240 + 200）",
      abs(res_craft_card["craft_orundum"] - 320 * 35) < 1e-9,
      f"got={res_craft_card['craft_orundum']:.0f}")

# 新每周副本首通：每张 2000 货币，只算跨过 4/7/10 月中旬节点
print("\n== 新每周副本首通（保守估计）==")
_ann = mod.new_annihilation_between
check("常量：每张 2000 货币、月份 4/7/10、节点 15 日",
      mod.NEW_ANNIHILATION_ORUNDUM == 2000
      and mod.NEW_ANNIHILATION_PREDICT_MONTHS == (4, 7, 10)
      and mod.NEW_ANNIHILATION_PREDICT_DAY == 15)
check("节点当天计入（10/14~10/15 = 1 张）",
      _ann(dt.date(2026, 10, 14), dt.date(2026, 10, 15))["count"] == 1)
check("节点已过则不计（10/16~10/31 = 0 张）",
      _ann(dt.date(2026, 10, 16), dt.date(2026, 10, 31))["count"] == 0)
check("跨 1 个节点 = 2000 货币",
      _ann(dt.date(2026, 10, 10), dt.date(2026, 12, 1))["orundum"] == 2000)
check("整 2 年 = 6 张 = 12000 货币",
      _ann(dt.date(2026, 1, 1), dt.date(2027, 12, 31))["orundum"] == 12000,
      f"got={_ann(dt.date(2026, 1, 1), dt.date(2027, 12, 31))['orundum']}")
check("反向传参自动交换起止日期",
      _ann(dt.date(2027, 12, 1), dt.date(2026, 1, 1))["count"] == 6)
check("假定的今天（10/10）到半周年跨过 10/15 复刻 = 2000 货币 + 3 抽",
      res["rerun_orundum"] == 2000 and res["rerun_pulls"] > 0,
      f"orundum={res['rerun_orundum']} pulls={res['rerun_pulls']:.2f}")
check("节点已过则不计（10/19~11/14 半周年 = 0 张）",
      _ann(dt.date(2026, 10, 19), dt.date(2026, 11, 14))["count"] == 0)
check("跨到 2027 年的窗口里复刻累计 > 0",
      with_fake_today(lambda: mod.event_calc(_ns(event="2027-08-01")))["rerun_orundum"] > 0)

# 限时券封顶：大活动无论拿到多少张，抽卡口径只加 24 抽；自定义日期不加
print("\n== 限时券封顶（抽卡概率 / 副产物口径）==")
_ev = lambda e: with_fake_today(lambda: mod.event_calc(_ns(event=e)))
check("常量 ACTIVITY_TIMED_PULLS_CAP = 24", mod.ACTIVITY_TIMED_PULLS_CAP == 24)
_ev1 = _ev("半周年")   # 窗口内 1 个大活动 = 24 张
check("1 个档期：+24 抽",
      _ev1["timed_pulls"] == 24 and _ev1["pool_extra"] == 24
      and _ev1["pool_pulls"] == _ev1["free_int"] + 24,
      f"timed={_ev1['timed_pulls']} extra={_ev1['pool_extra']}")
_ev4 = _ev("夏活")   # 窗口内 4 个档期 = 96 张，仍只加 24
check("4 个档期（96 张）窗口内共发 96，但本期只算 24",
      _ev4["timed_total"] == 96 and _ev4["pool_extra"] == 24
      and _ev4["pool_pulls"] == _ev4["free_int"] + 24,
      f"total={_ev4['timed_total']} extra={_ev4['pool_extra']}")
check("副产物与井位都用封顶后的抽数",
      _ev4["byproducts"]["pulls"] == _ev4["pool_pulls"]
      and _ev4["spark_tiers"]["pulls"] == _ev4["pool_pulls"])
_custom = _ev("2027-03-10")   # 自定义日期，即便跨到 48 张也不加
check("自定义日期不在档期内 → 本期可用限时券 0",
      _custom["event"]["kind"] == "custom" and _custom["timed_pulls"] == 0
      and _custom["pool_extra"] == 0
      and _custom["pool_pulls"] == _custom["free_int"],
      f"timed={_custom['timed_pulls']} extra={_custom['pool_extra']} total={_custom['timed_total']}")

check("自由资源里的抽卡货币 = 现有 + 账本货币 + 一次性额外",
      abs(res["free_orundum"] - (12000 + res["ledger"]["orundum"])) < 1e-9,
      f"got={res['free_orundum']:.0f} vs {12000 + res['ledger']['orundum']:.0f}")

# 春节签到：除夕 / 初一 / 初五 / 初八，每节点 500 货币
print("\n== 春节签到（4 个节点 × 500 货币）==")
_spr = mod.spring_signin_between
check("常量：4 个节点、每节点 500、合计 2000",
      len(mod.SPRING_SIGNIN_NODES) == 4 and mod.SPRING_SIGNIN_ORUNDUM_EACH == 500
      and mod.SPRING_SIGNIN_ORUNDUM_TOTAL == 2000)
check("2027 年节点：除夕 2/5、初一 2/6、初五 2/10、初八 2/13",
      [d.isoformat() for d, _ in mod.spring_signin_nodes(2027)]
      == ["2027-02-05", "2027-02-06", "2027-02-10", "2027-02-13"],
      f"got={[d.isoformat() for d, _ in mod.spring_signin_nodes(2027)]}")
check("节点名顺序：除夕/初一/初五/初八",
      [lb for _, lb in mod.spring_signin_nodes(2027)] == ["除夕", "初一", "初五", "初八"])
check("整年 4 个节点 = 2000 货币",
      _spr(dt.date(2027, 1, 1), dt.date(2027, 12, 31))["orundum"] == 2000)
check("只跨除夕~初二 = 2 个节点 1000 货币",
      _spr(dt.date(2027, 2, 4), dt.date(2027, 2, 7))["orundum"] == 1000)
check("不含春节的窗口 = 0",
      _spr(dt.date(2027, 10, 1), dt.date(2027, 12, 31))["count"] == 0)
check("节点当天计入（2/5~2/5 = 除夕）",
      _spr(dt.date(2027, 2, 5), dt.date(2027, 2, 5))["labels"] == ["除夕"])
check("春节签到计入自由资源（跨春节的窗口 = 4 个节点）",
      with_fake_today(lambda: mod.event_calc(_ns(event="春节")))["spring_orundum"] == 2000,
      "跨春节应拿满 4 个节点")
check("自由资源恒等式仍成立（现有 + 兑换凭证 + 账本）",
      (lambda r: abs(r["free_pulls"] - (r["base_pulls"] + r["yellow_plan"]["total_pulls"]
                                        + r["ledger"]["free_pulls"])) < 1e-9)
      (with_fake_today(lambda: mod.event_calc(_ns(event="2027-08-01")))))


# 大额资源
res2 = with_fake_today(lambda: mod.event_calc(
    _ns(orundum=300000, event="2026-11-01")))
check("500+ 抽 → 满井且必得限定",
      res2["can_spark"] and res2["analysis"]["p_limited"] == 1.0,
      f"total={res2['total_int']}")
check("满井时 to_spark <= 0", res2["to_spark"] <= 0, f"got={res2['to_spark']}")

print("\n== 九、夏活与活动日历（覆盖到 2126）==")
r_summer = mod.resolve_event("夏活", dt.date(2026, 6, 1))
check("2026-06-01 → 夏活 = 2026-08-01", r_summer["date"] == dt.date(2026, 8, 1),
      f"{r_summer['date']}")
check("夏活持续 14 天（8/1~8/14）",
      r_summer["end_date"] == dt.date(2026, 8, 14), f"{r_summer['end_date']}")
check("夏活进行中（8/2）仍算这一期",
      mod.resolve_event("夏活", dt.date(2026, 8, 2))["date"] == dt.date(2026, 8, 1))
check("夏活结束次日（8/15）才取次年",
      mod.resolve_event("夏活", dt.date(2026, 8, 15))["date"] == dt.date(2027, 8, 1))
check("默认取最近档期（2026-07-01 → 夏活 8/1）",
      mod.resolve_event("", dt.date(2026, 7, 1))["date"] == dt.date(2026, 8, 1))
check("默认取最近档期（2026-09-01 → 半周年 11/1）",
      mod.resolve_event("", dt.date(2026, 9, 1))["date"] == dt.date(2026, 11, 1))
check("默认取最近档期（2026-12-01 → 春节）",
      mod.resolve_event("", dt.date(2026, 12, 1))["kind"] == "cny")
check("春节表覆盖到 2126", max(mod.SPRING_FESTIVAL_TABLE) >= 2126,
      f"max={max(mod.SPRING_FESTIVAL_TABLE)}")
check("春节表覆盖到 2025", min(mod.SPRING_FESTIVAL_TABLE) <= 2025)
check("春节表日期都落在 1/21~2/20",
      all((m == 1 and d >= 21) or (m == 2 and d <= 20)
          for m, d in mod.SPRING_FESTIVAL_TABLE.values()),
      "有越界日期")
check("2120 年也能解析出春节",
      mod.resolve_event("春节", dt.date(2120, 1, 1))["date"].year == 2120,
      f"{mod.resolve_event('春节', dt.date(2120, 1, 1))['date']}")
check("2126 年也能解析出春节",
      mod.resolve_event("春节", dt.date(2126, 1, 1))["date"].year == 2126,
      f"{mod.resolve_event('春节', dt.date(2126, 1, 1))['date']}")
check("表外年份（2130）不崩溃",
      mod.resolve_event("春节", dt.date(2130, 1, 1))["date"] > dt.date(2130, 1, 1))
# 表外年份用 19 年周期外推：拿表内数据自证精度（2107→2126 应逐日吻合）
check("19 年周期外推在表内 2088~2126 逐日吻合",
      all(mod._spring_festival_by_year(y) == dt.date(y, *mod.SPRING_FESTIVAL_TABLE[y])
          for y in range(2088, 2127)))
check("表外 2200 年春节仍落在 1/21~2/20",
      (lambda d: (d.month, d.day) == (1, 21) or (d.month == 1 and d.day >= 21)
       or (d.month == 2 and d.day <= 20))(mod._spring_festival_by_year(2200)),
      f"{mod._spring_festival_by_year(2200)}")
check("表外年份的春节档期不会从赠送里消失（2127 仍有春节赠送）",
      any(d["name"] == "春节" and d["begin"].year == 2127
          for d in mod.rewards_between(dt.date(2126, 10, 1), dt.date(2128, 12, 31),
                                       dt.date(2126, 10, 1))["details"]),
      "应在 2127 年找到春节赠送")
check("表外年份春节签到仍能算（2127 有 4 个节点）",
      mod.spring_signin_between(dt.date(2127, 1, 1), dt.date(2127, 12, 31))["count"] == 4,
      f"{mod.spring_signin_between(dt.date(2127, 1, 1), dt.date(2127, 12, 31))}")

print("\n== 十、大活动赠送 ==")
full = mod.event_rewards(mod.EVENT_DURATION_DAYS)
check("整个活动共 8300 抽卡货币", abs(full["orundum"] - 8300) < 1e-9, f"{full['orundum']}")
check("整个活动共 24 张限时券（官方口径）",
      full["timed_pulls"] == mod.EVENT_REWARD_TIMED_PULLS_TOTAL == 24,
      f"{full['timed_pulls']}")
check("发放表合计 = 24 且长度 = 14 天",
      sum(mod.EVENT_REWARD_TIMED_SCHEDULE) == 24
      and len(mod.EVENT_REWARD_TIMED_SCHEDULE) == 14,
      f"{mod.EVENT_REWARD_TIMED_SCHEDULE}")
check("24 券 + 8300 货币 ≈ 官方口径的 38 抽",
      abs(24 + 8300 / 600 - 37.83) < 0.01,
      f"{24 + 8300 / 600:.2f}")
d1 = mod.event_rewards(1)
check("活动第 1 天：12 张限时券", d1["timed_pulls"] == 12, f"{d1['timed_pulls']}")
check("活动第 1 天：抽卡货币 = 8300/14",
      abs(d1["orundum"] - 8300 / 14) < 1e-9, f"{d1['orundum']:.2f}")
check("活动第 2 天：13 张限时券", mod.event_rewards(2)["timed_pulls"] == 13)
check("活动第 0 天：0", mod.event_rewards(0)["timed_pulls"] == 0)
check("赠送随天数单调不减",
      all(mod.event_rewards(i)["timed_pulls"] <= mod.event_rewards(i + 1)["timed_pulls"]
          for i in range(0, 20)))
check("超出 14 天不再增加",
      mod.event_rewards(30)["timed_pulls"] == mod.event_rewards(14)["timed_pulls"])

# 窗口恰好覆盖半周年全程（11/1~11/14）
w = mod.rewards_between(dt.date(2026, 11, 1), dt.date(2026, 11, 14), dt.date(2026, 11, 1))
check("11/1~11/14 窗口拿到完整赠送",
      abs(w["orundum"] - 8300) < 1e-9 and w["timed_pulls"] == 24,
      f"货币={w['orundum']:.0f} 券={w['timed_pulls']}")
# 只到开启日（当天）→ 只有第 1 天
w0 = mod.rewards_between(dt.date(2026, 10, 10), dt.date(2026, 11, 1), dt.date(2026, 10, 10))
check("10/10~11/1 窗口只有第 1 天赠送",
      abs(w0["orundum"] - 8300 / 14) < 1e-9 and w0["timed_pulls"] == 12,
      f"货币={w0['orundum']:.0f} 券={w0['timed_pulls']}")
# 跨过多个档期
w2 = mod.rewards_between(dt.date(2026, 10, 10), dt.date(2027, 8, 1), dt.date(2026, 10, 10))
check("跨年窗口经过 4 个大活动",
      len(w2["details"]) == 4, f"got={len(w2['details'])}: "
                               f"{[d['name'] for d in w2['details']]}")
check("跨年窗口限时券 = 3×24 + 12 = 84",
      w2["timed_pulls"] == 3 * 24 + 12, f"got={w2['timed_pulls']}")

print("\n== 十一、兑换凭证月度上限 ==")
check("每月上限常量 = 258 票 / 38 抽",
      mod.YELLOW_MONTHLY_LIMIT == 258 and mod.YELLOW_FULL_PULLS == 38)
p1 = mod.yellow_exchange_plan(258, months_available=0)
check("258 票 / 同月内：本月 38 抽", p1["now_pulls"] == 38 and p1["later_pulls"] == 0)
p2 = mod.yellow_exchange_plan(520, months_available=0)
check("520 票 / 同月内：本月 38 抽，剩余 262 票",
      p2["now_pulls"] == 38 and p2["leftover"] == 262, f"{p2['now_pulls']}/{p2['leftover']}")
p3 = mod.yellow_exchange_plan(520, months_available=1)
check("520 票 / 跨 1 次刷新：本月 38 + 之后 38 = 76 抽",
      p3["now_pulls"] == 38 and p3["later_pulls"] == 38, f"{p3['now_pulls']}/{p3['later_pulls']}")
check("520 票 / 跨 1 次刷新后剩 4 票", p3["leftover"] == 4, f"{p3['leftover']}")
p4 = mod.yellow_exchange_plan(300, months_available=0)
check("300 票 / 同月内：本月 38 抽，余 42 票（不足 258）",
      p4["now_pulls"] == 38 and p4["leftover"] == 42)
p5 = mod.yellow_exchange_plan(257, months_available=0)
check("257 票 / 同月内：本月 18 抽（差 1 票进不了第 5 档）",
      p5["now_pulls"] == 18, f"{p5['now_pulls']}")
p6 = mod.yellow_exchange_plan(516, months_available=1)
check("516 票 / 跨 1 次刷新：正好 76 抽，无剩余",
      p6["now_pulls"] == 38 and p6["later_pulls"] == 38 and p6["leftover"] == 0,
      f"{p6['now_pulls']}/{p6['later_pulls']}/{p6['leftover']}")
check("0 票：0 抽", mod.yellow_exchange_plan(0, 0)["now_pulls"] == 0)
check("票数越多抽数单调不减",
      all(mod.yellow_exchange_plan(i, 0)["now_pulls"]
          <= mod.yellow_exchange_plan(i + 1, 0)["now_pulls"] for i in range(0, 600, 7)))
check("每个月最多 38 抽（不会超发）",
      all(mod.yellow_exchange_plan(i, 0)["now_pulls"] <= 38 for i in range(0, 1200, 13)))

print("\n== 十二、刷新次数计算 ==")
check("同月内 = 0 次（本月额度已可用）",
      mod.month_refreshes_between(dt.date(2026, 10, 10), dt.date(2026, 10, 20)) == 0)
check("跨 11/1 = 1 次",
      mod.month_refreshes_between(dt.date(2026, 10, 10), dt.date(2026, 11, 1)) == 1)
check("10/10 → 12/15 = 2 次",
      mod.month_refreshes_between(dt.date(2026, 10, 10), dt.date(2026, 12, 15)) == 2)
check("跨度 1 年 = 12 次",
      mod.month_refreshes_between(dt.date(2026, 10, 10), dt.date(2027, 10, 10)) == 12)
check("end <= start = 0 次",
      mod.month_refreshes_between(dt.date(2026, 10, 10), dt.date(2026, 10, 1)) == 0)

print("\n== 十三、抽卡副产物 ==")
bp = mod.pulls_byproducts(100)
check("100 抽：3★ 预计 40 只", abs(bp["count_3"] - 40) < 1e-9, f"{bp['count_3']}")
check("100 抽：4★ 预计 50 只", abs(bp["count_4"] - 50) < 1e-9)
check("100 抽：5★ 预计 8 只", abs(bp["count_5"] - 8) < 1e-9)
check("100 抽：6★ 预计 = 100/期望", abs(bp["count_6"] - 100 / ev) < 1e-9)
check("4★ 每只 1 张兑换凭证 → 50 张", abs(bp["yellow_full"]["four"] - 50) < 1e-9)
check("5★ 每只 13 张兑换凭证 → 104 张", abs(bp["yellow_full"]["five"] - 104) < 1e-9)
check("6★ 只算重复 → (期望6星-1)×10",
      abs(bp["yellow_full"]["six"] - max(0.0, 100 / ev - 1) * 10) < 1e-9)
check("次级凭证 = 3★ × 5 = 200", abs(bp["green"] - 200) < 1e-9, f"{bp['green']}")
check("兑换凭证合计 = 三项之和",
      abs(bp["yellow_full_total"] - sum(bp["yellow_full"].values())) < 1e-9)
check("0 抽副产物全为 0",
      mod.pulls_byproducts(0)["yellow_full_total"] == 0
      and mod.pulls_byproducts(0)["green"] == 0)
check("副产物随抽数线性增长",
      abs(mod.pulls_byproducts(200)["yellow_full"]["four"]
          - 2 * mod.pulls_byproducts(100)["yellow_full"]["four"]) < 1e-9)
bp_r = mod.pulls_byproducts(100, five_star_repeat_only=True)
check("5★ 按“重复 5 张”口径可用",
      abs(bp_r["yellow_full"]["five"] - 40) < 1e-9, f"{bp_r['yellow_full']['five']}")

print("\n== 十四、井位递减 ==")
check("250 抽：300 井 0 个，200 井 1 个，75 井 0 个",
      (lambda t: (t["limited_count"], t["old_limited_count"], t["five_star_count"]))
      (mod.spark_tiers(250)) == (0, 1, 0),
      f"{mod.spark_tiers(250)}")
check("300 抽：300 井 1 个，剩 0",
      (lambda t: (t["limited_count"], t["left_contracts"]))(mod.spark_tiers(300)) == (1, 0))
check("600 抽：300 井 2 个",
      mod.spark_tiers(600)["limited_count"] == 2, f"{mod.spark_tiers(600)['limited_count']}")
check("275 抽：200 井 1 个 + 75 井 1 个",
      (lambda t: (t["old_limited_count"], t["five_star_count"]))
      (mod.spark_tiers(275)) == (1, 1), f"{mod.spark_tiers(275)}")
check("74 抽：什么都换不了", mod.spark_tiers(74)["total_operators"] == 0)
check("75 抽：75 井 1 个", mod.spark_tiers(75)["five_star_count"] == 1)
check("0 抽：0 个", mod.spark_tiers(0)["total_operators"] == 0)
check("总角色数 = 三档之和",
      all(mod.spark_tiers(i)["total_operators"]
          == mod.spark_tiers(i)["limited_count"]
          + mod.spark_tiers(i)["old_limited_count"]
          + mod.spark_tiers(i)["five_star_count"]
          for i in range(0, 1000, 37)))

print("\n== 十五、本月已换档位（资源折算用）==")
# yellow_stage_to_consumed：档位票面值 → 本月在当前档已消耗
check("已换到 10 → 消耗 0（本月没换过）", mod.yellow_stage_to_consumed(10) == 0)
check("已换到 18 → 消耗 10", mod.yellow_stage_to_consumed(18) == 10)
check("已换到 40 → 消耗 28", mod.yellow_stage_to_consumed(40) == 28)
check("已换到 70 → 消耗 68", mod.yellow_stage_to_consumed(70) == 68)
check("已换到 120 → 消耗 138", mod.yellow_stage_to_consumed(120) == 138)
check("已换满 258 → 消耗 258", mod.yellow_stage_to_consumed(258) == 258)
check("换算单调不减",
      all(mod.yellow_stage_to_consumed(v) <= mod.yellow_stage_to_consumed(w)
          for v, w in zip([10, 18, 40, 70, 120], [18, 40, 70, 120, 258])))

s = mod.yellow_month_state(516, consumed_this_month=0)
check("516 票 / 没换过：本月 38 抽，消耗 258", s["claim_pulls"] == 38 and s["month_spent"] == 258,
      f"{s['claim_pulls']}/{s['month_spent']}")
check("516 票 / 没换过：余 258 票，被月上限挡住",
      s["extra"] == 258 and s["block_reason"] == "month_limit",
      f"extra={s['extra']} reason={s['block_reason']}")
check("516 票 / 没换过：档位明细为 5 档全领",
      s["claimed"] == [(10, 1), (18, 2), (40, 5), (70, 10), (120, 20)], f"{s['claimed']}")

s2 = mod.yellow_month_state(516, consumed_this_month=28)      # 已换到 40
check("516 票 / 已换到 40：本月还能领 35 抽（40+70+120 三档）",
      s2["claim_pulls"] == 35, f"{s2['claim_pulls']}")
check("516 票 / 已换到 40：跳过已领的 10/18 档，实付 40+70+120=230",
      s2["claimed"] == [(40, 5), (70, 10), (120, 20)], f"{s2['claimed']}")
check("516 票 / 已换到 40：本次消耗 230，本月累计 258",
      sum(c for c, _ in s2["claimed"]) == 230 and s2["month_spent"] == 258,
      f"sum={sum(c for c, _ in s2['claimed'])} total={s2['month_spent']}")
check("516 票 / 已换到 40：余 258 票", s2["extra"] == 258, f"{s2['extra']}")

s3 = mod.yellow_month_state(100, consumed_this_month=28)      # 100 票 + 已换到 40
check("100 票 / 已换到 40：只够领到 40 档 = 5 抽",
      s3["claim_pulls"] == 5, f"{s3['claim_pulls']}")
check("100 票 / 已换到 40：实付 40，余 32",
      s3["claimed"] == [(40, 5)] and s3["extra"] == 32,
      f"{s3['claimed']} extra={s3['extra']}")
check("100 票 / 已换到 40：因票不够而停止",
      s3["block_reason"] == "no_tickets", f"{s3['block_reason']}")

# 用户给的验收例：500 票、本月已换到 18 档 → 37 抽、余 242
s500 = mod.yellow_month_state(500, consumed_this_month=10)
check("500 票 / 已换到 18：37 抽（18+40+70+120），余 242",
      s500["claim_pulls"] == 37 and s500["extra"] == 242,
      f"{s500['claim_pulls']}/{s500['extra']}")
check("500 票 / 已换到 18：不再把第 1 档的 1 抽白送一次",
      all(c > 0 for c, _ in s500["claimed"]), f"{s500['claimed']}")

s4 = mod.yellow_month_state(257, consumed_this_month=0)
check("257 票 / 没换过：18 抽（领到 70 档）",
      s4["claim_pulls"] == 18 and s4["block_reason"] == "no_tickets", f"{s4['claim_pulls']}")
s5 = mod.yellow_month_state(50, consumed_this_month=0)
check("50 票 / 没换过：3 抽（领到 18 档）", s5["claim_pulls"] == 3, f"{s5['claim_pulls']}")
s6 = mod.yellow_month_state(258, consumed_this_month=0)
check("258 票 / 没换过：38 抽且无余票",
      s6["claim_pulls"] == 38 and s6["extra"] == 0 and s6["block_reason"] == "month_limit",
      f"{s6['claim_pulls']}/{s6['extra']}/{s6['block_reason']}")
s7 = mod.yellow_month_state(0, consumed_this_month=0)
check("0 票：0 抽", s7["claim_pulls"] == 0)
s8 = mod.yellow_month_state(1000, consumed_this_month=0)
check("票很多时本月仍只消耗 258 票",
      s8["month_spent"] == 258 and s8["claim_pulls"] == 38, f"{s8['month_spent']}")
check("票再多本月抽数也不会超过 38",
      all(mod.yellow_month_state(i, 0)["claim_pulls"] <= 38 for i in range(0, 2000, 17)))
check("余票随票数单调不减",
      all(mod.yellow_month_state(i, 0)["extra"] <= mod.yellow_month_state(i + 1, 0)["extra"]
          for i in range(0, 1000, 23)))
check("任何情况下 claim_pulls 与 extra 都不为负",
      all(mod.yellow_month_state(i, 0)["claim_pulls"] >= 0
          and mod.yellow_month_state(i, 0)["extra"] >= 0 for i in range(0, 800, 11)))

print("\n== 十六、月度浮动资源（新/老玩家两套口径）==")
# 表自身的一致性
check("表覆盖 12 个月", len({m for e in mod.MONTHLY_EVENT_ESTIMATES for m in e["months"]}) == 12,
      f"got={sorted({m for e in mod.MONTHLY_EVENT_ESTIMATES for m in e['months']})}")
check("每月只出现一次",
      len([m for e in mod.MONTHLY_EVENT_ESTIMATES for m in e["months"]]) == 12)
check("新玩家高级货币 >= 老玩家高级货币",
      all(e["newbie_originite"] >= e["veteran_originite"] for e in mod.MONTHLY_EVENT_ESTIMATES))
check("大活动月份（1/4/5/8/11）抽卡参考值都有",
      all(e["pulls_ref"] for e in mod.MONTHLY_EVENT_ESTIMATES
          if set(e["months"]) & {1, 4, 5, 8, 11}),
      str([(e["months"], e["pulls_ref"]) for e in mod.MONTHLY_EVENT_ESTIMATES
           if set(e["months"]) & {1, 4, 5, 8, 11}]))
check("非大活动月份参考值为空（视活动而定）",
      all(not e["pulls_ref"] for e in mod.MONTHLY_EVENT_ESTIMATES
          if not (set(e["months"]) & {1, 4, 5, 8, 11})))
check("高级货币估值都在合理量级（10~50）",
      all(10 <= e["veteran_originite"] <= 50 and 10 <= e["newbie_originite"] <= 50
          for e in mod.MONTHLY_EVENT_ESTIMATES))

# 单月窗口
one = mod.monthly_float_resources(dt.date(2026, 8, 1), dt.date(2026, 8, 14))
check("只跨 8 月时只算夏活那档（37 颗）",
      one["originite"] == 37 and len(one["rows"]) == 1, f"{one['originite']} {one['rows']}")
check("37 颗高级货币 = 11.1 抽",
      abs(one["pulls_from_originite"] - 37 * 180 / 600) < 1e-9,
      f"{one['pulls_from_originite']:.2f}")

# 跨年窗口逐月累加
full = mod.monthly_float_resources(dt.date(2026, 10, 10), dt.date(2027, 8, 14))
expect_months = sorted([10, 11, 12, 1, 2, 3, 4, 5, 6, 7, 8])
check("跨年窗口覆盖 11 个自然月",
      sorted(full["months"]) == expect_months, f"{full['months']}")
# 老玩家：10月40 + 11月40 + 12月28 + 1月30 + 2月22 + 3月22 + 4月30 + 5月30 + 6月28 + 7月28 + 8月37
expect_vet = 40 + 40 + 28 + 30 + 22 + 22 + 30 + 30 + 28 + 28 + 37
check("老玩家口径逐月累加正确", full["veteran_originite"] == expect_vet,
      f"got={full['veteran_originite']} want={expect_vet}")
expect_new = 40 + 40 + 40 + 34 + 28 + 28 + 40 + 40 + 40 + 40 + 37
check("新玩家口径逐月累加正确", full["newbie_originite"] == expect_new,
      f"got={full['newbie_originite']} want={expect_new}")
check("新玩家 >= 老玩家（整段窗口）",
      full["newbie_originite"] >= full["veteran_originite"])
check("veteran=True 时取老玩家值",
      mod.monthly_float_resources(dt.date(2026, 10, 10), dt.date(2027, 8, 14),
                                  veteran=True)["originite"] == expect_vet)
check("veteran=False 时取新玩家值",
      mod.monthly_float_resources(dt.date(2026, 10, 10), dt.date(2027, 8, 14),
                                  veteran=False)["originite"] == expect_new)
# 关键回归：中间月份缺失时不能把其它月份算少
only_oct = mod.monthly_float_resources(dt.date(2026, 10, 1), dt.date(2026, 10, 31))
check("只跨 10 月时按 40 颗计（不因 9 月缺失而砍半）",
      only_oct["originite"] == 40, f"got={only_oct['originite']}")
check("窗口为空/反向也能处理",
      mod.monthly_float_resources(dt.date(2026, 10, 10), dt.date(2026, 10, 10))["originite"] >= 0)
check("pulls_ref 不参与合计（只作参考）",
      "pulls_ref" in full["rows"][0] and "pulls_ref" not in ("total", "total_pulls"))

print("\n== 十七、资源折算副产物输出 ==")
report = mod.report_resources(
    orundum=18000, originite=12, tickets=0, ten_tickets=0,
    use_originite=True, originite_keep=0, current_pity=0,
    cfg=mod.DEFAULT_CONFIG, yellow=258, yellow_stage=10)
check("资源折算报表含副产物段落", "砸下去能回多少票" in report)
check("含兑换凭证合计与次级凭证", "兑换凭证 " in report and "次级凭证" in report and "合计" in report)
check("含各稀有度数量", all(f"{s}★" in report for s in ("3", "4", "5", "6")))
check("副产物数值与独立计算一致",
      f"{mod.pulls_byproducts(71)['yellow_full_total']:.1f}" in report,
      "未找到期望的兑换凭证合计值")
check("说明里给出了需要几个月",
      "需要" in report and "个月兑换完" in report)

print("\n== 十八、兑换凭证换尽（逐月换到换不动）==")
check("637 票 → 84 抽 / 3 次 / 余 53（换满 258 才算）",
      (lambda p: (p["total_pulls"], p["months_needed"], p["leftover"]))
      (mod.yellow_exchange_until_done(637)) == (84, 3, 53),
      f"{mod.yellow_exchange_until_done(637)}")
check("895 票 → 122 抽 / 4 次 / 余 53（换满 258 才算）",
      (lambda p: (p["total_pulls"], p["months_needed"], p["leftover"]))
      (mod.yellow_exchange_until_done(895)) == (122, 4, 53),
      f"{mod.yellow_exchange_until_done(895)}")
check("520 票 → 76 抽 / 2 次 / 余 4",
      (lambda p: (p["total_pulls"], p["months_needed"], p["leftover"]))
      (mod.yellow_exchange_until_done(520)) == (76, 2, 4))
check("516 票 → 76 抽 / 2 次 / 刚好换完",
      (lambda p: (p["total_pulls"], p["months_needed"], p["leftover"]))
      (mod.yellow_exchange_until_done(516)) == (76, 2, 0))
check("258 票 → 38 抽 / 1 次 / 余 0",
      (lambda p: (p["total_pulls"], p["months_needed"], p["leftover"]))
      (mod.yellow_exchange_until_done(258)) == (38, 1, 0))
check("300 票 → 41 抽 / 2 次（第 2 个月只换 3 抽）",
      (lambda p: (p["total_pulls"], p["months_needed"]))
      (mod.yellow_exchange_until_done(300)) == (41, 2),
      f"{mod.yellow_exchange_until_done(300)}")
check("10 票 → 1 抽", mod.yellow_exchange_until_done(10)["total_pulls"] == 1)
check("9 票 → 0 抽", mod.yellow_exchange_until_done(9)["total_pulls"] == 0)
check("0 票 → 0 抽 / 0 次",
      (lambda p: (p["total_pulls"], p["months_needed"]))
      (mod.yellow_exchange_until_done(0)) == (0, 0))
# 传入“本月已换到 40 档”（已消耗 28）时应扣掉本月已领档位
p_c40 = mod.yellow_exchange_until_done(637, consumed_this_month=28)
check("637 票 / 已换到 40 档 → 本月 35 抽、共 81 抽、3 次、余 53",
      (p_c40["now_pulls"], p_c40["total_pulls"], p_c40["months_needed"], p_c40["leftover"])
      == (35, 81, 3, 53),
      f"{p_c40['now_pulls']}/{p_c40['total_pulls']}/{p_c40['months_needed']}/{p_c40['leftover']}")
check("637 票 / 已换到 40 档：逐月明细为 35+38+8",
      [b["pulls"] for b in p_c40["batches"]] == [35, 38, 8],
      f"{[b['pulls'] for b in p_c40['batches']]}")
check("被月上限截断时不会提前收尾（余票下月继续换）",
      p_c40["months_needed"] > 1)
check("本月全部换完时不产生多余月份",
      mod.yellow_exchange_until_done(258, consumed_this_month=0)["months_needed"] == 1)
check("每月最多换 38 抽",
      all(b["pulls"] <= 38 for b in mod.yellow_exchange_until_done(2000)["batches"]))
check("逐月消耗之和 + 余票 = 总票数（含已换档位）",
      all(sum(b["spent"] for b in mod.yellow_exchange_until_done(y, consumed_this_month=c)["batches"])
          + mod.yellow_exchange_until_done(y, consumed_this_month=c)["leftover"] == y
          for y in range(0, 1200, 37) for c in (0, 10, 28, 68, 138)))
check("抽数随票数单调不减",
      all(mod.yellow_exchange_until_done(i)["total_pulls"]
          <= mod.yellow_exchange_until_done(i + 1)["total_pulls"] for i in range(0, 900)))
check("换不满整月就收尾（不会出现比满月还低的继续换）",
      all(mod.yellow_exchange_until_done(y)["batches"][-1]["pulls"] <= 38
          for y in range(10, 1500, 13)))

print("\n== 十九、副产物价值文案（含月数）==")
txt = mod.byproduct_value_text(895)
check("895 张 → 122 抽 / 4 个月（与换尽一致）",
      "122 抽" in txt and "4 个月" in txt, txt)
check("637 张 → 84 抽 / 3 个月",
      "84 抽" in mod.byproduct_value_text(637) and "3 个月" in mod.byproduct_value_text(637),
      mod.byproduct_value_text(637))
check("520 张 → 76 抽 / 2 个月",
      "76 抽" in mod.byproduct_value_text(520) and "2 个月" in mod.byproduct_value_text(520),
      mod.byproduct_value_text(520))
check("400 张 → 56 抽 / 2 个月",
      "56 抽" in mod.byproduct_value_text(400) and "2 个月" in mod.byproduct_value_text(400),
      mod.byproduct_value_text(400))
check("文案里不再出现“需次月刷新且每月限一次”",
      "注意需次月刷新" not in mod.byproduct_value_text(400))
check("不足最小档位时给出说明",
      "不足最小档位" in mod.byproduct_value_text(5), mod.byproduct_value_text(5))
check("文案含每 258 票 = 38 抽的口径",
      "258 票 = 38 抽" in mod.byproduct_value_text(600))
check("530 张（2 满月 + 余 14 票换 1 抽）→ 77 抽 / 3 个月",
      "77 抽" in mod.byproduct_value_text(530) and "3 个月" in mod.byproduct_value_text(530),
      mod.byproduct_value_text(530))
check("不满一个月按一个月算：10 票也要 1 个月",
      "1 个月" in mod.byproduct_value_text(10), mod.byproduct_value_text(10))

print("\n== 二十、自造货币 ==")
check("月卡党 320 货币/天", mod.crafted_orundum_per_day(True) == 320)
check("非月卡 240 货币/天", mod.crafted_orundum_per_day(False) == 240)
check("常量一致",
      mod.CRAFT_JADE_PER_DAY_MONTHLY_CARD == 320 and mod.CRAFT_JADE_PER_DAY_NO_CARD == 240)
check("1-7 期望值为 4.8 理智/土块",
      abs(mod.CRAFT_ROCK_SANITY_PER_UNIT - 4.8) < 1e-9)
check("月卡日理智 = 240 + 80 = 320", mod.CRAFT_DAILY_SANITY_CARD == 320)
check("非月卡日理智 = 240", mod.CRAFT_DAILY_SANITY_NO_CARD == 240)
check("制造站比例 2:1", mod.CRAFT_ROCKS_PER_FRAGMENT == 2)
check("1 高级货币碎片 = 10 抽卡货币", mod.CRAFT_ORUNDUM_PER_FRAGMENT == 10)
# 复算：320 / 4.8 / 2 * 10 = 333.33 → 按用户口径取 320
check("月卡推算 320÷4.8÷2×10 ≈ 333 → 保守取 320",
      abs(320 / 4.8 / 2 * 10 - 333.33) < 0.01)
check("非月卡推算 240÷4.8÷2×10 = 250（正好整数）",
      abs(240 / 4.8 / 2 * 10 - 250) < 1e-9)
check("说明文案含关键数字",
      "4.8" in mod.craft_jade_detail(True) and "320" in mod.craft_jade_detail(True))
check("非月卡文案含 240",
      "240" in mod.craft_jade_detail(False), mod.craft_jade_detail(False))

print("\n== 二十一、月卡购买（张数 / 花费 / 附赠高级货币）==")
check("常量：30 元/张、立得 6 高级货币",
      mod.MONTHLY_CARD_PRICE == 30 and mod.MONTHLY_CARD_ORIGINITE == 6)

_mcp = mod.monthly_card_plan
_p1 = _mcp(dt.date(2026, 1, 15), dt.date(2026, 7, 20))
check("1/15 → 7/20 = 6 张（次月到目标月：2~7 月）",
      _p1["months"] == 6 and _p1["labels"] == ["2月", "3月", "4月", "5月", "6月", "7月"],
      f"{_p1['labels']}")
check("6 张 = 180 元", _p1["cost"] == 180, f"got={_p1['cost']}")
check("6 张 = 36 高级货币", _p1["originite"] == 36, f"got={_p1['originite']}")
check("36 高级货币 = 6,480 货币 = 10.8 抽",
      _p1["orundum"] == 36 * 180 and abs(_p1["pulls"] - 10.8) < 1e-9,
      f"{_p1['orundum']} 货币 / {_p1['pulls']:.2f} 抽")
check("同月内收工 = 0 张、0 元",
      (lambda p: p["months"] == 0 and p["cost"] == 0 and p["pulls"] == 0)(
          _mcp(dt.date(2026, 11, 7), dt.date(2026, 11, 30))))
check("跨年 11/7 → 次年 2/19 = 3 张（12、1、2 月）= 90 元",
      (lambda p: p["months"] == 3 and p["labels"] == ["12月", "1月", "2月"]
          and p["cost"] == 90 and p["originite"] == 18)(
          _mcp(dt.date(2026, 11, 7), dt.date(2027, 2, 19))))
check("刚好跨一个月 = 1 张",
      _mcp(dt.date(2026, 12, 20), dt.date(2027, 1, 3))["months"] == 1)
check("反向传参结果一致（自动交换起止）",
      _mcp(dt.date(2026, 7, 20), dt.date(2026, 1, 15))["months"] == 6)
check("月份标签数恒等于张数",
      all(len(_mcp(dt.date(2026, 1, 1), dt.date(2026, m, 1))["labels"])
          == _mcp(dt.date(2026, 1, 1), dt.date(2026, m, 1))["months"]
          for m in range(1, 13)))

_card_on = with_fake_today(lambda: mod.event_calc(_ns(event="春节", monthly_card=True)))
_card_off = with_fake_today(lambda: mod.event_calc(_ns(event="春节", monthly_card=False)))
check("抽数预估认月卡：10/10 → 春节结束 = 4 张 = 120 元 / 24 高级货币",
      _card_on["card_months"] == 4 and _card_on["card_cost"] == 120
      and _card_on["card_originite"] == 24,
      f"{_card_on['card_months']} 张 / {_card_on['card_cost']} 元")
check("没勾月卡则一张都不算",
      _card_off["card_months"] == 0 and _card_off["card_pulls"] == 0)
check("月卡增量 = 每日 200 货币 + 每张 6 高级货币（都已在账本里，不重复加）",
      abs((_card_on["free_pulls"] - _card_off["free_pulls"])
          - (_card_on["gained"] - _card_off["gained"])) < 1e-9,
      f"{_card_on['free_pulls'] - _card_off['free_pulls']:.2f}")
check("月卡附赠高级货币 = 4 张 × 6 = 24 颗",
      _card_on["card_originite"] == 24, f"{_card_on['card_originite']}")

_pb = mod.period_bonus(dt.date(2026, 1, 15), dt.date(2026, 7, 20), monthly_card=True)
_pb0 = mod.period_bonus(dt.date(2026, 1, 15), dt.date(2026, 7, 20), monthly_card=False)
check("period_bonus 同口径：6 张 = 180 元、附赠 36 高级货币 = 10.8 抽",
      abs(_pb["card_pulls"] - 10.8) < 1e-9 and _pb0["card_pulls"] == 0
      and _pb["card_cost"] == 180 and _pb["card_originite"] == 36,
      f"card_pulls={_pb['card_pulls']:.2f} / {_pb['card_cost']} 元")
check("月卡每天 200 货币也在账本里（不再由调用方预先加进日收入）",
      abs((_pb["pulls"] - _pb0["pulls"])
          - (10.8 + _pb["ledger"]["by_label"].get("月卡每日", {}).get("pulls", 0))) < 1e-9,
      f"{_pb['pulls'] - _pb0['pulls']:.2f} 抽")


print("\n== 二十二、逐日日历引擎（v3 口径）==")
_L = mod.daily_ledger


def _lab(start, end, label, **kw):
    return _L(start, end, **kw)["by_label"].get(label, {"orundum": 0.0, "originite": 0,
                                                       "tickets": 0, "timed": 0, "pulls": 0.0})


check("今天不算日常：0 天窗口 = 0 收入（11/20 没有任何节点）",
      _L(dt.date(2026, 11, 20), dt.date(2026, 11, 20))["free_pulls"] == 0)
check("每日任务 100 货币/天",
      _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "每日任务")["orundum"] == 100)
check("周一：周常 500",
      _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "每周任务")["orundum"] == 500)
check("周一：每周副本 1800",
      _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "每周副本作战")["orundum"] == 1800)
check("每月 1 号：次级凭证 600 货币 + 3 张券（整店 4 抽）",
      _lab(dt.date(2026, 11, 30), dt.date(2026, 12, 1), "次级凭证商店")["orundum"] == 600
      and _lab(dt.date(2026, 11, 30), dt.date(2026, 12, 1), "次级凭证商店")["tickets"] == 3)
check("次级凭证 3 个月（12/1、1/1、2/1）= 1800 货币 + 9 券 = 12 抽（对齐权威工具）",
      abs(_L(dt.date(2026, 11, 2), dt.date(2027, 2, 1))["by_label"]["次级凭证商店"]["pulls"] - 12) < 1e-9,
      f"{_L(dt.date(2026, 11, 2), dt.date(2027, 2, 1))['by_label']['次级凭证商店']['pulls']}")
check("每月 17 号：签到 1 张券",
      _lab(dt.date(2026, 12, 16), dt.date(2026, 12, 17), "每月签到")["tickets"] == 1)
check("每月 7/15/22/30 号：维护 200 货币",
      _lab(dt.date(2026, 12, 6), dt.date(2026, 12, 7), "维护补偿")["orundum"] == 200
      and _lab(dt.date(2026, 12, 29), dt.date(2026, 12, 30), "维护补偿")["orundum"] == 200)
check("每月 15 号复刻（老玩家）：2000 货币 + 3 抽",
      _lab(dt.date(2026, 12, 14), dt.date(2026, 12, 15), "复刻活动")["orundum"] == 2000
      and _lab(dt.date(2026, 12, 14), dt.date(2026, 12, 15), "复刻活动")["tickets"] == 3)
check("每月 10 号新活动（非大活动月）：28 高级货币 + 3 抽",
      _lab(dt.date(2026, 12, 9), dt.date(2026, 12, 10), "每月新活动")["originite"] == 28
      and _lab(dt.date(2026, 12, 9), dt.date(2026, 12, 10), "每月新活动")["tickets"] == 3)
check("大活动月（11 月半周年）不排新活动",
      _lab(dt.date(2026, 11, 9), dt.date(2026, 11, 10), "每月新活动")["originite"] == 0)
check("大活动月（2 月春节）不排新活动",
      _lab(dt.date(2027, 2, 9), dt.date(2027, 2, 10), "每月新活动")["originite"] == 0)
check("1 月无大版本 → 1/10 有新活动",
      _lab(dt.date(2027, 1, 9), dt.date(2027, 1, 10), "每月新活动")["originite"] == 28)
check("每月 15 号复刻（新玩家）：36 高级货币 + 3 抽",
      _lab(dt.date(2026, 12, 14), dt.date(2026, 12, 15), "复刻活动",
           veteran=False)["originite"] == 36
      and _lab(dt.date(2026, 12, 14), dt.date(2026, 12, 15), "复刻活动",
               veteran=False)["tickets"] == 3)
check("大版本第 1 天：600 货币 + 10 高级货币 + 3 抽 + 11 张限时券",
      _lab(dt.date(2027, 4, 30), dt.date(2027, 5, 1), "周年庆·开幕")["orundum"] == 600
      and _lab(dt.date(2027, 4, 30), dt.date(2027, 5, 1), "周年庆·开幕")["originite"] == 10
      and _lab(dt.date(2027, 4, 30), dt.date(2027, 5, 1), "周年庆·开幕")["tickets"] == 3
      and _lab(dt.date(2027, 4, 30), dt.date(2027, 5, 1), "周年庆·开幕")["timed"] == 11)
check("大版本第 1 天另有 5 高级货币停机补偿",
      _lab(dt.date(2027, 4, 30), dt.date(2027, 5, 1), "周年庆·停机维护")["originite"] == 5)
check("大版本第 8 天：16 高级货币",
      _lab(dt.date(2027, 5, 7), dt.date(2027, 5, 8), "周年庆·节点")["originite"] == 16)
check("大版本第 15 天：10 高级货币（春节是正月十五）",
      _lab(dt.date(2027, 5, 14), dt.date(2027, 5, 15), "周年庆·收尾")["originite"] == 10)
check("大版本第 2~14 天：每天 600 货币 + 1 张限时券",
      _lab(dt.date(2027, 5, 1), dt.date(2027, 5, 2), "周年庆·每日")["orundum"] == 600
      and _lab(dt.date(2027, 5, 1), dt.date(2027, 5, 2), "周年庆·每日")["timed"] == 1)
check("大版本前 7 天：前瞻预告 1200 货币",
      _lab(dt.date(2027, 4, 23), dt.date(2027, 4, 24), "周年庆·前瞻预告")["orundum"] == 1200)
check("春节签到：除夕 500 货币",
      _lab(dt.date(2027, 2, 4), dt.date(2027, 2, 5), "春节签到·除夕")["orundum"] == 500)
check("月卡：每天 200 货币",
      _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "月卡每日",
           monthly_card=True)["orundum"] == 200)
check("月卡：次月 1 号买一张，立得 6 高级货币",
      _lab(dt.date(2026, 11, 30), dt.date(2026, 12, 1), "月卡购买",
           monthly_card=True)["originite"] == 6)
check("月卡：本月这张不再重复买",
      _L(dt.date(2026, 11, 1), dt.date(2026, 11, 30), monthly_card=True)["card_months"] == 0)
check("自造货币：无月卡 240、有月卡 320/天",
      _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "自造货币",
           craft_jade=True)["orundum"] == 240
      and _lab(dt.date(2026, 11, 8), dt.date(2026, 11, 9), "自造货币", craft_jade=True,
               monthly_card=True)["orundum"] == 320)
check("限时券：活动第 1 天 11 张、第 8 天累计 18 张",
      _L(dt.date(2027, 4, 30), dt.date(2027, 5, 1))["timed_live"] == 11
      and _L(dt.date(2027, 4, 30), dt.date(2027, 5, 8))["timed_live"] == 18)
check("限时券：活动结束后归零",
      _L(dt.date(2027, 4, 30), dt.date(2027, 5, 20))["timed_live"] == 0)
check("账本：四类相加 = 自由资源",
      (lambda l: abs(sum(l["by_cat"][c]["pulls"] for c in mod.LEDGER_CATS)
                     - l["free_pulls"]) < 1e-9)(
          _L(dt.date(2026, 11, 7), dt.date(2027, 9, 1), monthly_card=True)))
check("账本：合计 = 自由 + 本期可用限时券",
      (lambda l: abs(l["total_pulls"] - l["free_pulls"] - l["timed_live"]) < 1e-9)(
          _L(dt.date(2026, 10, 1), dt.date(2026, 11, 14))))

print("\n== 二十三、两段算法一致性（目标抽数 ↔ 目标日）==")
_base = dict(orundum=6000, originite=0, tickets=0, ten=0, yellow=120, yellow_stage=10,
             monthly_card=False, craft_jade=False, od_per_run=240, no_spark=False,
             signin=True, green=True, maintenance=True, extra=0, new_player=False)
_cur = mod.pulls_from_resources(orundum=6000)["total_pulls"] + 8


def _freeze_today(day):
    """把「今天」钉在 day 上，让两段算法在同一根起跑线上比对。"""
    class _D(dt.date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)
    return _D


for _t in (100, 200, 300):
    _d = mod.first_day_reaching(_cur, _t, yellow=120, yellow_base=8,
                                start=dt.date(2026, 11, 7))
    _day = dt.date(2026, 11, 7) + dt.timedelta(days=_d)
    mod._dt.date = _freeze_today(dt.date(2026, 11, 7))
    try:
        _evc = mod.event_calc(_ns(event=_day.isoformat(), **_base))
    finally:
        mod._dt.date = _real_date
    check(f"目标 {_t} 抽 → 那天至少 {_t} 抽（实际 {_evc['pool_pulls']}）",
          _evc["pool_pulls"] >= _t and _evc["pool_pulls"] - _t < 40,
          f"day={_day} pool={_evc['pool_pulls']}")
check("已达成时返回 0 天", mod.first_day_reaching(500, 300, start=dt.date(2026, 11, 7)) == 0)
check("1 抽缺口 = 600 货币：100 + 200(维护) + 周一 2300 → 第 3 天",
      mod.first_day_reaching(0, 1, start=dt.date(2026, 11, 20)) == 3,
      f"{mod.first_day_reaching(0, 1, start=dt.date(2026, 11, 20))}")
check("活动期间：今天那张限时券算已用掉，次日起才计入",
      mod.major_timed_live(dt.date(2026, 11, 10), dt.date(2026, 11, 10)) == 0
      and mod.major_timed_live(dt.date(2026, 11, 11), dt.date(2026, 11, 10)) == 1)
check("活动期间 1 抽缺口：次日 +100 日常 +600 大版本 → 第 1 天",
      mod.first_day_reaching(0, 1, start=dt.date(2026, 11, 10)) == 1,
      f"{mod.first_day_reaching(0, 1, start=dt.date(2026, 11, 10))}")

print("\n== 二十四、兑换凭证口径统一 ==")
for _y in (40, 100, 300, 520):
    _a = mod.yellow_exchange_plan(_y, 12)["total_pulls"]
    _b = mod.yellow_exchange_until_done(_y)["total_pulls"]
    check(f"{_y} 票：攒抽页与兑换凭证页同一口径（{_a} 抽）", _a == _b, f"plan={_a} done={_b}")
check("本月已换到 120 档：本月只领剩下的 20 抽",
      mod.yellow_exchange_plan(258, 1,
                               consumed_this_month=mod.yellow_stage_to_consumed(120))["now_pulls"] == 20)

print("\n== 二十五、可井（刚好 300 抽）==")
_s300 = mod.spark_summary(300)
check("300 抽 = 300 档 1 个 + 赠送 1 个 = 2 个",
      _s300["tier300"] == 1 and _s300["gift"] == 1 and _s300["total_operators"] == 2,
      f"{_s300}")
check("299 抽只能换 200 档 1 个", mod.spark_summary(299)["total_operators"] == 1)
check("301 抽 = 1 兑换 + 1 赠送", mod.spark_summary(301)["total_operators"] == 2)
check("600 抽 = 2 兑换 + 1 赠送", mod.spark_summary(600)["total_operators"] == 3)
check("可井「个」数单调不减",
      all(mod.spark_summary(i)["total_operators"]
          <= mod.spark_summary(i + 1)["total_operators"] for i in range(0, 700, 7)))

print()
print("全部通过 ✅" if ok else "存在失败项 ❌")
sys.exit(0 if ok else 1)

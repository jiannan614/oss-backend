# -*- coding: utf-8 -*-
"""budget_core.py 的自动化自检（验证用，可删除）。

覆盖：
  折算   四种资源的折算率、整数抽与余数
  机制   6★ 概率表（含软保底与硬保底）、理论平均间隔
  账本   装备 / 保底 / 通用 / 高级配额的手算对照、玩家档位差异
  统计   同种子可复现、均值贴近理论、档位只有三档
  视图   四张 KPI、一张折叠明细、一段结论
  边界   垫抽越界、未知档位、抽数超上限

    python selftest.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("budget_core", HERE / "budget_core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["budget_core"] = core
spec.loader.exec_module(core)

ok = True


def check(name, cond, extra=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def main() -> int:
    # ① 资源折算
    check("抽卡货币 1:1", core.convert({"orundum": 6000})["total"] == 6000)
    check("高级货币 ×75", core.convert({"originite": 10})["total"] == 750)
    check("单抽券 = 500", core.convert({"tickets": 1})["total"] == 500)
    check("十连券 = 5000", core.convert({"ten": 1})["total"] == 5000)

    full = core.convert({"originite": 10, "orundum": 6000, "tickets": 3, "ten": 1})
    check("四项合计 13250", full["total"] == 13250)
    check("现有抽数 26.5", full["draws"] == 26.5)
    check("完整 26 抽 / 余 250", full["full_draws"] == 26 and full["remainder"] == 250)

    # ② 概率表与理论间隔
    check("基础 0.8%", core.six_rate_permille(1) == 8)
    check("第 65 抽仍是基础", core.six_rate_permille(65) == 8)
    check("第 66 抽 5.8%", core.six_rate_permille(66) == 58)
    check("第 79 抽 70.8%", core.six_rate_permille(79) == 708)
    check("第 80 抽必出", core.six_rate_permille(80) == 1000)
    gap = core.theoretical_gap()
    check("理论平均间隔 50~60 抽", 50 < gap < 60, f"= {gap:.2f}")

    # ③ 账本口径（手算）
    new = core.PROFILES["new"]["owned"]
    led = core._ledger(100, 0, 0, 2, [2, 0, 0, 0, 0], [0] * 9, [0] * 8, new)
    check("4★ 两份：装备 40 / 通用 0", led["equip"] == 40 and led["universal"] == 0)

    led = core._ledger(100, 0, 0, 8, [8, 0, 0, 0, 0], [0] * 9, [0] * 8, new)
    check("4★ 八份：溢出 2 份 = 10", led["universal"] == 10 and led["equip"] == 160)

    led = core._ledger(100, 0, 1, 0, [0] * 5, [3] + [0] * 8, [0] * 8, new)
    check("5★ 三份：保底 20 / 通用 0",
          led["pity"] == 20 and led["universal"] == 0 and led["equip"] == 200)
    check("保底配额可折抽数", abs(led["pity_draws"] - 20 / core.PITY_PER_DRAW) < 1e-9)

    vet = core.PROFILES["veteran"]["owned"]
    led = core._ledger(100, 0, 1, 0, [0] * 5, [3] + [0] * 8, [0] * 8, vet)
    check("老玩家 5★ 三份：通用 60 / 保底 30",
          led["universal"] == 60 and led["pity"] == 30)

    led = core._ledger(100, 2, 0, 0, [0] * 5, [0] * 9, [2] + [0] * 7, vet)
    check("老玩家 6★ 两份（不含本体）：保底 50 / 高级 0",
          led["pity"] == 50 and led["premium"] == 0)

    # ③a 6★ 保底配额：第 2 份起**每份** 50
    for copies, want in ((1, 0), (2, 50), (4, 150)):
        led = core._ledger(100, copies, 0, 0, [0] * 5, [0] * 9,
                           [copies] + [0] * 7, new)
        check(f"6★ {copies} 份 → 保底 {want}", led["pity"] == want)

    # ③b 每 240 抽赠送的凭据：计入 6★ 总数、也推进 UP 的溢出，但不给其它收益
    led = core._ledger(240, 3, 0, 0, [0] * 5, [0] * 9, [3] + [0] * 7, new, gift=1)
    check("赠送计入 6★ 总数", led["six"] == 4 and led["six_drawn"] == 3)
    check("UP 份数含赠送", led["up_six"] == 4 and led["up_six_drawn"] == 3)
    check("赠送不给装备配额", led["equip"] == 3 * core.EQUIP_QUOTA[core.SIX])
    check("平均间隔只用抽到的 6★", abs(led["avg_gap"] - 240 / 3) < 1e-9)
    # 5 份 + 3 赠品 = 8 份 → 溢出 2 份 × 10 = 20
    led = core._ledger(720, 5, 0, 0, [0] * 5, [0] * 9, [5] + [0] * 7, new, gift=3)
    check("赠送推进满阶：5 + 3 = 8 份 → 高级配额 20", led["premium"] == 20)
    check("赠送不给保底配额（只按抽到的 5 份算）",
          led["pity"] == 4 * core.PITY_QUOTA[core.SIX])

    # ③c 满图鉴氪佬：7 位非 UP 6★ 已满阶，抽到任何一份都直接溢出
    whale = core.PROFILES["whale"]["six_owned"]
    led = core._ledger(100, 6, 0, 0, [0] * 5, [0] * 9, [0, 1, 1, 1, 1, 1, 1, 0],
                       new, six_owned=whale)
    check("氪佬：6 个非 UP 各 1 份 = 60 高级配额", led["premium"] == 60)
    check("氪佬：每个非 UP 都从第 2 份起给 50",
          led["pity"] == 6 * core.PITY_QUOTA[core.SIX])
    led = core._ledger(100, 1, 0, 0, [0] * 5, [0] * 9, [1] + [0] * 7, new, six_owned=whale)
    check("氪佬的 UP 仍从 0 份起算", led["premium"] == 0 and led["pity"] == 0)

    # ④ 单组：同种子可复现
    a = core.simulate_once(600, random.Random(7))
    b = core.simulate_once(600, random.Random(7))
    check("同种子同结果", a == b)
    check("600 抽的量级合理", 0 <= a["six"] <= 30 and 0 <= a["five"] <= 150)
    check("没出 6★ 时间隔为无穷", (a["avg_gap"] == math.inf) == (a["six_drawn"] == 0))

    # ⑤ 一组统计
    data = core.simulate(600, 400, profile="new", seed=2026)
    check("600 抽均值 > 7 个 6★", data["mean"]["six"] > 7.0)
    check("均值贴近理论", abs(data["mean_gap"] - data["theory_gap"]) < 5)
    check("Max ≥ 平均 ≥ Min",
          data["max"]["six"] >= data["mean"]["six"] >= data["min"]["six"])
    check("档位只有三档", [t["label"] for t in data["tiers"]] == ["欧", "正常", "非酋"])

    # ⑤a 统计层的两个 6★ 口径：600 抽固定赠 2 个
    check("600 抽平均赠 2 个凭据", data["mean"]["gift_six"] == 2.0)
    check("6★ 总数 = 抽到的 + 赠送",
          abs(data["mean"]["six"] - data["mean"]["six_drawn"] - 2) < 1e-9)
    check("平均间隔不含赠送",
          abs(data["mean"]["avg_gap"] - 600 / data["mean"]["six_drawn"]) < 1e-6)
    check("档位计数覆盖全部组", sum(t["count"] for t in data["tiers"]) == 400)

    # ⑤a 加急招募（30 抽触发的免费十连）出的货同样进账本
    d30 = core.simulate(30, 4000, seed=13)
    check(f"加急招募的 6★ 计入（{d30['mean']['six']:.2f}）", d30["mean"]["six"] > 0.26)
    check(f"加急招募的 5★ 计入（{d30['mean']['five']:.2f}）", d30["mean"]["five"] > 4.4)
    check("30 抽不足 240，没有赠送凭据", d30["mean"]["gift_six"] == 0)

    # ⑤a2 输出必须是合法 JSON（inf 会被 json.dumps 写成 Infinity，浏览器解析失败）
    check("json_safe 清掉 inf/nan",
          core.json_safe(float("inf")) is None and core.json_safe(float("nan")) is None)
    tiny = core.run({"orundum": 25 * 500}, runs=200, seed=3)
    raw = core._envelope_ok(tiny)
    check("极小抽数的输出不含 Infinity / NaN",
          "Infinity" not in raw and "NaN" not in raw)
    check("极小抽数的信封能被 json.loads 解析", json.loads(raw)["ok"] is True)

    # ⑤b 引擎对账：矩阵版与逐抽版的随机数流不同，逐组不可能相同，只比分布
    if core._matrix_engine() is not None:
        fast = core.simulate(300, 800, seed=1, engine="matrix")
        slow = core.simulate(300, 800, seed=2, engine="loop")
        check("矩阵引擎生效", fast["engine_kind"] == "numpy-matrix")
        check("可回退逐抽版", slow["engine_kind"] == "python-loop")
        for key, tol in (("six", 0.6), ("five", 1.6), ("four", 2.2)):
            gap = abs(fast["mean"][key] - slow["mean"][key])
            check(f"两引擎 {key} 均值一致（差 {gap:.2f} < {tol}）", gap < tol)
        for key in ("equip", "pity", "universal"):
            a, b = fast["mean"][key], slow["mean"][key]
            check(f"两引擎 {key} 均值一致（{a:.0f} vs {b:.0f}）",
                  abs(a - b) <= max(0.02 * max(a, b), 1.0))
        ped_fast = core.simulate(120, 600, start_pity=70, seed=5, engine="matrix")
        ped_slow = core.simulate(120, 600, start_pity=70, seed=6, engine="loop")
        gap = abs(ped_fast["mean"]["six"] - ped_slow["mean"]["six"])
        check(f"垫 70 抽起跑两引擎一致（差 {gap:.2f}）", gap < 0.6)
        one = core.simulate(1, 4000, seed=7, engine="matrix")
        check("矩阵引擎单抽 6★ 率 ≈ 0.8%", abs(one["mean"]["six"] - 0.008) < 0.006)
        check("矩阵引擎 0 抽不出 6★",
              core.simulate(0, 50, seed=8, engine="matrix")["mean"]["six"] == 0)

    # ⑥ 视图
    view = core.run({"orundum": 300000}, runs=300, seed=11)
    table = view["tables"][0]
    check("kpis 四项", len(view["kpis"]) == 4)
    check("只有一张表", len(view["tables"]) == 1)
    check("四列：指标 / Max / 平均 / Min", table["columns"] == ["指标", "Max", "平均", "Min"])
    check("主表十一行（含「其中 UP」）", len(table["rows"]) == 11)
    check("明细默认折叠", table["collapsed"] is True)
    check("保障行带可折抽数", any("可折" in r[1] for r in table["rows"] if r[0] == "保底配额"))
    check("环形图有数据", view["donut"]["total"] == 300 and bool(view["donut"]["items"]))
    check("结论是一段话", len(view["notes"]) == 1)
    # 数字格式化：KPI 里不该出现 97.48941271905014 这种长尾
    tails = [v for v in (k["value"] for k in view["kpis"])
             if "." in v and len(v.split(".")[1].rstrip("个抽 ")) > 1]
    check("KPI 小数不超过一位", not tails, str(tails))
    check("结论含均值 / 保底 / 免责",
          "理想情况" in view["notes"][0] and "保底" in view["notes"][0]
          and "不代表真实" in view["notes"][0])

    # ⑦ 边界
    for kwargs, name in (({"pity": 80}, "垫抽 80"),
                         ({"profile": "x"}, "未知档位"),
                         ({"pity": -1}, "垫抽 -1")):
        try:
            core.run({"orundum": 500}, **kwargs)
            check(f"{name} 应该报错", False)
        except ValueError:
            check(f"{name} 报错", True)

    try:
        core.run({"orundum": 500 * (core.MAX_DRAWS + 100)}, runs=10)
        check("抽数超上限应该报错", False)
    except ValueError:
        check("抽数超上限报错", True)

    print()
    print("结果：" + ("全部通过" if ok else "有失败项"))
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)

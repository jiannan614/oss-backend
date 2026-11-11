#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 抽数规划计算服务的 JSON 接口

把 planner_core.py（纯标准库）包一层 HTTP，供任意前端调用。两种用法：

* GET|POST /api/v1/gacha/<mode> —— 直接调用，同步返回「视图模型」
  （kpis / tables / cards / donut / notes），前端照着排版即可；
* POST /internal/jobs/<job_id> —— 带令牌与 HMAC 签名的任务式调用，
  适合把计算放到独立机器上（协议见 README「服务间调用」）。

mode：
    count   资源折算（货币 / 券 / 兑换凭证 → 总抽数）
    calc    攒抽预测（到目标日能攒多少、还差多少天）
    rate    爆率与保底曲线（N 抽内至少出 k 个最高稀有度的概率）
    yellow  兑换凭证的阶梯兑换（本月能换多少、要几个月）
    event   活动窗口与赠送预估

环境变量：
    WORKER_TOKEN    共享令牌；留空即不校验（仅限本机开发）
    WORKER_SECRET   HMAC 签名密钥，任务式调用需要

    pip install flask waitress
    python api.py --port 8502            # 开发
    python api.py --port 8502 --prod     # waitress
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request

import planner_core as gc

app = Flask(__name__)
app.json.ensure_ascii = False

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "dev-worker-token")
WORKER_SECRET = os.getenv("WORKER_SECRET") or os.getenv("SECRET_KEY") or "shuchi-dev-key-change-me"

CFG = gc.DEFAULT_CONFIG
E = gc.ORUNDUM_PER_PULL
MODES = ("count", "calc", "yellow")

#: 同步模式没有真正的任务队列，这里只留最近若干条结果，供 GET 回放
_RESULTS: "OrderedDict[str, dict]" = OrderedDict()
_RESULT_LIMIT = 200


# --------------------------------------------------------------------------- #
# 统一信封（与调用方的 接口说明 一致）
# --------------------------------------------------------------------------- #


def ok(data: Any, status: int = 200, **meta):
    body: dict[str, Any] = {"ok": True, "data": data}
    if meta:
        body["meta"] = meta
    return jsonify(body), status


def fail(code: str, message: str, status: int = 400, details: Any = None):
    body: dict[str, Any] = {"ok": False, "error": code, "message": message}
    if details:
        body["details"] = details
    return jsonify(body), status


def canonical_params(params: dict[str, Any]) -> str:
    """必须与调用方 ``dispatch.canonical_params`` 完全一致。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign(*parts: Any) -> str:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(WORKER_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _token_ok() -> bool:
    if not WORKER_TOKEN:  # 留空 = 不校验（只应该出现在本地调试）
        return True
    got = request.headers.get("X-API-Token", "")
    return hmac.compare_digest(got, WORKER_TOKEN)


def internal_only(view):
    """门禁：只认共享令牌，不认 Cookie。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _token_ok():
            return fail("bad_token", "缺少或错误的 X-API-Token", 401)
        return view(*args, **kwargs)

    return wrapped


def _status_signature_ok(job_id: str) -> bool:
    expected = sign(f"status|{job_id}")
    got = request.headers.get("X-API-Signature", "")
    return hmac.compare_digest(got, expected)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 计算：把 planner_core 的返回值整理成「结论卡片 + 表格 + 备注」
#
# 变量与 app.py（Streamlit 界面）的侧边栏 / 页签**一一对应**，同一个键名走
# 同一套默认值与口径，所以两边算出来的数字必然一致。
# --------------------------------------------------------------------------- #


def pct(value: float, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%"


def safe_int(params: dict[str, Any], key: str, default: int = 0,
             low: int = 0, high: int | None = None) -> int:
    raw = params.get(key, default)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = default
    value = max(low, value)
    return min(high, value) if high is not None else value


def safe_float(params: dict[str, Any], key: str, default: float = 0.0,
               low: float = 0.0, high: float | None = None) -> float:
    raw = params.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    value = max(low, value)
    return min(high, value) if high is not None else value


def _bool(params: dict[str, Any], key: str, default: bool = False) -> bool:
    raw = params.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "checked"}


def _base(p: dict[str, Any]) -> dict[str, Any]:
    """与 app.py 的 base_params() 同口径：键名一致，默认一致。

    ``new_player`` 不再单独收参数，而是由玩家档位推出来：
    新玩家 / 中期玩家 = 新玩家口径（浮动高级货币更多），资深玩家 = 老玩家口径。
    """
    profile = _profile(p)
    return {
        "orundum": safe_int(p, "orundum"),
        "originite": safe_int(p, "originite"),
        "tickets": safe_int(p, "tickets"),
        "ten": safe_int(p, "ten"),
        "yellow": safe_int(p, "yellow"),
        "yellow_stage": safe_int(p, "yellow_stage", 10, 0, 258),
        "keep": safe_int(p, "keep"),
        "no_originite": not _bool(p, "use_originite", True),
        "pity": safe_int(p, "pity", 0, 0, 200),
        "craft_jade": _bool(p, "craft_jade"),
        "monthly_card": _bool(p, "monthly_card"),
        "new_player": not gc.profile_is_veteran(profile),
        "veteran": gc.profile_is_veteran(profile),
        "no_spark": _bool(p, "no_spark"),
        "extra": safe_int(p, "extra"),
        "profile": profile,
    }


def _profile(p: dict[str, Any]) -> str:
    """玩家档位：新 / 中 / 老（只影响副产物里兑换凭证的估算）。"""
    raw = str(p.get("profile") or p.get("player_profile") or "").strip().lower()
    alias = {"new": "new", "newbie": "new", "新": "new", "新玩家": "new", "新玩家": "new",
             "mid": "mid", "middle": "mid", "中": "mid", "中玩家": "mid", "中期玩家": "mid",
             "veteran": "veteran", "old": "veteran", "老": "veteran",
             "老玩家": "veteran", "资深玩家": "veteran"}
    return alias.get(raw, gc.DEFAULT_PLAYER_PROFILE)


def _snapshot(p: dict[str, Any]) -> dict[str, Any]:
    """现有资源折算 + 本月兑换凭证可换（资源折算 / 攒抽预测共用同一口径）。"""
    b = _base(p)
    r = gc.pulls_from_resources(
        orundum=b["orundum"], originite=b["originite"], tickets=b["tickets"],
        ten_tickets=b["ten"], use_originite=not b["no_originite"],
        originite_keep=b["keep"])
    state = gc.yellow_month_state(
        b["yellow"], consumed_this_month=gc.yellow_stage_to_consumed(b["yellow_stage"]))
    return {
        "b": b, "r": r, "state": state,
        "yellow_pulls": state["claim_pulls"],
        "current": r["total_pulls"] + state["claim_pulls"],
    }


def _income(p: dict[str, Any]) -> dict[str, Any]:
    """收入口径：自造货币必须同时进日收入与月收入，否则两个数自相矛盾。"""
    b = _base(p)
    income = gc.income_breakdown(monthly_card=b["monthly_card"])
    craft_per_day = gc.crafted_orundum_per_day(b["monthly_card"]) if b["craft_jade"] else 0
    daily = income["daily_total"] + craft_per_day
    return {
        "income": income,
        "craft_per_day": craft_per_day,
        "daily": daily,
        "monthly_od": income["monthly_od"] + craft_per_day * 30,
    }


def _spark_table(pulls: int) -> dict[str, Any]:
    """可井：>300 先吃 300 档（另赠送 1 个），否则依次 200 / 75，最后报余数。

    表格只留 4 行（档位 / 可换 / 兑换点数），赠送那 1 个折进合计并用底部备注说明，
    不再单起一行——原来那行把「2024-05-01 起」写进「兑换点数消耗」列，纯属碍眼。
    """
    s = gc.spark_summary(pulls)
    rows = [
        ["300 兑换点数 · 当期限定 6★", f"{s['tier300']} 个", f"{s['tier300'] * 300}"],
        ["200 兑换点数 · 解限旧限定", f"{s['tier200']} 个", f"{s['tier200'] * 200}"],
        ["75 兑换点数 · 当期 UP 5★", f"{s['tier75']} 个", f"{s['tier75'] * 75}"],
        ["合计", f"{s['total_operators']} 个", f"余 {s['left']} 兑换点数"],
    ]
    note = f"满 {gc.CONTRACT_COST_PER_SPARK} 抽会额外赠送 1 个当期限定"
    if s["gift"]:
        note += f"（已计入合计的 {s['total_operators']} 个）"
    else:
        note += "（本窗口不够，暂未计入）"
    return {
        "title": "可井",
        "columns": ["档位", "可换", "兑换点数消耗"],
        "rows": rows,
        "note": note,
        "layout": "half",
    }


# --------------------------------------------------------------------------- #
# ① 现有资源汇总
# --------------------------------------------------------------------------- #
def build_count(p: dict[str, Any]) -> dict[str, Any]:
    """总计抽数 + 环表（高级货币/抽卡货币/单/十/兑换凭证）+ 产物预算。

    兑换凭证只算**当前这一轮**（最多 38 抽），超出部分不折进合计 —— 那些要等次月刷新。
    """
    b = _base(p)
    s = gc.resource_summary(
        orundum=b["orundum"], originite=b["originite"], tickets=b["tickets"],
        ten_tickets=b["ten"], yellow=b["yellow"], yellow_stage=b["yellow_stage"],
        use_originite=not b["no_originite"], originite_keep=b["keep"],
        profile=b["profile"])
    prod = s["products"]
    pulls_of = {item["key"]: item["pulls"] for item in s["items"]}

    kpis = [
        {"label": "总计", "value": f"{s['total_pulls']} 抽"},
        {"label": "六星", "value": f"{prod['count_6']:.2f} 个", "hint": "平均预算"},
        {"label": "五星", "value": f"{prod['count_5']:.2f} 个"},
        {"label": "兑换凭证", "value": f"{prod['yellow_full_total']:.0f} 张"},
        {"label": "次级凭证", "value": f"{prod['green']:.0f} 张"},
    ]

    donut = {
        "total": s["total_pulls"],
        "unit": "抽",
        "caption": "现有资源",
        "items": [
            {"label": item["label"], "pulls": item["pulls"], "value": item["value"],
             "percent": round(item["percent"], 1),
             "color": item["color"], "shade": item["shade"]}
            for item in s["items"] if item["pulls"] > 0
        ],
    }

    rows = [
        ["抽卡货币", f"{s['orundum']:,} 货币（余 {s['leftover_orundum']} 货币）",
         f"{pulls_of.get('orundum', 0)} 抽"],
        ["高级货币", f"{s['originite']} 颗（折算 {s['originite_used']} 颗）",
         f"{pulls_of.get('originite', 0)} 抽"],
        ["单抽凭证", f"{s['tickets']} 张", f"{pulls_of.get('tickets', 0)} 抽"],
        ["十连凭证", f"{s['ten_tickets']} 张", f"{pulls_of.get('ten', 0)} 抽"],
        ["兑换凭证（本月这一轮）", f"{s['yellow']} 张 → {s['yellow_pulls']} 抽",
         f"余 {s['yellow_left']} 张"],
        ["合计", "—", f"{s['total_pulls']} 抽"],
    ]

    notes: list[str] = []
    if s["yellow_claimed"]:
        detail = " + ".join(f"{c}票→{g}抽" for c, g in s["yellow_claimed"])
        notes.append(f"兑换凭证本月可领：{detail}。只算这一轮（上限 {gc.YELLOW_FULL_PULLS} 抽 / "
                     f"{gc.YELLOW_MONTHLY_LIMIT} 票），超出的 {s['yellow_left']} 张要等次月刷新。")
    elif s["yellow"]:
        notes.append(f"兑换凭证本月已换到 {s['yellow_stage']} 档，这一轮没有可领的档位了。")
    if b["pity"]:
        notes.append(f"当前垫了 {b['pity']} 抽，距 {CFG.hard_pity} 抽硬保底还剩 "
                     f"{max(0, CFG.hard_pity - b['pity'])} 抽。")
    notes.append(f"产物按【{prod['profile_label']}】口径估算（{prod['profile']}），"
                 f"基于平均预算，不代表真实情况，仅供参考。")

    return {
        "kpis": kpis,
        "donut": donut,
        "tables": [{"title": "资源明细", "columns": ["资源", "数量", "折合"], "rows": rows}],
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# ② 攒抽预测
# --------------------------------------------------------------------------- #
def _days_with_yellow(current: int, target: int, extra_orundum: int,
                      yellow: int, yellow_base: int, today: "_dt.date",
                      monthly_card: bool = False, veteran: bool = True,
                      craft_jade: bool = False,
                      consumed_this_month: int = 0) -> tuple[dict, dict]:
    """目标抽数算法：逐日累加到「够缺口」为止（引擎在 planner_core.first_day_reaching）。

    以前这里走 `period_bonus` 的二分，而「额外进账」是按日期跳变的，
    二分会在窗口边界上给出偏晚的日期。现在和「目标日」共用同一台逐日账本。
    """
    days = gc.first_day_reaching(current, target, extra_orundum=extra_orundum,
                                 yellow=yellow, yellow_base=yellow_base,
                                 veteran=veteran, monthly_card=monthly_card,
                                 craft_jade=craft_jade,
                                 consumed_this_month=consumed_this_month,
                                 start=today)
    if days is None:
        return {"days": None, "need_orundum": 0}, {}
    if days == 0:
        return {"days": 0, "need_orundum": 0}, {}
    bonus = gc.period_bonus(today, today + _dt.timedelta(days=days),
                            yellow=yellow, yellow_base=yellow_base,
                            veteran=veteran, monthly_card=monthly_card,
                            craft_jade=craft_jade,
                            consumed_this_month=consumed_this_month)
    return {"days": days,
            "need_orundum": max(0, (target - current) * E - extra_orundum)}, bonus


def _resource_stats(b: dict[str, Any], bonus: dict | None) -> list[dict[str, str]]:
    """「达成时手上」四项资源：高级货币 / 抽卡货币 / 单抽凭证 / 十连凭证。

    数值全部来自逐日账本。`bonus` 为 None 表示「就是现在手上的」。
    返回结构化数据，前端可以直接铺成四格卡片（而不是一张宽表）。
    """
    led = (bonus or {}).get("ledger") or {"originite": 0.0, "orundum": 0.0, "tickets": 0.0}
    kept = max(0, b["originite"] - b["keep"]) if b["no_originite"] else 0
    return [
        {"label": "高级货币", "value": f"{led['originite'] + kept:.0f}", "unit": "颗"},
        {"label": "抽卡货币", "value": f"{b['orundum'] + led['orundum']:,.0f}", "unit": "货币"},
        {"label": "单抽凭证", "value": f"{b['tickets'] + led['tickets']:.0f}", "unit": "张"},
        {"label": "十连凭证", "value": f"{b['ten']}", "unit": "张"},
    ]


def _target_card(target: int, stats: list[dict[str, str]], foot: str) -> dict[str, Any]:
    """一个目标一张卡：卡头「100 抽 · 详细」，卡内固定四项资源。"""
    return {"title": f"{target} 抽", "note": "详细", "foot": foot, "stats": stats}


def build_rate(p: dict[str, Any]) -> dict[str, Any]:
    today = _dt.date.today()
    snap = _snapshot(p)
    b = snap["b"]
    current, yellow = snap["current"], b["yellow"]
    consumed = gc.yellow_stage_to_consumed(b["yellow_stage"])
    y_base = snap["yellow_pulls"]            # 本月这一轮，已经算进 current 里
    inc = _income(p)

    raw_target = str(p.get("target") or "100 200 300").replace(",", " ").split()
    targets = [int(x) for x in raw_target if x.strip().lstrip("-").isdigit()]
    if not targets:
        targets = [100, 200, 300]
    extra = safe_int(p, "extra")

    kpis = [
        {"label": "当前现有抽数", "value": f"{current} 抽",
         "hint": f"含本月兑换凭证 {y_base} 抽"},
        {"label": "基础日收", "value": f"{inc['daily']:.0f} 货币/日"},
        {"label": "基础月收", "value": f"{inc['monthly_od'] / E:.1f} 抽/月",
         "hint": f"{inc['monthly_od']:,.0f} 货币/月"},
        {"label": "玩家口径", "value": "老玩家" if b["veteran"] else "新玩家",
         "hint": gc.PLAYER_PROFILE_MAP[b["profile"]]["label"]},
    ]

    notes: list[str] = []
    notes.append(
        f"玩家档位【{gc.PLAYER_PROFILE_MAP[b['profile']]['label']}】→ 按"
        f"【{'老玩家' if b['veteran'] else '新玩家'}】口径算每月 15 号复刻"
        + ("（复刻已打过，拿 2000 货币 + 3 抽）" if b["veteran"]
           else "（复刻没打过，拿 36 高级货币 + 3 抽）")
        + "；与「自定义日期/活动算法」同一套口径。")
    if b["craft_jade"]:
        notes.append(f"已计入自造货币 {inc['craft_per_day']} 货币/天"
                     f"（≈ {inc['craft_per_day'] * 30 / E:.1f} 抽/月）。自造货币会占用理智，不刷就别勾。")

    rows = []
    cards: list[dict[str, Any]] = []
    for target in targets:
        d, bonus = _days_with_yellow(current, target, extra, yellow, y_base, today,
                                     monthly_card=b["monthly_card"],
                                     veteran=b["veteran"], craft_jade=b["craft_jade"],
                                     consumed_this_month=consumed)
        gap = max(0, target - current)
        if d["days"] is None:
            rows.append([f"{target} 抽", "无法达成", "—", f"{gap} 抽"])
            cards.append(_target_card(target, _resource_stats(b, None),
                                      "十年内攒不到，先调低目标或换个目标日"))
            continue
        if d["days"] == 0:
            rows.append([f"{target} 抽", "已达成", "—", "0 抽"])
            cards.append(_target_card(target, _resource_stats(b, None),
                                      "现在就够，不用再攒"))
            continue
        date = (today + _dt.timedelta(days=d["days"])).strftime("%Y-%m-%d")
        rows.append([f"{target} 抽", f"{d['days']} 天", date, f"{gap} 抽"])
        cards.append(_target_card(target, _resource_stats(b, bonus),
                                  f"{d['days']} 天后 · {date} 届时手上"))
        have = current + int(round(bonus["pulls"]))
        notes.append(f"目标 {target} 抽：{d['days']} 天后届时约 {have} 抽"
                     f"（含期间账本进账 {bonus['pulls']:.0f} 抽）。")
        if bonus.get("card_pulls"):
            notes.append(f"目标 {target} 抽：期间月卡 {bonus['card']['months']} 张 = "
                         f"{bonus['card_cost']} 元（另得高级货币 {bonus['card_originite']} 颗）。")

    tables = [{
        "title": "抽数目标 · 攒到各目标还要多久",
        "columns": ["目标", "还需", "预计达成", "缺口"],
        "rows": rows,
    }]

    horizon = gc.month_refreshes_between(today, today + _dt.timedelta(days=400))
    credit = gc.yellow_exchange_plan(yellow, horizon,
                                     consumed_this_month=consumed)["total_pulls"] - y_base
    if credit:
        notes.append(f"未来一年内兑换凭证还能再换 {credit} 抽（每月上限 "
                     f"{gc.YELLOW_FULL_PULLS} 抽 = {gc.YELLOW_MONTHLY_LIMIT} 票）。")
    notes.append("「还需」是从今天起一天一天累加账本得出的第几天，"
                 "不是「货币/天 × 天数」的估算；每月 15 号复刻、大版本节点都会整笔跳进来。")

    # cards：一个目标一张卡（高级货币 / 抽卡货币 / 单抽 / 十连），前端直接铺成四格，
    # 不再用一张宽表把目标挨在一起。
    return {"kpis": kpis, "tables": tables, "notes": notes, "cards": cards}


# --------------------------------------------------------------------------- #
# ③ 目标日 / 活动预估
# --------------------------------------------------------------------------- #
def build_event(p: dict[str, Any]) -> dict[str, Any]:
    b = _base(p)
    event_choice = str(p.get("event") or "")
    # 没明确选活动（空 / 「最近的一个」）时，目标日只是兜底信息：
    # 让前端把这几张表折叠起来，先看「目标抽数」那一段。
    is_default_event = event_choice in ("", "最近的一个", "recent")
    if event_choice == "自定义日期":
        event_arg = str(p.get("event_date") or "").strip()
        if not event_arg:
            event_arg = ""  # 留空则回落到「最近的一个」
    else:
        event_arg = event_choice

    namespace = argparse.Namespace(
        event=event_arg,
        orundum=b["orundum"], originite=b["originite"], tickets=b["tickets"], ten=b["ten"],
        keep=b["keep"], yellow=b["yellow"], yellow_stage=b["yellow_stage"],
        pity=b["pity"], monthly_card=b["monthly_card"], craft_jade=b["craft_jade"],
        new_player=b["new_player"], no_spark=b["no_spark"],
        no_originite=b["no_originite"], extra=safe_int(p, "extra"),
        profile=b["profile"],
        signin=True, green=True, maintenance=True,
    )
    data = gc.event_calc(namespace)
    ev = data["event"]
    analysis = data["analysis"]

    kpis = [
        {"label": "本期可抽", "value": f"{data['pool_pulls']} 抽",
         "hint": f"自由 {data['free_int']} + 限时 {data['pool_extra']}"},
        {"label": "自由资源", "value": f"{data['free_int']} 抽",
         "hint": "不会过期，任意卡池"},
        {"label": "限时资源", "value": f"{data['pool_extra']} 抽",
         "hint": f"窗口内共发 {data['timed_total']} 张，过期作废"},
        {"label": "至少出 1 个限定", "value": pct(analysis["p_limited"]),
         "hint": "井保证" if analysis["spark_by_gift"] else ""},
    ]
    if data["card_months"]:
        kpis.append({
            "label": "月卡要买",
            "value": f"{data['card_months']} 张 · {data['card_cost']} 元",
            "hint": f"立得 {data['card_originite']} 颗高级货币",
        })

    if data["can_spark"]:
        verdict = (f"够吃 300 井（多出 {data['pool_pulls'] - gc.CONTRACT_COST_PER_SPARK} 抽）："
                   f"直接下池，抽满 300 换井 + 领赠送，当期限定必得。")
    elif data["can_spark_upper"]:
        verdict = (f"卡在井线上：保底差 {-data['to_spark']} 抽，算上浮动误差大概率够 300 井。"
                   f"按「能满井」准备，提前出了就收手。")
    else:
        verdict = (f"距 300 井还差 {-data['to_spark']} 抽；按现有抽数硬抽拿下当期限定的概率 "
                   f"{pct(analysis['p_limited'])}。")

    if ev.get("is_target"):
        target_line = (f"目标日 {ev['end_date'].strftime('%Y-%m-%d')}"
                       f"（自定义目标日）")
        when_rows = [["目标日", ev["end_date"].strftime("%Y-%m-%d")]]
    else:
        target_line = (f"目标日 {ev.get('name') or '最近档期'}"
                       f" · 活动最后一天 {ev['end_date'].strftime('%Y-%m-%d')}")
        when_rows = [
            ["活动开始", ev["date"].strftime("%Y-%m-%d")],
            ["活动结束", ev["end_date"].strftime("%Y-%m-%d")],
        ]

    # 时间线 + 合计：两列，和 app.py 的卡片一一对应
    detail_rows = when_rows + [
        ["兑换凭证刷新", f"{data['yellow_plan']['months_available']} 次"],
        ["逐日账本", f"{data['days']} 天"],
        ["共计", f"{data['pool_pulls']} 抽"
                 f"（自由 {data['free_int']} + 限时 {data['pool_extra']}）"],
    ]

    # 进账分类：这张表加总 = 自由资源 + 限时券 = 共计（以前前端没有这张表）
    cat_rows: list[list[str]] = []
    for row in data["breakdown"]:
        cat_rows.append([row["label"], row["detail"], f"{row['pulls']:.1f} 抽"])
    cat_rows.append(["自由资源", "不会过期，任意卡池", f"{data['free_int']} 抽"])
    if data["timed_total"] > data["pool_extra"]:
        cat_rows.append(["限时券（窗口共发）", "越界的那部分会过期作废",
                         f"{data['timed_total']} 张"])
    cat_rows.append(["合计", "自由 + 本期可用限时券", f"{data['pool_pulls']} 抽"])

    tables = [
        {
            "title": target_line,
            "columns": ["项目", "数值"],
            "rows": detail_rows,
            "layout": "half",
            "collapsed": is_default_event,
        },
        {
            "title": "进账分类（加总 = 共计）",
            "columns": ["来源", "明细", "折合"],
            "rows": cat_rows,
            "collapsed": is_default_event,
        },
        dict(_spark_table(data["pool_pulls"]), collapsed=is_default_event),
    ]

    notes = []
    if ev.get("clamped"):
        notes.append(f"目标日 {ev['end_date_raw'].strftime('%Y-%m-%d')} 早于今天，按今天统计。")
    notes += [
        verdict,
        f"抽卡口径：自由资源 {data['free_int']} 抽"
        + (f" + 目标日所在档期的限时券 {data['pool_extra']} 抽" if data["pool_extra"]
           else "（目标日不在大版本窗口内，限时券不计入概率）")
        + f" = {data['pool_pulls']} 抽。",
        "每月 15 号按一次复刻算：新玩家 36 高级货币 + 3 抽，老玩家 2000 货币 + 3 抽。",
        "大版本（正月初一 / 5.1 / 8.1 / 11.1 各 14 天）：第 1 天 600 货币 + 10 高级货币 + 3 抽 "
        "+ 11 张限时券 + 5 高级货币停机补偿，第 8 天 16 高级货币，第 2~14 天每天 600 货币 + 1 张券。",
        "概率部分是精确计算，「能攒多少」按逐日账本估算，只当决策参考。",
    ]
    return {"kpis": kpis, "tables": tables, "notes": notes}


# --------------------------------------------------------------------------- #
# ④ 兑换凭证折算
# --------------------------------------------------------------------------- #
def build_yellow(p: dict[str, Any]) -> dict[str, Any]:
    b = _base(p)
    yellow, stage = b["yellow"], b["yellow_stage"]
    consumed = gc.yellow_stage_to_consumed(stage)
    state = gc.yellow_month_state(yellow, consumed_this_month=consumed)
    plan = gc.yellow_exchange_until_done(yellow, consumed_this_month=consumed)

    kpis = [
        {"label": "本月可换", "value": f"{state['claim_pulls']} 抽"},
        {"label": "换完可得", "value": f"{plan['total_pulls']} 抽"},
        {"label": "需要几个月", "value": f"{plan['months_needed']} 个月"},
        {"label": "余多少", "value": f"{plan['leftover']} 张"},
    ]

    rows = []
    for batch in plan["batches"]:
        label = "本月" if batch["is_current_month"] else f"第 {batch['month_index'] + 1} 个月"
        rows.append([
            label,
            f"{batch['pulls']} 抽",
            f"{batch['spent']} 票",
            " + ".join(batch["stages"]) if batch["stages"] else "无可领档位",
        ])
    tables = [{
        "title": "逐月兑换明细",
        "columns": ["月份", "可得", "消耗", "档位"],
        "rows": rows,
    }]
    notes = [
        f"阶梯 10/18/40/70/120 票 = 1/2/5/10/20 抽，合计 {gc.YELLOW_FULL_COST} 票 = "
        f"{gc.YELLOW_FULL_PULLS} 抽；每月 1 日 4:00 刷新。本月已换到 {stage} 档"
        f"（已消耗 {consumed} 票）。",
        (f"余 {plan['leftover']} 票不足最小档位，换不动了。" if plan["leftover"] else "刚好换完。"),
        gc.YELLOW_VALUE_NOTE,
    ]
    if not yellow:
        notes.insert(0, "兑换凭证填 0 就只能换到 0 抽——先去填个数量（例如 637）。")
    return {"kpis": kpis, "tables": tables, "notes": notes}


def build_calc(p: dict[str, Any]) -> dict[str, Any]:
    """攒抽计算器（前端两个算法之一）：把「目标抽数」和「目标日期/活动」合成一次返回。

    两段都走 `first_day_reaching` / `period_bonus` → 同一台 `daily_ledger`，
    并且用同一个玩家档位推出来的新老玩家口径，所以「还要多少天」和「那天有多少抽」
    不会互相打架。

    以前这里只返回 rate 的 kpis，活动那四个数（本期可抽 / 自由 / 限时 / 出限定概率）
    直接丢了 —— 前端看到的「目标日」区块没有自己的结论行。现在两边都带上。
    """
    rate = build_rate(p)
    event = build_event(p)
    seen = {k["label"] for k in rate["kpis"]}
    kpis = list(rate["kpis"]) + [k for k in event["kpis"] if k["label"] not in seen]
    notes = rate["notes"] + event["notes"]
    return {
        "kpis": kpis,
        "tables": rate["tables"] + event["tables"],
        "cards": rate.get("cards", []),
        "notes": notes,
    }


BUILDERS = {
    "count": build_count,
    "calc": build_calc,
    "rate": build_rate,
    "event": build_event,
    "yellow": build_yellow,
}


# 单机只有一份算力：进程内一把互斥锁，同一时刻只让一个请求真的去算。
# 为什么需要它：调用方的 waitress 是 8 线程，几个长窗口（目标抽数要一天一天
# 扫 3650 天）叠在一起会把 CPU 抢空，最后每个请求都很慢甚至超时。
# 排队比一起变慢更好：后来的请求直接被告知「正在算」，前端隔一两秒重试即可。
_COMPUTE_LOCK = threading.Lock()
BUSY_MESSAGE = "算力正在推演中，请稍后"


class ComputeBusy(RuntimeError):
    """前一个请求还没算完，这一条先不接。"""


def run_mode(mode: str, params: dict[str, Any], *, wait: bool = False) -> dict[str, Any]:
    """跑一次算法。

    ``wait=False``（默认）时，抢不到锁立刻抛 :class:`ComputeBusy`，
    由路由层翻译成 503 + 「算力正在推演中，请稍后」。
    """
    if mode not in BUILDERS:
        raise ValueError(f"不认识的算法：{mode}（可选 {' / '.join(MODES)}）")
    if not _COMPUTE_LOCK.acquire(blocking=wait):
        raise ComputeBusy(BUSY_MESSAGE)
    try:
        started = time.time()
        view = BUILDERS[mode](params)
        view["mode"] = mode
        view["params"] = params
        view["elapsed_ms"] = int((time.time() - started) * 1000)
        view["engine"] = f"planner_core {gc.VERSION}"
        return view
    finally:
        _COMPUTE_LOCK.release()


# 路由：握手协议
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health():
    """探活：调用方的 /api/backends 会打这里。"""
    return ok({
        "service": "planner-api",
        "engine": f"planner_core {gc.VERSION}",
        "modes": list(MODES),
        "cached": len(_RESULTS),
        "busy": _COMPUTE_LOCK.locked(),
    })


@app.post("/internal/jobs/<job_id>")
@internal_only
def create_job(job_id: str):
    """握手第三跳：接一条任务，同步算完直接把结果放回 data.result。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return fail("bad_request", "body 必须是 JSON 对象")

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return fail("bad_request", "params 必须是对象")

    # 验签：证明这条任务确实来自调用方，且参数没被改过
    expected = sign(
        job_id,
        payload.get("tool", ""),
        payload.get("user_id", 0),
        payload.get("created_at", 0),
        canonical_params(params),
    )
    got = request.headers.get("X-API-Signature", "")
    if not hmac.compare_digest(got, expected):
        return fail("bad_signature", "签名对不上：参数可能被改过，或者 WORKER_SECRET 不一致", 403)

    mode = str(params.get("mode") or "count")
    try:
        view = run_mode(mode, params)
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 1})
    except ValueError as exc:
        return fail("invalid_params", str(exc), 422)

    data = {
        "job_id": job_id,
        "status": "succeeded",       # 同步计算：进来就算完了
        "adapter": "gacha",
        "adapter_job_id": job_id,
        "progress": {"done": 1, "total": 1},
        "logs": [],
        "result": view,
    }
    _remember(job_id, data)
    return ok(data, 200, adapter="gacha", mode="sync")


@app.get("/internal/jobs/<job_id>")
@internal_only
def get_job(job_id: str):
    """查状态。同步模式没有队列，回放上次结果即可。"""
    if not _status_signature_ok(job_id):
        return fail("bad_signature", "查状态的签名对不上", 403)
    cached = _RESULTS.get(job_id)
    if cached is None:
        return fail("not_found", "这个任务不在缓存里（同步模式只保留最近 200 条）", 404)
    return ok(cached)


@app.post("/internal/jobs/<job_id>/cancel")
@internal_only
def cancel_job(job_id: str):
    """计算是毫秒级的，等走到这里通常已经结束了。"""
    if not _status_signature_ok(job_id):
        return fail("bad_signature", "签名对不上", 403)
    return fail("too_late", "同步计算没有可取消的中间态：结果已经算完了", 409)


def _remember(job_id: str, data: dict[str, Any]) -> None:
    _RESULTS[job_id] = data
    _RESULTS.move_to_end(job_id)
    while len(_RESULTS) > _RESULT_LIMIT:
        _RESULTS.popitem(last=False)


# --------------------------------------------------------------------------- #
# 路由：直接调用（curl / 未来别的前端）
# --------------------------------------------------------------------------- #


@app.get("/api/v1/gacha/<mode>")
@internal_only
def direct_get(mode: str):
    try:
        return ok(run_mode(mode, dict(request.args)))
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 1})
    except ValueError as exc:
        return fail("invalid_params", str(exc), 422)


@app.post("/api/v1/gacha/<mode>")
@internal_only
def direct_post(mode: str):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return fail("bad_request", "body 必须是 JSON 对象")
    try:
        return ok(run_mode(mode, payload))
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 1})
    except ValueError as exc:
        return fail("invalid_params", str(exc), 422)


@app.errorhandler(404)
def not_found(_exc):
    return fail("not_found", "接口不存在", 404)


@app.errorhandler(405)
def bad_method(_exc):
    return fail("method_not_allowed", "方法不对", 405)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api.py", description="planner 计算内核 JSON 接口")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（只对本地开放）")
    parser.add_argument("--port", type=int, default=8502, help="监听端口，默认 8502")
    parser.add_argument("--prod", action="store_true", help="用 waitress（需 pip install waitress）")
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许监听 0.0.0.0 / 内网地址（需要同时设 GACHA_ALLOW_REMOTE=1）。"
                             "默认禁止：计算后端只跑本地，外网一律经 Nginx 反代进来")
    args = parser.parse_args(argv)

    # 结构约束：计算内核**永远只跑在本地回环上**，公网入口交给 Nginx。
    # 以前有人 `--host 0.0.0.0` 起服务，等于把 /api/v1/gacha 直接挂到公网；
    # 现在除非显式加 --allow-remote，否则非回环地址一律拒绝启动。
    loopback = {"127.0.0.1", "localhost", "::1"}
    # 双钥匙：既要 --allow-remote，又要环境变量 GACHA_ALLOW_REMOTE=1。
    # 只加一个参数就放开监听太容易了（复制粘贴一份启动命令就出事），
    # 两道闸门能挡住"手滑把计算后端挂到公网"。
    if args.host not in loopback:
        env_ok = os.getenv("GACHA_ALLOW_REMOTE") == "1"
        if not (args.allow_remote and env_ok):
            print(f"[x] 拒绝监听 {args.host}：计算后端只允许本地回环地址。", flush=True)
            print("    前端一律经 Nginx → 127.0.0.1:%d，不要把 8502 暴露出去；" % args.port,
                  flush=True)
            print("    确实要直接对外（容器 / 内网隔离环境）需同时满足：", flush=True)
            print("      --allow-remote  +  GACHA_ALLOW_REMOTE=1", flush=True)
            return 2

    print(f"[*] 计算内核 {gc.VERSION}，算法 {' / '.join(MODES)}", flush=True)
    print(f"[*] 监听 http://{args.host}:{args.port}"
          f"{'' if args.host in loopback else '（已显式放开对外监听）'}", flush=True)
    if WORKER_TOKEN == "dev-worker-token":
        print("[!] 正在用默认 WORKER_TOKEN，上线前务必换成随机值", flush=True)

    if args.prod:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=8)
        return 0
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

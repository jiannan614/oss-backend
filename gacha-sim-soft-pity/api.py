#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 软保底池模拟服务（无状态 JSON）

「连续 N 抽不出货就逐抽加息」的经典模型：基础概率固定，软保底阶段线性递增，
到硬保底抽数必定出货。状态由客户端持有，服务端无会话。

    GET  /api/health                探活
    GET  /api/v1/sim/meta           卡池文案、规则、常量、按钮、分档阈值
    POST /api/v1/sim/soft/draw      {state, n}      → 抽 n 次
    POST /api/v1/sim/soft/reset     {}              → 回到初始态
    POST /api/v1/sim/soft/view      {state}         → 只算展示模型，不抽
    POST /api/v1/sim/soft/mc        {state, runs}   → 蒙特卡洛

档位：请求头 X-Client-Tier: member|guest 决定单次蒙特卡洛上限（20 万 / 3 万轮）。

    pip install flask waitress numpy
    python api.py --port 8507
"""

from __future__ import annotations

import argparse
import os

from flask import Flask, request


import core as engine  # noqa: E402

import common as api  # noqa: E402

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = api.MAX_BODY

POOL = "soft"

STATE = {
    "total": ("int", 2_000_000),
    "pity": ("int", 10_000),
    "since_five_plus": ("int", 10_000),
    "well_used": ("bool", 0),
    "count_six": ("int", 1_000_000),
    "count_five": ("int", 1_000_000),
    "count_four": ("int", 1_000_000),
    "count_three": ("int", 1_000_000),
    "count_main_up": ("int", 100_000),
    "count_gift_up": ("int", 100_000),
    # 六星明细（名字 → 次数）：只用来算出货一览里的「副 UP 角色 | UP」与「其他六星」
    "six_detail": ("json", 0),
}


# --------------------------------------------------------------------------- #
# 展示模型
# --------------------------------------------------------------------------- #
def build_view(core: engine.AKPool) -> dict:
    units = core.main_up_units
    side_up = core.six_detail.get(engine.SIDE_UP, 0)
    avg_six = core.total / core.count_six if core.count_six else 0.0
    avg_up = core.total / units if units else 0.0
    return {
        "pity": [
            {"label": "集齐主 UP", "value": f"{units} / {engine.TARGET_MAIN_UP}",
             "ratio": units / engine.TARGET_MAIN_UP},
            {"label": "六星保底", "value": f"{core.pity} / {engine.SIX_HARD_PITY}",
             "ratio": core.pity / engine.SIX_HARD_PITY},
            {"label": f"{engine.WELL_AT} 抽赠送",
             "value": "已领取" if core.well_used else f"{core.total} / {engine.WELL_AT}",
             "ratio": 1.0 if core.well_used else core.total / engine.WELL_AT,
             "muted": core.well_used},
        ],
        "drops": [
            {"label": f"{engine.MAIN_UP} | UP", "tone": "six",
             "count": core.count_main_up + core.count_gift_up},
            {"label": f"{engine.SIDE_UP} | UP", "tone": "six", "count": side_up},
            {"label": "其他六星", "tone": "six",
             "count": core.count_six - core.count_main_up - side_up},
            {"label": "五星", "tone": "five", "count": core.count_five},
            {"label": "四星", "tone": "four", "count": core.count_four},
            {"label": "三星", "tone": "three", "count": core.count_three},
        ],
        "kpis": [
            {"label": "总抽数", "value": f"{core.total:,}"},
            {"label": "六星", "value": f"{core.count_six} 个"
                                       + (f" · 每 {avg_six:.1f} 抽" if core.count_six else "")},
            {"label": "已获主 UP", "value": f"{units} / {engine.TARGET_MAIN_UP}"
                                           + (f" · 含赠送 {core.count_gift_up}"
                                              if core.count_gift_up else "")},
            {"label": "消耗抽卡货币", "value": f"{core.total * engine.GEM_PER_DRAW:,}"},
            {"label": "平均", "value": f"{avg_up:.1f} 抽 / 个 UP" if units else "—"},
        ],
        "donut": {
            "title": "稀有度占比",
            "items": [
                {"label": "6 星", "count": core.count_six, "tone": "six"},
                {"label": "5 星", "count": core.count_five, "tone": "five"},
                {"label": "4 星", "count": core.count_four, "tone": "four"},
                {"label": "3 星", "count": core.count_three, "tone": "three"},
            ],
            "empty": "还没有结果。",
        },
        "free_ten": None,
        "pending": False,
    }


# --------------------------------------------------------------------------- #
# 抽卡
# --------------------------------------------------------------------------- #
def do_draw(core: engine.AKPool, n: int) -> dict:
    batch: list[dict] = []
    rows: list[dict] = []
    six_rows: list[dict] = []

    for ev in core.run(n):
        if isinstance(ev, engine.DrawResult):
            up = ev.name in (engine.MAIN_UP, engine.SIDE_UP)
            batch.append({"name": ev.name, "rarity": ev.rarity, "up": up})
            rows.append({
                "抽数": ev.index,
                "星级": engine.RARITY_LABEL.get(ev.rarity, "?"),
                "角色": ev.name,
                "备注": ev.note or ("本期 UP" if up else ""),
            })
            if ev.rarity == engine.SIX:
                six_rows.append({
                    "index": ev.index,
                    "source": "常规" + (f" · {ev.note}" if ev.note else ""),
                    "name": ev.name + ("|UP" if up else ""),
                })
        elif isinstance(ev, engine.RewardEvent):
            rows.append({
                "抽数": ev.index, "星级": "赠送",
                "角色": ev.name, "备注": f"累计 {engine.WELL_AT} 抽赠送兑换券",
            })
            # 300 抽赠送的也是主 UP 兑换券，一并记进六星记录
            six_rows.append({
                "index": ev.index,
                "source": f"{engine.WELL_AT} 抽赠送",
                "name": ev.name + "|UP",
            })
    return {"batch": batch, "rows": rows, "six_rows": six_rows}


META = {
    "id": POOL,
    "slug": "sim-soft",
    "title": "玩家·周年庆限定抽取",
    "up": f"{engine.MAIN_UP} / {engine.SIDE_UP}",
    "banner": engine.BANNER,
    "period": engine.PERIOD,
    "version": engine.VERSION,
    "noun": "角色",
    "rules": [
        f"六星 {engine.SIX_BASE:.0%}",
        f"{engine.SIX_SOFT_START + 1} 抽起 +{engine.SIX_STEP:.0%}",
        f"{engine.SIX_HARD_PITY} 抽必出",
        f"双 UP 各 {engine.UP_SHARE:.0%}",
        f"{engine.WELL_AT} 抽送兑换券",
    ],
    "tip": ("结果窗口只铺两排 —— 百连这种一次抽很多的情况，后面几排要往下滚才看得到；"
            "完整明细在页面下方的「抽卡记录」里。"),
    "rule_blocks": [
        {"type": "p", "text": f"**六星**：{engine.MAIN_UP}（UP）、{engine.SIDE_UP}（UP），"
                              f"另有 {len(engine.OTHER_SIX)} 名常驻 / 限定"},
        {"type": "ul", "items": [
            f"概率：基础 {engine.SIX_BASE:.0%}；第 {engine.SIX_SOFT_START + 1} 抽起每抽 "
            f"+{engine.SIX_STEP:.0%}，第 {engine.SIX_HARD_PITY} 抽必出",
            f"出货分配：双 UP 合计占六星 {engine.UP_SHARE * 2:.0%}（各 {engine.UP_SHARE:.0%}）",
            f"赠送：累计 {engine.WELL_AT} 抽额外送 1 个 {engine.MAIN_UP} 兑换券，仅一次",
            f"单价：{engine.GEM_PER_DRAW} 抽卡货币 / 抽",
        ]},
        {"type": "caption", "text": "纯娱乐模拟，数值不代表游戏内真实情况。"},
    ],
    "buttons": [
        {"label": "单抽", "n": 1, "primary": True},
        {"label": "十连", "n": 10},
        {"label": "百连", "n": 100},
    ],
    "record_label": "抽卡记录",
    "six_log_label": "六星记录",
    "high_tone": "six",
    "currency": "抽卡货币",
    "empty_hint": "点下面的按钮开始抽，结果显示在这里",
    "empty_stats": "还没开始抽：点上面的按钮，这里会出现星级分布和关键指标。",
    "mc_title": "抽到满阶需要多少抽",
    "mc_hint": (f"每一轮都从零开始抽，直到集齐 {engine.TARGET_MAIN_UP} 个主 UP"
                f"（1 本体 + 5 兑换券，含 {engine.WELL_AT} 抽赠送）。"),
    "consts": {"target": engine.TARGET_MAIN_UP, "well_at": engine.WELL_AT,
               "gem_per_draw": engine.GEM_PER_DRAW},
}


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
def _bundle(core, drawn: dict) -> dict:
    return {**drawn, "state": api.export_state(core, STATE), "view": build_view(core)}


@app.get("/api/health")
def health():
    return api.ok({"service": "sim-soft-api", "engine": f"core {engine.VERSION}", "pools": [POOL]})


@app.get("/api/v1/sim/meta")
def meta():
    guarded = api.guard()
    if guarded:
        return guarded
    return api.ok({
        "service": "soft",
        "pools": {POOL: META},
        "tiers": {POOL: [{"label": t[0], "lo": t[1], "hi": t[2]} for t in engine.MC_TIERS]},
    })


def _bad_pool(pool: str):
    if pool != POOL:
        return api.fail("not_found", f"这个服务只有 {POOL}，没有 {pool}", 404)
    return None


@app.post("/api/v1/sim/<pool>/draw")
def draw(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    bad = _bad_pool(pool)
    if bad:
        return bad

    body = api.payload()
    n = api.read_int(body, "n", 1, 1, api.MAX_DRAW)
    core = engine.AKPool()
    api.apply_state(core, body.get("state"), STATE)
    return api.ok(_bundle(core, do_draw(core, n)), meta={"pool": POOL, "n": n})


@app.post("/api/v1/sim/<pool>/reset")
def reset(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    bad = _bad_pool(pool)
    if bad:
        return bad
    core = engine.AKPool()
    core.reset()
    return api.ok(_bundle(core, {"batch": [], "rows": [], "six_rows": []}), meta={"pool": POOL})


@app.post("/api/v1/sim/<pool>/view")
def view(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    bad = _bad_pool(pool)
    if bad:
        return bad
    body = api.payload()
    core = engine.AKPool()
    api.apply_state(core, body.get("state"), STATE)
    return api.ok({"state": api.export_state(core, STATE), "view": build_view(core)},
                  meta={"pool": POOL})


@app.post("/api/v1/sim/<pool>/mc")
def monte_carlo(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    bad = _bad_pool(pool)
    if bad:
        return bad

    tier = (request.headers.get("X-Client-Tier") or "guest").strip().lower()
    limit = api.tier_limit(tier)
    runs = api.read_int(api.payload(), "runs", 10_000, 100, limit)

    summary = engine.simulate_summary(runs, engine.TARGET_MAIN_UP)
    summary["tiers"] = [
        {"label": row["label"], "lo": row["lo"], "hi": row["hi"],
         "count": row["count"], "pct": row["pct"]}
        for row in summary.get("tiers", [])
    ]
    summary["limit"] = limit
    return api.ok(summary, meta={"pool": POOL, "tier": tier})


@app.errorhandler(404)
def not_found(_exc):
    return api.fail("not_found", "接口不存在", 404)


@app.errorhandler(405)
def bad_method(_exc):
    return api.fail("method_not_allowed", "方法不对", 405)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api.py", description="软保底池抽卡模拟 JSON 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("GACHA_SOFT_API_PORT", "8507")))
    parser.add_argument("--prod", action="store_true")
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"[x] 拒绝监听 {args.host}：抽卡服务只允许本地回环地址。", flush=True)
        return 2

    print(f"[*] 软保底池内核 {engine.VERSION}，池子 {POOL}", flush=True)
    print(f"[*] 监听 http://{args.host}:{args.port}", flush=True)

    if args.prod:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=8)
        return 0
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

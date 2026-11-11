#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 三阶段保底池模拟服务（无状态 JSON）

软保底不是一条直线，而是分三段加速（见 core.py 的阶段常量），最后一段直接抬到
硬保底。状态由客户端持有，服务端无会话。

    GET  /api/health                 探活
    GET  /api/v1/sim/meta            卡池文案、规则、常量、按钮、分档阈值
    POST /api/v1/sim/stage/draw      {state, n}      → 抽 n 次
    POST /api/v1/sim/stage/reset     {}              → 回到初始态
    POST /api/v1/sim/stage/view      {state}         → 只算展示模型，不抽
    POST /api/v1/sim/stage/mc        {state, runs}   → 蒙特卡洛

档位：请求头 X-Client-Tier: member|guest 决定单次蒙特卡洛上限（20 万 / 3 万轮）。

    pip install flask waitress numpy
    python api.py --port 8508
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

POOL = "stage"

STATE = {
    "total": ("int", 2_000_000),
    "soft_pity": ("int", 10_000),
    "four_pity": ("int", 10_000),
    "up_guaranteed": ("bool", 0),
    "four_guaranteed": ("bool", 0),
    "count_five": ("int", 1_000_000),
    "count_five_up": ("int", 100_000),
    "count_four": ("int", 1_000_000),
    "count_four_up": ("int", 1_000_000),
    "count_three": ("int", 1_000_000),
    "count_big_pity": ("int", 100_000),
    "five_detail": ("json", 0),
}


def build_view(core: engine.MCCore) -> dict:
    up_rate = core.count_five_up / core.count_five if core.count_five else 0.0
    avg_five = core.total / core.count_five if core.count_five else 0.0
    avg_up = core.total / core.count_five_up if core.count_five_up else 0.0
    return {
        "pity": [
            {"label": "满阶", "value": f"{core.count_five_up} / {engine.TARGET_UP}",
             "ratio": core.count_five_up / engine.TARGET_UP},
            {"label": "五星保底", "value": f"{core.soft_pity} / {engine.HARD_PITY}",
             "ratio": core.soft_pity / engine.HARD_PITY},
            {"label": "四星保底", "value": f"{core.four_pity} / {engine.FOUR_PITY}",
             "ratio": core.four_pity / engine.FOUR_PITY},
        ],
        "drops": [
            {"label": f"{engine.UP} | UP", "tone": "five", "count": core.count_five_up},
            {"label": "其他五星", "tone": "five",
             "count": core.count_five - core.count_five_up},
            {"label": "四星", "tone": "four", "count": core.count_four},
            {"label": "三星", "tone": "three", "count": core.count_three},
        ],
        "kpis": [
            {"label": "总抽数", "value": f"{core.total:,}"},
            {"label": "五星", "value": f"{core.count_five} 个"
                                       + (f" · 每 {avg_five:.1f} 抽" if core.count_five else "")},
            {"label": "已获 UP", "value": f"{core.count_five_up} / {engine.TARGET_UP}"
                                          + (" · 已满阶" if core.count_five_up >= engine.TARGET_UP else "")},
            {"label": "消耗抽卡货币", "value": f"{core.total * engine.GEM_PER_DRAW:,}"},
            {"label": "平均", "value": f"{avg_up:.1f} 抽 / 个 UP" if core.count_five_up else "—"},
        ],
        "donut": {
            "title": "星级占比",
            "items": [
                {"label": "5 星", "count": core.count_five, "tone": "five"},
                {"label": "4 星", "count": core.count_four, "tone": "four"},
                {"label": "3 星", "count": core.count_three, "tone": "three"},
            ],
            "empty": "还没有结果。",
        },
        "free_ten": None,
        "pending": False,
        # 旧界面在五星记录下面还有一行小字，放在这里由前端渲染
        "notes": [f"五星中 UP 占比 {up_rate:.1%}，大保底触发 {core.count_big_pity} 次。"]
                  if core.total else [],
    }


def do_draw(core: engine.MCCore, n: int) -> dict:
    batch: list[dict] = []
    rows: list[dict] = []
    five_rows: list[dict] = []
    for ev in core.run(n):
        up = ev.name == engine.UP
        batch.append({"name": ev.name, "rarity": ev.rarity, "up": up})
        rows.append({
            "抽数": ev.index,
            "星级": engine.RARITY_LABEL.get(ev.rarity, "?"),
            "名称": ev.name,
            "备注": ev.note or ("本期 UP" if up else ""),
        })
        if ev.rarity == engine.FIVE:
            five_rows.append({
                "index": ev.index,
                "source": "常规" + (f" · {ev.note}" if ev.note else ""),
                "name": ev.name + ("|UP" if up else ""),
            })
    return {"batch": batch, "rows": rows, "six_rows": five_rows}


META = {
    "id": POOL,
    "slug": "sim-stage",
    "title": "玩家·角色活动抽取",
    "up": engine.UP,
    "banner": engine.BANNER,
    "period": engine.PERIOD,
    "version": engine.VERSION,
    "noun": "角色",
    "rules": [
        f"五星 {engine.FIVE_BASE / 10:.1f}%",
        f"{engine.SOFT_START} 抽起软保底",
        f"{engine.HARD_PITY} 抽必出",
        "歪了下次必 UP",
        f"{engine.FOUR_PITY} 抽保底四星",
    ],
    "tip": ("结果窗口只铺两排 —— 百连这种一次抽很多的情况，后面几排要往下滚才看得到；"
            "完整明细在页面下方的「抽取记录」里。"),
    "rule_blocks": [
        {"type": "p", "text": f"**五星**：{engine.UP}（UP）、"
                              f"{'、'.join(n for n in engine.POOL_FIVE if n != engine.UP)}（常驻）"},
        {"type": "p", "text": "**四星 UP**：" + "、".join(f"{n}（UP）" for n in engine.FOUR_UP)},
        {"type": "p", "text": "**四星角色（常驻）**：" + "、".join(engine.FOUR_ROLE)},
        {"type": "p", "text": "**四星武器**：" + "、".join(engine.FOUR_WEAPON)},
        {"type": "p", "text": "**三星武器**：" + "、".join(engine.POOL_THREE)},
        {"type": "ul", "items": [
            f"概率：五星基础 {engine.FIVE_BASE / 10:.1f}%，第 {engine.SOFT_START} 抽起三阶段软保底，"
            f"第 {engine.HARD_PITY} 抽硬保底",
            f"五星权重：{' : '.join(str(w) for w in engine.WEIGHTS_FIVE)}"
            f"（UP 天然占五成，叠加「歪了下次必 UP」后约 66.7%）",
            f"四星：基础 {engine.FOUR_BASE}%，{engine.FOUR_PITY} 抽内必出；四星内容里 UP 占五成"
            f"（{len(engine.FOUR_UP)} 个 UP 均等），歪了下次四星必 UP",
            f"单价：{engine.GEM_PER_DRAW} 抽卡货币 / 抽；满阶 = {engine.TARGET_UP} 个 UP",
        ]},
        {"type": "caption", "text": "纯娱乐模拟，数值不代表游戏内真实情况。"},
    ],
    "buttons": [
        {"label": "单抽", "n": 1, "primary": True},
        {"label": "十连", "n": 10},
        {"label": "百连", "n": 100},
    ],
    "record_label": "抽取记录",
    "six_log_label": "五星记录",
    # 这个池子最高五星：记录表用金色，不用六星红
    "high_tone": "five",
    "currency": "抽卡货币",
    "empty_hint": "点下面的按钮开始抽取，结果显示在这里",
    "empty_stats": "还没开始抽：点上面的按钮，这里会出现星级分布和关键指标。",
    "mc_title": "抽到满阶（7 个 UP）需要多少抽",
    "mc_hint": (f"每一轮都从零开始抽，直到集满 {engine.TARGET_UP} 个 UP"
                f"（满阶 = {engine.TARGET_UP} 个，1 抽 = {engine.GEM_PER_DRAW} 抽卡货币）。"),
    "consts": {"target": engine.TARGET_UP, "gem_per_draw": engine.GEM_PER_DRAW},
}


def _bundle(core, drawn: dict) -> dict:
    return {**drawn, "state": api.export_state(core, STATE), "view": build_view(core)}


@app.get("/api/health")
def health():
    return api.ok({"service": "sim-stage-api", "engine": f"core {engine.VERSION}", "pools": [POOL]})


@app.get("/api/v1/sim/meta")
def meta():
    guarded = api.guard()
    if guarded:
        return guarded
    return api.ok({
        "service": "stage",
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
    core = engine.MCCore()
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
    core = engine.MCCore()
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
    core = engine.MCCore()
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

    summary = engine.simulate_summary(runs, engine.TARGET_UP)
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
    parser = argparse.ArgumentParser(prog="api.py", description="三阶段池抽卡模拟 JSON 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("GACHA_STAGE_API_PORT", "8508")))
    parser.add_argument("--prod", action="store_true")
    args = parser.parse_args(argv)

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"[x] 拒绝监听 {args.host}：抽卡服务只允许本地回环地址。", flush=True)
        return 2

    print(f"[*] 三阶段池内核 {engine.VERSION}，池子 {POOL}", flush=True)
    print(f"[*] 监听 http://{args.host}:{args.port}", flush=True)

    if args.prod:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=8)
        return 0
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 武器池模拟服务（无状态 JSON）

与角色池的区别：最高稀有度概率恒定、没有软保底，靠「N 抽必出」与里程碑推进。
状态同样由客户端持有，服务端无会话。

    GET  /api/health                  探活
    GET  /api/v1/sim/meta             卡池文案、规则、常量、按钮、分档阈值
    POST /api/v1/sim/weapon/draw      {state, n}      → 抽 n 次
    POST /api/v1/sim/weapon/reset     {}              → 回到初始态
    POST /api/v1/sim/weapon/view      {state}         → 只算展示模型，不抽
    POST /api/v1/sim/weapon/mc        {state, runs}   → 蒙特卡洛

档位：请求头 X-Client-Tier: member|guest 决定单次蒙特卡洛上限（20 万 / 3 万轮）。

    pip install flask waitress numpy
    python api.py --port 8506
"""

from __future__ import annotations

import argparse
import os
import sys

from flask import Flask, request

# 内核就在隔壁目录，显式加进 sys.path（不依赖 cwd，双击 / systemd 都能起）

import core as engine  # noqa: E402

import common as api  # noqa: E402

app = Flask(__name__)
app.json.ensure_ascii = False
# 记录表的列顺序由 row 的键序决定（抽数 / 来源 / 星级 / 角色 / 备注），
# 一旦被按字母排序，前端表头就会变成「备注、角色、抽数…」，与旧界面不一致。
app.json.sort_keys = False
app.config["MAX_CONTENT_LENGTH"] = api.MAX_BODY


# --------------------------------------------------------------------------- #
# 状态规格：只搬这些字段，别的（rng、派生属性）一律不搬
# --------------------------------------------------------------------------- #

WPN_STATE = {
    "total": ("int", 2_000_000),
    "six_pity": ("int", 10_000),
    "up_pity": ("int", 10_000),
    "up_active": ("bool", 0),
    "up_consumed_at": ("int", 2_000_000),
    "up_reason": ("str", 32),
    "count_six": ("int", 1_000_000),
    "count_five": ("int", 1_000_000),
    "count_four": ("int", 1_000_000),
    "count_up": ("int", 100_000),
    "count_box": ("int", 100_000),
    "count_gift_up": ("int", 100_000),
    "count_forced_up": ("int", 100_000),
    # 里程碑游标与「本期是否出过 5★+」：不搬它们，抽到一半刷新就会重复开箱
    "_box_seen": ("int", 100),
    "_up_seen": ("int", 100),
    "_period": ("int", 100_000),
    "_period_high": ("bool", 0),
}


# --------------------------------------------------------------------------- #
# 视图模型：后端算「展示什么」，前端只管排版
# --------------------------------------------------------------------------- #
def _wpn_view(core: engine.WeaponCore) -> dict:
    nxt_box = min([d for d in engine.BOX_DRAWS if d > core.total], default=None)
    nxt_up = min([d for d in engine.UP_DRAWS if d > core.total], default=None)
    marks = [d for d in (nxt_box, nxt_up) if d]
    nxt = min(marks) if marks else None

    up_state = (
        {"label": "UP 保底", "value": f"{core.up_pity} / {engine.UP_PITY}",
         "ratio": core.up_pity / engine.UP_PITY}
        if core.up_active
        else {"label": "UP 保底", "value": f"已用完 @ {core.up_consumed_at} 抽",
              "ratio": 1.0, "muted": True}
    )
    avg_six = core.total / core.count_six if core.count_six else 0.0
    avg_up = core.total / core.up_units if core.up_units else 0.0
    return {
        "pity": [
            {"label": "六星保底", "value": f"{core.six_pity} / {engine.SIX_PITY}",
             "ratio": core.six_pity / engine.SIX_PITY},
            up_state,
            ({"label": "下一个里程碑", "value": f"{nxt} 抽", "ratio": core.total / nxt}
             if nxt else {"label": "里程碑", "value": "已到顶", "ratio": 1.0}),
        ],
        "drops": [
            {"label": f"{engine.WPN_UP} | UP", "tone": "six", "count": core.up_units},
            {"label": "其他六星", "tone": "six", "count": core.six_total - core.count_up},
            {"label": "五星", "tone": "five", "count": core.count_five},
            {"label": "四星", "tone": "four", "count": core.count_four},
        ],
        "kpis": [
            {"label": "总抽数", "value": f"{core.total:,}"},
            {"label": "六星", "value": f"{core.count_six} 个"
                                       + (f" · 每 {avg_six:.1f} 抽" if core.count_six else "")},
            {"label": "已获 UP", "value": f"{core.up_units}"
                                         + (f" · 含里程碑 {core.count_gift_up}"
                                            if core.count_gift_up else "")},
            {"label": "里程碑箱", "value": f"{core.count_box} 次"},
            {"label": "平均", "value": f"{avg_up:.1f} 抽 / 把 UP" if core.up_units else "—"},
        ],
        "donut": {
            "title": "稀有度占比",
            "items": [
                {"label": "6 星", "count": core.six_total, "tone": "six"},
                {"label": "5 星", "count": core.count_five, "tone": "five"},
                {"label": "4 星", "count": core.count_four, "tone": "four"},
            ],
            "empty": "还没有结果。",
            "note": (f"其中里程碑箱开出 {core.count_box} 个六星，已计入上图。"
                     if core.count_box else ""),
        },
        "free_ten": None,
        "pending": False,
    }


# --------------------------------------------------------------------------- #
# 抽卡：把事件翻成「结果窗口 / 记录表 / 六星记录」三份前端要的东西
# --------------------------------------------------------------------------- #
def _wpn_draw(core: engine.WeaponCore, n: int) -> dict:
    batch: list[dict] = []
    rows: list[dict] = []
    six_rows: list[dict] = []
    for drop in core.draw(n):
        batch.append({"name": drop.name, "rarity": drop.rarity, "up": drop.is_up})
        rows.append({
            "抽数": drop.index,
            "来源": drop.source,
            "星级": engine.RARITY_LABEL[drop.rarity],
            "武器": drop.name,
            "备注": drop.note,
        })
        if drop.rarity == engine.SIX:
            six_rows.append({
                "index": drop.index,
                "source": drop.source,   # 常规 / 里程碑箱 / 里程碑 UP
                "name": drop.name + ("|UP" if drop.is_up else ""),
            })
    return {"batch": batch, "rows": rows, "six_rows": six_rows}


# --------------------------------------------------------------------------- #
# 池子注册表
# --------------------------------------------------------------------------- #
POOLS = {
    "weapon": {
        "core": engine.WeaponCore,
        "state": WPN_STATE,
        "view": _wpn_view,
        "draw": _wpn_draw,
        "free_ten": None,
        "mc": engine.simulate_summary,
        "tiers": engine.MC_TIERS,
        "meta": {
            "id": "weapon",
            "slug": "sim-weapon",
            "title": "武器池模拟",
            "up": engine.WPN_UP, "banner": engine.WPN_BANNER, "period": engine.WPN_PERIOD,
            "version": engine.VERSION,
            "noun": "武器",
            "rules": [
                f"6★ {engine.RATE_SIX:.0%} 恒定",
                f"{engine.SIX_PITY} 抽保底循环",
                f"{engine.UP_PITY} 抽必出 UP",
                f"{engine.UP_PERIODS[0]} 期起送 UP",
                f"1 期 {engine.DRAWS_PER_PERIOD} 抽",
            ],
            "tip": ("结果窗口只铺两排 —— 八十连这种一次抽很多的情况，后面几排要往下滚才看得到；"
                    "完整明细在页面下方的「武器抽卡记录」里。"),
            "rule_blocks": [
                {"type": "p", "text": "**六星**：" + "，".join(
                    f"{name}（UP）" if name == engine.WPN_UP else name
                    for name in [engine.WPN_UP, *engine.OTHER_SIX_POOL])},
                {"type": "p", "text": "**五星**：" + "、".join(engine.FIVE_POOL)},
                {"type": "p", "text": "**四星**：" + "、".join(engine.FOUR_POOL)},
                {"type": "ul", "items": [
                    f"概率：6★ 恒定 {engine.RATE_SIX:.0%}，无软保底",
                    f"六星保底：{engine.SIX_PITY} 抽内必出，出即重置、循环",
                    f"UP 保底：{engine.UP_PITY} 抽必得 UP，一次性、不继承",
                    f"里程碑：{' / '.join(map(str, engine.BOX_PERIODS))} 期开里程碑箱（非 UP 六星），"
                    f"{' / '.join(map(str, engine.UP_PERIODS))} 期直接送 UP 武器"
                    f"（1 期 = {engine.DRAWS_PER_PERIOD} 抽）",
                ]},
                {"type": "caption", "text": "纯娱乐模拟，数值不代表游戏内真实情况。"},
            ],
            "buttons": [
                {"label": "十连", "n": 10, "primary": True},
                {"label": "八十连", "n": 80},
            ],
            "record_label": "武器抽卡记录",
            "six_log_label": "六星记录",
            "high_tone": "six",
            "consts": {
                "target": engine.TARGET_UP_WEAPONS,
                "six_pity": engine.SIX_PITY,
                "up_pity": engine.UP_PITY,
            },
            "empty_hint": "点下面的按钮开始申领，结果显示在这里",
            "empty_stats": "还没开始抽：点上面的按钮，这里会出现星级分布和关键指标。",
            "mc_title": "抽到满阶需要多少抽",
            "mc_hint": (f"每一轮都从零开始申领，直到集齐 {engine.TARGET_UP_WEAPONS} 把 UP 武器"
                        f"（含里程碑赠送）。"),
        },
    },
}


def _bundle(pool: str, core, drawn: dict) -> dict:
    """一次响应里带齐：新状态 + 本次事件 + 展示模型。"""
    spec = POOLS[pool]["state"]
    return {
        **drawn,
        "state": api.export_state(core, spec),
        "view": POOLS[pool]["view"](core),
    }


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return api.ok({
        "service": "sim-weapon-api",
        "engine": f"core {engine.VERSION}",
        "pools": list(POOLS),
    })


@app.get("/api/v1/sim/meta")
def meta():
    """两个池子的静态文案与常量。前端拿它渲染抬头、规则、按钮、分档。"""
    guarded = api.guard()
    if guarded:
        return guarded
    return api.ok({
        "service": "weapon",
        "pools": {name: spec["meta"] for name, spec in POOLS.items()},
        "tiers": {name: [{"label": lo_t[0], "lo": lo_t[1], "hi": lo_t[2]}
                         for lo_t in spec["tiers"]] for name, spec in POOLS.items()},
    })


def _pool(name: str):
    spec = POOLS.get(name)
    if spec is None:
        return None, api.fail("not_found", f"没有这个池子：{name}", 404)
    return spec, None


@app.post("/api/v1/sim/<pool>/draw")
def draw(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    spec, bad = _pool(pool)
    if bad:
        return bad

    body = api.payload()
    n = api.read_int(body, "n", 1, 1, api.MAX_DRAW)
    core = spec["core"]()
    api.apply_state(core, body.get("state"), spec["state"])
    drawn = spec["draw"](core, n)
    return api.ok(_bundle(pool, core, drawn), meta={"pool": pool, "n": n})


@app.post("/api/v1/sim/<pool>/free-ten")
def free_ten(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    spec, bad = _pool(pool)
    if bad:
        return bad
    if spec["free_ten"] is None:
        return api.fail("not_supported", "这个池子没有加急招募", 400)

    body = api.payload()
    core = spec["core"]()
    api.apply_state(core, body.get("state"), spec["state"])
    drawn = spec["free_ten"](core)
    return api.ok(_bundle(pool, core, drawn), meta={"pool": pool})


@app.post("/api/v1/sim/<pool>/reset")
def reset(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    spec, bad = _pool(pool)
    if bad:
        return bad

    core = spec["core"]()
    core.reset()
    return api.ok(_bundle(pool, core, {"batch": [], "rows": [], "six_rows": []}),
                  meta={"pool": pool})


@app.post("/api/v1/sim/<pool>/view")
def view(pool: str):
    """按前端带回来的状态重算展示模型（不抽卡、不改状态）。

    刷新页面时用：服务端不存会话，前端把 state 传上来，这里复原内核、
    重新算一遍保底进度与指标，界面就和刷新前一模一样。
    """
    guarded = api.guard()
    if guarded:
        return guarded
    spec, bad = _pool(pool)
    if bad:
        return bad

    body = api.payload()
    core = spec["core"]()
    api.apply_state(core, body.get("state"), spec["state"])
    return api.ok({
        "state": api.export_state(core, spec["state"]),
        "view": spec["view"](core),
    }, meta={"pool": pool})


@app.post("/api/v1/sim/<pool>/mc")
def monte_carlo(pool: str):
    guarded = api.guard()
    if guarded:
        return guarded
    spec, bad = _pool(pool)
    if bad:
        return bad

    # 档位由 调用方 代理时按登录态注入：会员 500 万、游客 1 万
    tier = (request.headers.get("X-Client-Tier") or "guest").strip().lower()
    limit = api.tier_limit(tier)
    body = api.payload()
    runs = api.read_int(body, "runs", 10_000, 100, limit)
    summary = spec["mc"](runs)
    summary["tiers"] = [
        {"label": row["label"], "lo": row["lo"], "hi": row["hi"],
         "count": row["count"], "pct": row["pct"]}
        for row in summary.get("tiers", [])
    ]
    summary["limit"] = limit
    return api.ok(summary, meta={"pool": pool, "tier": tier})


@app.errorhandler(404)
def not_found(_exc):
    return api.fail("not_found", "接口不存在", 404)


@app.errorhandler(405)
def bad_method(_exc):
    return api.fail("method_not_allowed", "方法不对", 405)


@app.errorhandler(413)
def too_large(_exc):
    return api.fail("too_large", "请求体太大", 413)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api.py", description="本站抽卡模拟 JSON 服务")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（只对本地开放）")
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("GACHA_WEAPON_API_PORT", "8506")),
                        help="监听端口，默认 8506")
    parser.add_argument("--prod", action="store_true", help="用 waitress（需已安装）")
    args = parser.parse_args(argv)

    loopback = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback:
        print(f"[x] 拒绝监听 {args.host}：抽卡服务只允许本地回环地址。", flush=True)
        print("    前端一律经 Nginx → 调用方 代理 → 127.0.0.1:%d。" % args.port, flush=True)
        return 2

    print(f"[*] 本服务内核 {engine.VERSION}，池子 {' / '.join(POOLS)}",
          flush=True)
    print(f"[*] 监听 http://{args.host}:{args.port}", flush=True)

    if args.prod:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=8)
        return 0
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

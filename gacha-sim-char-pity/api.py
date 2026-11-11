#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 角色池模拟服务（无状态 JSON）

只做计算：收一个状态进来 → 抽 n 次 → 把新状态、本次事件、以及「要展示什么」
（保底进度 / 出货一览 / 关键指标 / 环形图分档）一起还回去。服务端不存会话，
进程随时可以重启，客户端把 state 原样带回来就能续上。

    GET  /api/health                探活
    GET  /api/v1/sim/meta           卡池文案、规则、常量、按钮、分档阈值
    POST /api/v1/sim/char/draw      {state, n}      → 抽 n 次
    POST /api/v1/sim/char/free-ten  {state}         → 领一次保底十连
    POST /api/v1/sim/char/reset     {}              → 回到初始态
    POST /api/v1/sim/char/view      {state}         → 只算展示模型，不抽
    POST /api/v1/sim/char/mc        {state, runs}   → 蒙特卡洛

档位：请求头 X-Client-Tier: member|guest 决定单次蒙特卡洛上限（20 万 / 3 万轮）。
这个头应由你自己的网关按登录态注入，不要信任浏览器直传。

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
CHAR_STATE = {
    "total": ("int", 2_000_000),
    "small_pity": ("int", 10_000),
    "five_pity": ("int", 10_000),
    "up_pity": ("int", 10_000),
    "big_pity_active": ("bool", 0),
    "big_pity_consumed_at": ("int", 2_000_000),
    "big_pity_reason": ("str", 32),
    "gift_step": ("int", 100_000),
    "count_six": ("int", 1_000_000),
    "count_five": ("int", 1_000_000),
    "count_four": ("int", 1_000_000),
    "count_up": ("int", 100_000),
    "count_gift": ("int", 100_000),
    "count_big_pity": ("int", 100_000),
    "free_ten_used": ("bool", 0),
    "free_ten_pending": ("bool", 0),
    "free_count_six": ("int", 1_000),
    "free_count_five": ("int", 1_000),
    "free_count_four": ("int", 1_000),
    "free_count_up": ("int", 1_000),
}



# --------------------------------------------------------------------------- #
# 视图模型：后端算「展示什么」，前端只管排版
# --------------------------------------------------------------------------- #
def _char_view(core: engine.GachaCore) -> dict:
    units = core.up_units
    big = (
        {"label": "大保底", "value": f"{core.up_pity} / {engine.BIG_PITY_MAX}",
         "ratio": core.up_pity / engine.BIG_PITY_MAX}
        if core.big_pity_active
        else {"label": "大保底", "value": f"已失效 @ {core.big_pity_consumed_at} 抽",
              "ratio": 1.0, "muted": True}
    )
    gift_done = core.count_gift
    gift_rest = int(core.total - engine.GIFT_INTERVAL * gift_done)
    avg_six = core.total / core.count_six if core.count_six else 0.0
    avg_up = core.total / units if units else 0.0
    return {
        "pity": [
            {"label": "集齐 UP", "value": f"{units} / {engine.TARGET_UP_UNITS}",
             "ratio": units / engine.TARGET_UP_UNITS},
            {"label": "小保底", "value": f"{core.small_pity} / {engine.SIX_HARD_PITY}",
             "ratio": core.small_pity / engine.SIX_HARD_PITY},
            big,
            {"label": f"{engine.GIFT_INTERVAL} 抽赠兑换券",
             "value": f"× {gift_done} · {gift_rest} / {engine.GIFT_INTERVAL}",
             "ratio": gift_rest / engine.GIFT_INTERVAL},
        ],
        "drops": [
            {"label": f"{engine.UP} | UP", "tone": "six",
             "count": core.count_up + core.free_count_up + core.count_gift},
            {"label": "其他六星", "tone": "six",
             "count": (core.count_six - core.count_up)
                      + (core.free_count_six - core.free_count_up)},
            {"label": "五星", "tone": "five", "count": core.count_five + core.free_count_five},
            {"label": "四星", "tone": "four", "count": core.count_four + core.free_count_four},
        ],
        "kpis": [
            {"label": "总抽数", "value": f"{core.total:,}"},
            {"label": "六星", "value": f"{core.count_six} 个"
                                       + (f" · 每 {avg_six:.1f} 抽" if core.count_six else "")},
            {"label": "已获 UP", "value": f"{units} / {engine.TARGET_UP_UNITS}"},
            {"label": "消耗抽卡货币", "value": f"{core.total * engine.GEM_PER_DRAW:,}"},
            {"label": "平均", "value": f"{avg_up:.1f} 抽 / 个 UP" if units else "—"},
        ],
        "donut": {
            "title": "稀有度占比",
            "items": [
                {"label": "6 星", "count": core.count_six, "tone": "six"},
                {"label": "5 星", "count": core.count_five, "tone": "five"},
                {"label": "4 星", "count": core.count_four, "tone": "four"},
            ],
            "empty": "还没有常规池的结果。",
        },
        "free_ten": {
            "used": core.free_ten_used,
            "six": core.free_count_six,
            "five": core.free_count_five,
            "four": core.free_count_four,
        },
        "pending": core.free_ten_pending,
    }


# --------------------------------------------------------------------------- #
# 抽卡：把事件翻成「结果窗口 / 记录表 / 六星记录」三份前端要的东西
# --------------------------------------------------------------------------- #
def _char_draw(core: engine.GachaCore, n: int) -> dict:
    batch: list[dict] = []
    rows: list[dict] = []
    six_rows: list[dict] = []

    def record(ev: engine.DrawResult) -> None:
        free = bool(ev.free_index)
        source = (f"加急 {ev.free_index}/{engine.FREE_TEN_SIZE}" if free
                  else (ev.note or "常规"))
        index = core.total if free else ev.index
        up = ev.name == engine.UP
        rows.append({
            "抽数": index,
            "来源": source,
            "星级": engine.RARITY_LABEL.get(ev.rarity, "?"),
            "角色": ev.name,
            "备注": ev.note or ("本期 UP" if up else ""),
        })
        if ev.rarity == engine.SIX:
            six_rows.append({
                "index": index,
                "source": source,
                "name": ev.name + ("|UP" if up else ""),
            })

    # 百连是连抽，中途到点的加急照样自动跑；单抽 / 十连挂起等玩家点
    for ev in core.run(n, auto_free=n >= 100):
        if isinstance(ev, engine.DrawResult):
            batch.append({"name": ev.name, "rarity": ev.rarity,
                          "up": ev.name == engine.UP, "free": bool(ev.free_index)})
            record(ev)
        elif isinstance(ev, engine.GiftEvent):
            rows.append({
                "抽数": ev.index, "来源": "赠送", "星级": "赠送",
                "角色": ev.name, "备注": f"累计 {engine.GIFT_INTERVAL} 抽赠送兑换券",
            })
            # 六星记录也把 240 抽赠送算进去（它送的正是 UP 兑换券）
            six_rows.append({
                "index": ev.index,
                "source": f"{engine.GIFT_INTERVAL} 抽赠送",
                "name": ev.name + "|UP",
            })
    return {"batch": batch, "rows": rows, "six_rows": six_rows}


def _char_free_ten(core: engine.GachaCore) -> dict:
    batch: list[dict] = []
    rows: list[dict] = []
    six_rows: list[dict] = []
    if not core.free_ten_pending:
        return {"batch": [], "rows": [], "six_rows": []}
    for ev in core.run_free_ten():
        if isinstance(ev, engine.DrawResult):
            batch.append({"name": ev.name, "rarity": ev.rarity,
                          "up": ev.name == engine.UP, "free": True})
            index = core.total
            up = ev.name == engine.UP
            source = f"加急 {ev.free_index}/{engine.FREE_TEN_SIZE}"
            rows.append({
                "抽数": index, "来源": source,
                "星级": engine.RARITY_LABEL.get(ev.rarity, "?"),
                "角色": ev.name, "备注": ev.note or ("本期 UP" if up else ""),
            })
            if ev.rarity == engine.SIX:
                six_rows.append({"index": index, "source": source,
                                 "name": ev.name + ("|UP" if up else "")})
    return {"batch": batch, "rows": rows, "six_rows": six_rows}


# --------------------------------------------------------------------------- #
# 池子注册表
# --------------------------------------------------------------------------- #
POOLS = {
    "char": {
        "core": engine.GachaCore,
        "state": CHAR_STATE,
        "view": _char_view,
        "draw": _char_draw,
        "free_ten": _char_free_ten,
        "mc": engine.simulate_summary,
        "tiers": engine.MC_TIERS,
        "meta": {
            "id": "char",
            "slug": "sim-char",
            "title": "角色池模拟",
            "up": engine.UP, "banner": engine.UP_BANNER, "period": engine.UP_PERIOD,
            "version": engine.VERSION,
            "noun": "角色",
            "rules": [
                f"六星 {engine.SIX_BASE_RATE / 10:.1f}%",
                f"{engine.SIX_SOFT_PITY + 1} 抽起上涨",
                f"{engine.SIX_HARD_PITY} 抽必出",
                f"{engine.BIG_PITY_MAX} 抽内必得 UP",
                f"{engine.GIFT_INTERVAL} 抽赠兑换券",
                f"{engine.FREE_TEN_TRIGGER} 抽送十连",
            ],
            "tip": ("结果窗口只铺两排 —— 百连这种一次抽很多的情况，后面几排要往下滚才看得到；"
                    "完整明细在页面下方的「角色抽卡记录」里。"),
            "rule_blocks": [
                {"type": "p", "text": "**六星**：" + "，".join(
                    f"{name}（UP）" if name == engine.UP else name for name in engine.POOL_SIX)},
                {"type": "ul", "items": [
                    f"概率：基础 {engine.SIX_BASE_RATE / 10:.1f}%，第 {engine.SIX_SOFT_PITY + 1} 抽起软保底，"
                    f"第 {engine.SIX_HARD_PITY} 抽硬保底",
                    f"大保底：前 {engine.BIG_PITY_MAX} 抽必得 UP，触发一次后失效",
                    f"赠送：每累计 {engine.GIFT_INTERVAL} 抽赠 1 个兑换券",
                    f"加急招募：累计 {engine.FREE_TEN_TRIGGER} 抽触发一次免费十连，"
                    f"独立计算、不计入任何保底与累抽",
                ]},
                {"type": "caption", "text": "纯娱乐模拟，数值不代表游戏内真实情况。"},
            ],
            "buttons": [
                {"label": "单抽", "n": 1, "primary": True},
                {"label": "十连", "n": 10},
                {"label": "百连", "n": 100},
            ],
            "record_label": "角色抽卡记录",
            "six_log_label": "六星记录",
            "high_tone": "six",
            "currency": "抽卡货币",
            "consts": {
                "gift_interval": engine.GIFT_INTERVAL,
                "free_ten_trigger": engine.FREE_TEN_TRIGGER,
                "free_ten_size": engine.FREE_TEN_SIZE,
                "target": engine.TARGET_UP_UNITS,
                "gem_per_draw": engine.GEM_PER_DRAW,
            },
            "empty_hint": "点下面的按钮开始抽，结果显示在这里",
            "empty_stats": "还没开始抽：点上面的按钮，这里会出现星级分布和逐抽明细。",
            "mc_title": "抽到满阶需要多少抽",
            "mc_hint": (f"每一轮都从零开始抽，直到集齐 {engine.TARGET_UP_UNITS} 个 UP 单位"
                        f"（1 本体 + 5 兑换券，含每 {engine.GIFT_INTERVAL} 抽赠送与加急招募的 UP）。"),
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
        "service": "sim-char-api",
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
        "service": "char",
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
                        default=int(os.getenv("GACHA_CHAR_API_PORT", "8506")),
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

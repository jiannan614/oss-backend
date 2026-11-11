# -*- coding: utf-8 -*-
"""抽卡模拟服务的公共层

三个抽卡服务（本站 / 软保底池 / 三阶段池）各自一个进程、一份 Flask 应用，
共用这里的四件事：

1. **统一信封**：`{ok, data}` / `{ok:false, error, message}`，和服务里其它接口一致，
   前端只认 `ok`，不猜 HTTP 码。
2. **令牌**：和 planner 共用 `WORKER_TOKEN`。没配令牌就放行 —— 本地裸跑方便，
   正式环境一定配了。
3. **状态搬运**：抽卡状态由**前端持有**，每次请求带上来、算完还回去。
   服务端不存任何会话：重启不丢进度、多线程不用加锁、测试可以直接重放。
4. **参数收敛**：前端传来的数字再离谱，也只会在合法区间里落地，
   不会把内核推进非法状态，也不会变成 DoS 杠杆。

为什么状态放前端：这个服务是玩具，抽卡进度本来就该跟这一个浏览器标签走。
放服务端要引入 session / 过期清理 / 多线程锁，收益是「刷新不丢」——
但刷新丢进度本来就是玩家预期内的事（重置按钮就摆在那儿）。
"""

from __future__ import annotations

import os
from typing import Any

from flask import jsonify, request

#: 单次请求体上限（状态 + 记录不多，2MB 足够；防止误传大文件把进程拖死）
MAX_BODY = 2 * 1024 * 1024

#: 抽卡次数的硬上限（一次请求）。百连是 100，留点余量给未来的「井」。
MAX_DRAW = 200

#: 蒙特卡洛单次上限（按**轮**）。真正的档位由 调用方 代理按登录态注入
#: `X-Client-Tier`，这里只是「就算头被伪造，也别想拿它当 DoS 杠杆」的最后一道。
#:
#: 20 万轮是前端输入框的上限，也是实测最舒服的点：四个池子分别 51ms / 430ms /
#: 457ms / 1160ms，峰值内存 12~68MB —— 几个人同时算也不会把机器压垮。
#: 日/月总额度（500 万 / 2000 万轮）记在 调用方 的 `mc_usage` 表里，不在这里。
MAX_RUNS_MEMBER = 200_000
MAX_RUNS_GUEST = 30_000

#: 兼容旧名（曾经只有一档 20 万）
MAX_RUNS = MAX_RUNS_MEMBER


def tier_limit(tier: str) -> int:
    """会员 20 万 / 游客 3 万。档位之外的脏值一律按游客算。"""
    return MAX_RUNS_MEMBER if (tier or "").strip().lower() == "member" else MAX_RUNS_GUEST


# --------------------------------------------------------------------------- #
# 信封
# --------------------------------------------------------------------------- #
def ok(data: Any, status: int = 200, **meta):
    body: dict[str, Any] = {"ok": True, "data": data}
    if meta:
        body["meta"] = meta
    return jsonify(body), status


def fail(code: str, message: str, status: int = 400, details: Any = None):
    body: dict[str, Any] = {"ok": False, "error": code, "message": message}
    if details is not None:
        body["details"] = details
    return jsonify(body), status


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def token_ok() -> bool:
    """共享令牌校验。没配 `WORKER_TOKEN` 视为本地开发，放行。"""
    expect = (os.getenv("WORKER_TOKEN") or "").strip()
    if not expect:
        return True
    got = (request.headers.get("X-API-Token") or "").strip()
    return got == expect


def guard():
    """需要令牌的接口在函数第一行调用：返回 None 表示通过。"""
    if not token_ok():
        return fail("bad_token", "服务间令牌不对", 403)
    return None


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
def payload() -> dict:
    """读 JSON body；不是对象就返回空 dict（调用方按默认值处理）。"""
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return {}
    return raw


def read_int(data: dict, key: str, default: int, lo: int, hi: int) -> int:
    """取一个整数并夹到 [lo, hi]；类型不对就用默认值（再夹一次）。"""
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def read_bool(data: dict, key: str, default: bool = False) -> bool:
    """取一个布尔。兼容前端传 "true"/"false" 字符串的情况。"""
    if key not in data:
        return default
    raw = data[key]
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


# --------------------------------------------------------------------------- #
# 状态搬运
# --------------------------------------------------------------------------- #
#: 字段规格：{字段名: (类型, 上限)}
#:   int  → 夹到 [0, 上限]
#:   bool → 转布尔
#:   str  → 截断到上限长度
#:   json → 只接受 dict（键截断、值转 int，最多 200 个键），用于「六星明细」这种计数表
StateSpec = dict[str, tuple[str, int]]


def export_state(core: Any, spec: StateSpec) -> dict:
    """把内核里登记过的字段原样导出成 JSON 可序列化的 dict。"""
    out: dict[str, Any] = {}
    for key, (kind, _limit) in spec.items():
        value = getattr(core, key)
        if kind == "bool":
            out[key] = bool(value)
        elif kind == "str":
            out[key] = str(value)
        elif kind == "json":
            out[key] = dict(value) if isinstance(value, dict) else {}
        else:
            out[key] = int(value)
    return out


def apply_state(core: Any, data: dict | None, spec: StateSpec) -> None:
    """把前端带回来的状态写回内核。

    缺字段就保持内核的默认值（等价于「从头开始」）；类型不对就跳过这一项，
    不让一个坏字段把整台机器带崩。
    """
    if not isinstance(data, dict):
        return
    for key, (kind, limit) in spec.items():
        if key not in data:
            continue
        raw = data[key]
        try:
            if kind == "bool":
                if isinstance(raw, str):
                    value: Any = raw.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    value = bool(raw)
            elif kind == "str":
                value = str(raw)[:limit]
            elif kind == "json":
                if not isinstance(raw, dict):
                    continue
                # 只收「名字 → 计数」这种形状；键数封顶，防止被塞成垃圾场
                items = list(raw.items())[:200]
                value = {str(k)[:64]: max(0, int(v)) for k, v in items
                         if isinstance(v, (int, float))}
            else:
                value = int(raw)
                value = 0 if value < 0 else min(value, limit)
        except (TypeError, ValueError):
            continue
        setattr(core, key, value)


def reset_state(core: Any, spec: StateSpec) -> dict:
    """把内核打回初始态，并返回它的状态快照。"""
    core.reset()
    return export_state(core, spec)

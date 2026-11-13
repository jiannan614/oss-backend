#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.py —— 抽数预算服务的 JSON 接口

把 budget_core.py（纯标准库）包一层 HTTP，供任意前端调用。两种用法：

* GET|POST /api/v1/budget/run —— 直接调用，同步返回「视图模型」
  （kpis / donut / tables / notes），前端照着排版即可；
* POST /internal/jobs/<job_id> —— 带令牌与 HMAC 签名的任务式调用，
  适合把计算放到独立机器上（协议见 README「服务间调用」）。

参数（都对同一个 run 生效）：
    orundum     抽卡货币数量
    originite   高级货币数量（按 1:75 折算）
    tickets     单抽券
    ten         十连券
    profile     new | veteran（玩家档位，影响保底配额与通用配额）
    pity        当前垫抽数（0~79）
    runs        模拟组数；**默认 5000，上限 5000**，一般不需要传

环境变量：
    WORKER_TOKEN    共享令牌；留空即不校验（仅限本机开发）
    WORKER_SECRET   HMAC 签名密钥，任务式调用需要

    pip install flask waitress
    python api.py --port 8510            # 开发
    python api.py --port 8510 --prod     # waitress
"""

from __future__ import annotations

import argparse
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

import budget_core as core

app = Flask(__name__)
app.json.ensure_ascii = False
app.json.sort_keys = False

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "dev-worker-token")
WORKER_SECRET = os.getenv("WORKER_SECRET") or os.getenv("SECRET_KEY") or "dev-worker-secret-change-me"

#: 同步模式没有真正的任务队列，这里只留最近若干条结果，供 GET 回放
_RESULTS: "OrderedDict[str, dict]" = OrderedDict()
_RESULT_LIMIT = 200

#: 单机只有一份算力：同一时刻只让一个请求真的去算
_COMPUTE_LOCK = threading.Lock()
BUSY_MESSAGE = "算力正在推演中，请稍后"


class ComputeBusy(RuntimeError):
    """前一个请求还没算完，这一条先不接。"""


# --------------------------------------------------------------------------- #
# 统一信封
# --------------------------------------------------------------------------- #
def ok(data: Any, status: int = 200, **meta):
    body: dict[str, Any] = {"ok": True, "data": data}
    if meta:
        body["meta"] = meta
    # jsonify 内部是 json.dumps，默认 allow_nan=True 会把 inf 写成 `Infinity`，
    # 浏览器 JSON.parse 直接失败 —— 统一在出口处洗一遍（见 core.json_safe）
    return jsonify(core.json_safe(body)), status


def fail(code: str, message: str, status: int = 400, details: Any = None):
    body: dict[str, Any] = {"ok": False, "error": code, "message": message}
    if details:
        body["details"] = details
    return jsonify(core.json_safe(body)), status


def canonical_params(params: dict[str, Any]) -> str:
    """必须与调用方的 ``canonical_params`` 完全一致。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign(*parts: Any) -> str:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(WORKER_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _token_ok() -> bool:
    if not WORKER_TOKEN:  # 留空 = 不校验（只应该出现在本地调试）
        return True
    return hmac.compare_digest(request.headers.get("X-API-Token", ""), WORKER_TOKEN)


def internal_only(view):
    """门禁：只认共享令牌，不认 Cookie。"""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _token_ok():
            return fail("bad_token", "缺少或错误的 X-API-Token", 401)
        return view(*args, **kwargs)

    return wrapped


def _status_signature_ok(job_id: str) -> bool:
    return hmac.compare_digest(request.headers.get("X-API-Signature", ""),
                               sign(f"status|{job_id}"))


# --------------------------------------------------------------------------- #
# 参数收敛
# --------------------------------------------------------------------------- #
def safe_int(params: dict[str, Any], key: str, default: int = 0,
             low: int = 0, high: int | None = None) -> int:
    raw = params.get(key, default)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = default
    value = max(low, value)
    return min(high, value) if high is not None else value


def _counts(params: dict[str, Any]) -> dict[str, int]:
    return {key: safe_int(params, key) for key, _, _ in core.RESOURCES}


def _profile(params: dict[str, Any]) -> str:
    raw = str(params.get("profile") or "new").strip().lower()
    alias = {"new": "new", "新": "new", "新玩家": "new", "新手": "new",
             "veteran": "veteran", "old": "veteran", "老": "veteran",
             "老玩家": "veteran", "资深": "veteran"}
    return alias.get(raw, "new")


def run_budget(params: dict[str, Any], *, wait: bool = False) -> dict[str, Any]:
    """跑一次预算。抢不到锁立刻抛 :class:`ComputeBusy`，由路由层翻译成 503。"""
    if not _COMPUTE_LOCK.acquire(blocking=wait):
        raise ComputeBusy(BUSY_MESSAGE)
    try:
        return core.run(
            _counts(params),
            profile=_profile(params),
            pity=safe_int(params, "pity", 0, 0, core.SIX_HARD_PITY - 1),
            runs=safe_int(params, "runs", core.RUNS, 1, core.RUNS),
            seed=safe_int(params, "seed", 0) or None,
        )
    finally:
        _COMPUTE_LOCK.release()


# --------------------------------------------------------------------------- #
# 路由：探活
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return ok({
        "service": "budget-api",
        "engine": f"budget_core {core.VERSION}",
        "runs": core.RUNS,
        "max_draws": core.MAX_DRAWS,
        "cached": len(_RESULTS),
        "busy": _COMPUTE_LOCK.locked(),
    })


# --------------------------------------------------------------------------- #
# 路由：任务式调用（带签名）
# --------------------------------------------------------------------------- #
@app.post("/internal/jobs/<job_id>")
@internal_only
def create_job(job_id: str):
    """接一条任务，同步算完直接把结果放回 data.result。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return fail("bad_request", "body 必须是 JSON 对象")

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return fail("bad_request", "params 必须是对象")

    expected = sign(job_id, payload.get("tool", ""), payload.get("user_id", 0),
                    payload.get("created_at", 0), canonical_params(params))
    if not hmac.compare_digest(request.headers.get("X-API-Signature", ""), expected):
        return fail("bad_signature", "签名对不上：参数可能被改过，或 WORKER_SECRET 不一致", 403)

    try:
        result = run_budget(params)
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 2})
    except ValueError as exc:
        return fail("invalid_params", str(exc), 422)

    data = {
        "job_id": job_id,
        "status": "succeeded",       # 同步计算：进来就算完了
        "adapter": "budget",
        "adapter_job_id": job_id,
        "progress": {"done": 1, "total": 1},
        "logs": [],
        "result": result,
    }
    _remember(job_id, data)
    return ok(data, 200, adapter="budget", mode="sync")


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
    """计算是秒级的，等走到这里通常已经结束了。"""
    if not _status_signature_ok(job_id):
        return fail("bad_signature", "签名对不上", 403)
    return fail("too_late", "同步计算没有可取消的中间态：结果已经算完了", 409)


def _remember(job_id: str, data: dict[str, Any]) -> None:
    _RESULTS[job_id] = data
    _RESULTS.move_to_end(job_id)
    while len(_RESULTS) > _RESULT_LIMIT:
        _RESULTS.popitem(last=False)


# --------------------------------------------------------------------------- #
# 路由：直接调用
# --------------------------------------------------------------------------- #
@app.get("/api/v1/budget/run")
@internal_only
def direct_get():
    try:
        return ok(run_budget(dict(request.args)))
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 2})
    except ValueError as exc:
        return fail("invalid_params", str(exc), 422)


@app.post("/api/v1/budget/run")
@internal_only
def direct_post():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return fail("bad_request", "body 必须是 JSON 对象")
    try:
        return ok(run_budget(payload))
    except ComputeBusy:
        return fail("busy", BUSY_MESSAGE, 503, {"retry_after": 2})
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
    parser = argparse.ArgumentParser(prog="api.py", description="抽数预算服务 JSON 接口")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（只对本地开放）")
    parser.add_argument("--port", type=int, default=8510,
                        help="监听端口，默认 8510（与其它抽卡服务错开）")
    parser.add_argument("--prod", action="store_true", help="用 waitress（需 pip install waitress）")
    parser.add_argument("--allow-remote", action="store_true",
                        help="允许监听 0.0.0.0 / 内网地址（需要同时设 BUDGET_ALLOW_REMOTE=1）")
    args = parser.parse_args(argv)

    # 结构约束：计算后端**永远只跑在本地回环上**，公网入口交给反向代理。
    # 双钥匙（参数 + 环境变量）是为了挡住「复制一条命令就把接口挂到公网」。
    loopback = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback:
        env_ok = os.getenv("BUDGET_ALLOW_REMOTE") == "1"
        if not (args.allow_remote and env_ok):
            print(f"[x] 拒绝监听 {args.host}：计算后端只允许本地回环地址。", flush=True)
            print(f"    前端一律经反向代理 → 127.0.0.1:{args.port}，不要直接暴露；", flush=True)
            print("    确实要对外（容器 / 内网隔离环境）需同时满足：", flush=True)
            print("      --allow-remote  +  BUDGET_ALLOW_REMOTE=1", flush=True)
            return 2

    print(f"[*] 计算内核 {core.VERSION}，{core.RUNS} 组 / 单组 {core.MAX_DRAWS} 抽上限", flush=True)
    print(f"[*] 监听 http://{args.host}:{args.port}"
          f"{'' if args.host in loopback else '（已显式放开对外监听）'}", flush=True)
    if WORKER_TOKEN == "dev-worker-token":
        print("[!] 正在用默认 WORKER_TOKEN，上线前务必换成随机值", flush=True)

    if args.prod:
        from waitress import serve

        serve(app, host=args.host, port=args.port, threads=4)
        return 0
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

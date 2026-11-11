#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 冒烟自检（不依赖外部服务，直接跑）

    python selftest.py

只验证「接口活着、状态能往返、数字不崩」，不校验概率口径。
规则改动后请另外跑一次蒙特卡洛对账。
"""
import sys

sys.path.insert(0, ".")

import api  # noqa: E402

POOL = "char"
client = api.app.test_client()
failed = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(detail)) if detail else ""))
    if not cond:
        failed.append(name)


h = client.get("/api/health")
check("health 返回 200", h.status_code == 200)
check("service 名", h.get_json()["data"]["service"] == "sim-char-api", h.get_json()["data"]["service"])

m = client.get("/api/v1/sim/meta")
check("meta 返回 200", m.status_code == 200)
check("meta 含本池", POOL in m.get_json()["data"]["pools"])

d = client.post(f"/api/v1/sim/{POOL}/draw", json={"n": 10}).get_json()
check("draw ok", d["ok"])
check("十连 10 条", len(d["data"]["batch"]) == 10, len(d["data"]["batch"]))
state = d["data"]["state"]
check("累计抽数 = 10", state["total"] == 10, state["total"])

d2 = client.post(f"/api/v1/sim/{POOL}/draw", json={"state": state, "n": 5}).get_json()
check("状态回传后续抽 = 15", d2["data"]["state"]["total"] == 15, d2["data"]["state"]["total"])

v = client.post(f"/api/v1/sim/{POOL}/view", json={"state": state}).get_json()
check("view ok", v["ok"])

r = client.post(f"/api/v1/sim/{POOL}/reset", json={}).get_json()
check("reset 归零", r["data"]["state"]["total"] == 0)

mc = client.post(f"/api/v1/sim/{POOL}/mc", json={"runs": 500}).get_json()
check("mc ok", mc["ok"])
check("mc 轮次 = 500", mc["data"]["runs"] == 500)
check("分档合计 ≈ 轮次", abs(sum(t["count"] for t in mc["data"]["tiers"]) - 500) <= 1,
      sum(t["count"] for t in mc["data"]["tiers"]))

over = client.post(f"/api/v1/sim/{POOL}/draw", json={"n": 999}).get_json()["data"]["state"]["total"]
check("超限请求被 clamp 到 200（含赠送十连）", 200 <= over <= 220, over)

print()
print("全部通过 ✅" if not failed else f"失败 {len(failed)} 项 ❌")
sys.exit(1 if failed else 0)

# gacha-sim-weapon-pity · 武器池模拟服务

一个**无状态**的抽卡模拟 HTTP 服务，专门处理「概率恒定 + 循环保底 + 里程碑赠送」这类
武器池：没有软保底，全靠「N 抽必出」和「到点送」推进，所以它的矩阵算法与角色池不同
（按抽推进，而不是一次抽满间隔）。

## 规则

| 项 | 值 | 位置 |
| --- | --- | --- |
| 最高稀有度 | 4%，恒定，**无软保底** | `core.RATE_SIX` |
| 其中 UP 占比 | 25%（其余为其他最高稀有度） | `core.UP_SHARE` |
| 保底 | 40 抽内必出，出即重置、循环 | `core.SIX_PITY` |
| UP 保底 | 80 抽内必得 UP，一次性、不继承 | `core.UP_PITY` |
| 次级概率 | 五星 15% | `core.RATE_FIVE` |
| 里程碑（开箱） | 10 / 26 / 42 / 58 / 74 期 → 随机最高稀有度（非 UP） | `BOX_PERIODS` |
| 里程碑（送 UP） | 18 / 34 / 50 / 66 / 82 期 → 直接送 UP | `UP_PERIODS` |
| 期与抽 | 1 期 = 10 抽 | `DRAWS_PER_PERIOD` |
| 满阶目标 | 6 把 UP（1 本体 + 5 次重复） | `TARGET_UP_WEAPONS` |

里程碑是 16 期一个循环，「开箱」与「送 UP」交替出现。这是本池唯一有状态依赖的地方：
**本期是否已经出过五星以上**会影响下一次里程碑的判定，所以矩阵版必须按抽推进。

## 跑起来

```bash
pip install -r requirements.txt
python api.py --port 8506

curl -s http://127.0.0.1:8506/api/health
# {"ok": true, "data": {"service": "sim-weapon-api", "engine": "core v1.2.0", "pools": ["weapon"]}}
```

> 默认端口 8506 与角色池相同——两个包是独立的，同时部署请改一个（`--port 8507` 或用环境变量
> `GACHA_WEAPON_API_PORT`）。

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 探活 |
| GET | `/api/v1/sim/meta` | 卡池文案、规则、常量、按钮、分档阈值 |
| POST | `/api/v1/sim/weapon/draw` | `{state, n}` → 抽 n 次（n ≤ 200） |
| POST | `/api/v1/sim/weapon/reset` | `{}` → 回到初始态 |
| POST | `/api/v1/sim/weapon/view` | `{state}` → 只算展示模型，不抽 |
| POST | `/api/v1/sim/weapon/mc` | `{state, runs}` → 蒙特卡洛 |

本池没有「保底十连」，所以没有 `free-ten` 接口。

### 一次十连

```bash
curl -s -X POST http://127.0.0.1:8506/api/v1/sim/weapon/draw \
  -H 'Content-Type: application/json' -d '{"n": 10}'
```

```jsonc
{
  "ok": true,
  "data": {
    "batch": [{"name": "武器07", "rarity": 5, "up": false, "free": false}],
    "rows":  [{"抽数": 1, "来源": "常规", "星级": "5 星", "武器": "武器07", "备注": ""}],
    "six_rows": [],
    "state": {"total": 10, "six_pity": 10, "up_pity": 10, "up_active": true, "...": "..."},
    "view": {"pity": [{"label": "六星保底", "value": "4 / 40", "ratio": 0.1},
                      {"label": "UP 保底", "value": "10 / 80", "ratio": 0.125},
                      {"label": "下一个里程碑", "value": "100 抽", "ratio": 0.1}],
             "drops": [], "kpis": [], "donut": null}
  },
  "meta": {"pool": "weapon", "n": 10}
}
```

### 状态字段

| 字段 | 含义 |
| --- | --- |
| `total` | 累计抽数 |
| `six_pity` | 距上次最高稀有度的抽数（40 抽循环） |
| `up_pity` | 距上次 UP 的抽数（80 抽一次性） |
| `up_active` / `up_consumed_at` / `up_reason` | UP 保底是否还有效、何时作废、原因 |
| `count_six` / `count_five` / `count_four` / `count_up` | 各类计数 |
| `count_box` / `count_gift_up` / `count_forced_up` | 开箱、里程碑送 UP、保底强制 UP 的次数 |
| `_box_seen` / `_up_seen` / `_period` / `_period_high` | 里程碑游标与「本期是否出过五星+」 |

带下划线的四个字段**必须一起回传**，否则抽到一半刷新会重复开箱。完整清单见 `api.py` 的 `WPN_STATE`。

### 蒙特卡洛

```bash
curl -s -X POST http://127.0.0.1:8506/api/v1/sim/weapon/mc \
  -H 'Content-Type: application/json' -d '{"runs": 50000}'
```

- `runs` 上限：`X-Client-Tier: member` → 20 万；其余 → 3 万。
- 分档阈值在 `core.MC_TIERS`：≤279 / 280–380 / >380 抽。
- 实测参考（单核）：20 万轮约 1.0–1.2 秒，峰值内存 12 MB —— 是本组里最慢的池子，
  因为按抽推进，`runs` 维向量化要一步步走完 1200 抽。

## 项目结构

```
api.py         HTTP 层：路由、状态搬运、视图组装、令牌与档位
core.py        纯逻辑内核：规则常量 + WeaponCore + 矩阵版蒙特卡洛
mc_matrix.py   蒙特卡洛矩阵引擎（通用，四个模拟包各一份）
common.py      信封 / 令牌 / 参数收敛 / 状态导出与回填
selftest.py    冒烟自检（`python selftest.py`）
```

## 口径说明

概率与里程碑结构按公开信息建模，名单与文案是占位符。模拟结果只反映本文件的模型，
**不代表任何具体产品的真实表现**。

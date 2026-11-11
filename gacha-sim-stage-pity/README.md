# gacha-sim-stage-pity · 三阶段保底池模拟服务

软保底不是一条直线，而是**分三段加速**，最后一段直接抬到硬保底。本包还带「歪了一次，
下次必中」的补偿机制——所以它比普通的软保底池多一个布尔状态。

```
66–70 抽：每抽 +5.6%   →   71–75 抽：28% 起每抽 +11.2%   →   76–79 抽：95.2% 起   →   第 80 抽 100%
```

## 规则

| 项 | 值 | 位置 |
| --- | --- | --- |
| 最高稀有度 | 0.8% | `core.FIVE_BASE`（千分比 8） |
| 软保底起点 | 第 66 抽 | `core.SOFT_START` |
| 三段加速 | 66–70 / 71–75 / 76–79 / 第 80 抽 | `core` 阶段常量 |
| 硬保底 | 第 80 抽 100% | `core.HARD_PITY` |
| 次级概率 | 四星 6%，且 10 抽内必出四星 | `FOUR_BASE` / `FOUR_PITY` |
| 五星池权重 | UP 5 : 其余各 1 | `core.WEIGHTS_FIVE` |
| 歪了补偿 | 下次必出 UP（不跨池继承） | `core` 内 `up_guaranteed` |
| 满阶目标 | 7 个 UP | `core.TARGET_UP` |
| 货币换算 | 1 抽 = 160 抽卡货币 | `core.GEM_PER_DRAW` |

**「歪了下次必中」的实现要点**：`up_guaranteed` 是状态里的一个布尔，出非 UP 的五星时置真，
下次出五星时优先判 UP 并清回假。它必须随 `state` 往返，否则刷新后补偿就丢了。

## 跑起来

```bash
pip install -r requirements.txt
python api.py --port 8508

curl -s http://127.0.0.1:8508/api/health
# {"ok": true, "data": {"service": "sim-stage-api", "engine": "core v1.2.0", "pools": ["stage"]}}
```

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 探活 |
| GET | `/api/v1/sim/meta` | 卡池文案、规则、常量、按钮、分档阈值 |
| POST | `/api/v1/sim/stage/draw` | `{state, n}` → 抽 n 次（n ≤ 200） |
| POST | `/api/v1/sim/stage/reset` | `{}` → 回到初始态 |
| POST | `/api/v1/sim/stage/view` | `{state}` → 只算展示模型，不抽 |
| POST | `/api/v1/sim/stage/mc` | `{state, runs}` → 蒙特卡洛 |

### 一次十连

```bash
curl -s -X POST http://127.0.0.1:8508/api/v1/sim/stage/draw \
  -H 'Content-Type: application/json' -d '{"n": 10}'
```

```jsonc
{
  "ok": true,
  "data": {
    "batch": [{"name": "角色02", "rarity": 4, "up": false}],
    "rows":  [{"抽数": 1, "来源": "常规", "星级": "4 星", "名称": "角色02", "备注": ""}],
    "six_rows": [],
    "state": {"total": 10, "soft_pity": 10, "four_pity": 10,
              "up_guaranteed": false, "four_guaranteed": false, "...": "..."},
    "view": {"pity": [{"label": "满阶", "value": "0 / 7", "ratio": 0.0},
                      {"label": "五星保底", "value": "10 / 80", "ratio": 0.125},
                      {"label": "四星保底", "value": "7 / 10", "ratio": 0.7}],
             "drops": [], "kpis": [], "donut": null, "notes": []}
  },
  "meta": {"pool": "stage", "n": 10}
}
```

注意本池的最高稀有度是五星，所以记录表的列名是「名称」而不是「角色」，`six_rows` 里放的是
五星明细——视图模型会如实反映这一点，前端不需要为池子写特例。

### 状态字段

| 字段 | 含义 |
| --- | --- |
| `total` | 累计抽数 |
| `soft_pity` | 距上次五星的抽数（三阶段软保底用） |
| `four_pity` | 距上次四星的抽数（10 抽保底用） |
| `up_guaranteed` | 下次五星是否必为 UP |
| `four_guaranteed` | 下次四星是否必为 UP（四星档的保底） |
| `count_five` / `count_five_up` / `count_four` / `count_four_up` / `count_three` | 各档计数 |
| `count_big_pity` | 触到硬保底的次数 |
| `five_detail` | 「名字 → 次数」明细 |

完整清单见 `api.py` 的 `STATE`。

### 蒙特卡洛

```bash
curl -s -X POST http://127.0.0.1:8508/api/v1/sim/stage/mc \
  -H 'Content-Type: application/json' -d '{"runs": 100000}'
```

- `runs` 上限：`X-Client-Tier: member` → 20 万；其余 → 3 万。
- 分档五档：≤280 / 281–400 / 401–550 / 551–700 / >700（`core.MC_TIERS`）。
- 实测：20 万轮约 51 ms，峰值 57 MB —— 是本组里最快的，因为每个 UP 只需 1~2 次出货间隔，
  数组规模小。

## 项目结构

```
api.py         HTTP 层：路由、状态搬运、视图组装、令牌与档位
core.py        纯逻辑内核：规则常量 + GachaCore + 矩阵版蒙特卡洛
mc_matrix.py   蒙特卡洛矩阵引擎（通用，四个模拟包各一份）
common.py      信封 / 令牌 / 参数收敛 / 状态导出与回填
selftest.py    冒烟自检（`python selftest.py`）
```

## 口径说明

概率与三阶段曲线按公开信息建模，名单与文案是占位符。模拟结果只反映本文件的模型，
**不代表任何具体产品的真实表现**。

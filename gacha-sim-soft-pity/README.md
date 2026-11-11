# gacha-sim-soft-pity · 软保底池模拟服务

最经典的一类卡池：基础概率固定，**连续不出货就逐抽加息**，到硬保底抽数必定出货；
出货后计数归零，重新开始。本包还带「双 UP 分摊」与「满额赠送」。

```
2% 恒定 → 第 51 抽起每抽 +2% → 第 99 抽 100%
```

## 规则

| 项 | 值 | 位置 |
| --- | --- | --- |
| 最高稀有度 | 2% | `core.SIX_BASE` |
| 软保底起点 | 连续 50 抽未出 → 第 51 抽 4% | `core.SIX_SOFT_START` |
| 每抽递增 | +2% | `core.SIX_STEP` |
| 硬保底 | 第 99 抽 100% | `core.SIX_HARD_PITY` |
| 双 UP | 合计占最高稀有度 70%（主 35% + 副 35%） | `core.UP_SHARE` |
| 次级分配 | 非最高稀有度时按 8 : 50 : 40 分给五星 / 四星 / 三星 | `FIVE_SHARE` 等 |
| 累计赠送 | 满 300 抽额外赠 1 个主 UP，仅一次 | `core.WELL_AT` |
| 满阶目标 | 6 个主 UP | `TARGET_MAIN_UP` |
| 货币换算 | 1 抽 = 600 抽卡货币 | `GEM_PER_DRAW` |
| 十连保底 | 开关 `TEN_PITY_5STAR`，默认关闭 | `core.TEN_PITY_5STAR` |

概率曲线可以自己验：`core.six_rate(pity)` 返回「距上次最高稀有度第 `pity` 抽」的单抽概率，
和 `GachaCore.step` 完全一致，蒙特卡洛矩阵版也用它。

## 跑起来

```bash
pip install -r requirements.txt
python api.py --port 8507

curl -s http://127.0.0.1:8507/api/health
# {"ok": true, "data": {"service": "sim-soft-api", "engine": "core v1.2.0", "pools": ["soft"]}}
```

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 探活 |
| GET | `/api/v1/sim/meta` | 卡池文案、规则、常量、按钮、分档阈值 |
| POST | `/api/v1/sim/soft/draw` | `{state, n}` → 抽 n 次（n ≤ 200） |
| POST | `/api/v1/sim/soft/reset` | `{}` → 回到初始态 |
| POST | `/api/v1/sim/soft/view` | `{state}` → 只算展示模型，不抽 |
| POST | `/api/v1/sim/soft/mc` | `{state, runs}` → 蒙特卡洛 |

### 一次十连

```bash
curl -s -X POST http://127.0.0.1:8507/api/v1/sim/soft/draw \
  -H 'Content-Type: application/json' -d '{"n": 10}'
```

```jsonc
{
  "ok": true,
  "data": {
    "batch": [{"name": "角色04", "rarity": 4, "up": false}],
    "rows":  [{"抽数": 1, "来源": "常规", "星级": "4 星", "角色": "角色04", "备注": ""}],
    "six_rows": [],
    "state": {"total": 10, "pity": 10, "since_five_plus": 10, "well_used": false, "...": "..."},
    "view": {"pity": [{"label": "集齐主 UP", "value": "0 / 6", "ratio": 0.0},
                      {"label": "六星保底", "value": "10 / 99", "ratio": 0.101},
                      {"label": "300 抽赠送", "value": "10 / 300", "ratio": 0.033}],
             "drops": [], "kpis": [], "donut": null}
  },
  "meta": {"pool": "soft", "n": 10}
}
```

### 状态字段

| 字段 | 含义 |
| --- | --- |
| `total` | 累计抽数（兼作「井」的进度） |
| `pity` | 距上次最高稀有度的抽数（决定当前单抽概率） |
| `since_five_plus` | 距上次五星及以上的抽数 |
| `well_used` | 300 抽赠送是否已用掉（`WELL_AT`） |
| `count_six` / `count_five` / `count_four` / `count_three` | 各稀有度计数 |
| `count_main_up` / `count_gift_up` | 抽到的主 UP 数、赠送拿到的个数 |
| `six_detail` | 「名字 → 次数」的明细，用来算出货一览里的 UP / 其他 |

完整清单见 `api.py` 的 `STATE`。

### 蒙特卡洛

```bash
curl -s -X POST http://127.0.0.1:8507/api/v1/sim/soft/mc \
  -H 'Content-Type: application/json' -d '{"runs": 100000}'
```

- `runs` 上限：`X-Client-Tier: member` → 20 万；其余 → 3 万。
- 分档：<250 极欧 / 250–400 正常 / 401–550 偏非 / >550 极非（`core.MC_TIERS`）。
- 实测：20 万轮约 430 ms，峰值 68 MB —— 内存最大的一档，因为 `K = 2 × target` 的
  间隔表要一次抽满。

## 项目结构

```
api.py         HTTP 层：路由、状态搬运、视图组装、令牌与档位
core.py        纯逻辑内核：规则常量 + GachaCore + 矩阵版蒙特卡洛
mc_matrix.py   蒙特卡洛矩阵引擎（通用，四个模拟包各一份）
common.py      信封 / 令牌 / 参数收敛 / 状态导出与回填
selftest.py    冒烟自检（`python selftest.py`）
```

## 口径说明

概率与赠送结构按公开信息建模，名单与文案是占位符（`角色01`、`示例卡池`）。
模拟结果只反映本文件的模型，**不代表任何具体产品的真实表现**。

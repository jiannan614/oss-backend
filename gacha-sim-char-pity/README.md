# gacha-sim-char-pity · 角色池模拟服务

一个**无状态**的抽卡模拟 HTTP 服务：输入状态 → 抽 n 次 → 返回本次结果、新状态，
以及一份「该怎么画」的视图模型。适合做单池、带「N 抽内必得 UP」大保底的角色池演示。

```
基础概率 → 软保底逐抽加息 → 硬保底 → 大保底兜住 UP → 累计赠送兜住重复
```

## 规则

| 项 | 值 | 位置 |
| --- | --- | --- |
| 最高稀有度（六星） | 0.8% | `core.SIX_BASE_RATE` |
| 五星 | 8% | `core.FIVE_BASE_RATE` |
| 软保底 | 第 66 抽起，每抽 +5% | `SIX_SOFT_PITY` / `SIX_RATE_STEP` |
| 硬保底 | 第 80 抽必出 | `SIX_HARD_PITY` |
| 十连保底 | 连续 10 抽无五星 → 强制五星 | `FIVE_PITY_MAX` |
| 大保底 | 前 120 抽内必得当期 UP，同池仅一次、不继承 | `BIG_PITY_MAX` / `BIG_PITY_ONCE` |
| 累计赠送 | 每满 240 抽赠 1 个兑换券，不占抽数、不动计数器 | `GIFT_INTERVAL` |
| 保底十连 | 累计 30 抽触发一次免费十连；独立计算、不吃软保底、必出五星+ | `FREE_TEN_TRIGGER` / `FREE_TEN_SIZE` |
| 满阶目标 | 6 个 UP（1 本体 + 5 兑换券） | `TARGET_UP_UNITS` |
| 货币换算 | 1 抽 = 500 抽卡货币 | `GEM_PER_DRAW` |

**大保底的两个分支**（细节最容易写错的地方，`core.GachaCore.step` 里逐行对应）：

- 120 抽内提前抽到 UP → 大保底**消耗掉、立即失效**，本期不再保；
- 到第 120 抽还没出 → **该抽强制 UP**，同时小保底清零。

## 跑起来

```bash
pip install -r requirements.txt
python api.py --port 8506            # 开发
python api.py --port 8506 --prod     # waitress
```

```bash
curl -s http://127.0.0.1:8506/api/health
# {"ok": true, "data": {"service": "sim-char-api", "engine": "core v1.2.0", "pools": ["char"]}}
```

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 探活 |
| GET | `/api/v1/sim/meta` | 卡池文案、规则、常量、按钮、分档阈值 |
| POST | `/api/v1/sim/char/draw` | `{state, n}` → 抽 n 次（n ≤ 200） |
| POST | `/api/v1/sim/char/free-ten` | `{state}` → 领一次保底十连 |
| POST | `/api/v1/sim/char/reset` | `{}` → 回到初始态 |
| POST | `/api/v1/sim/char/view` | `{state}` → 只算展示模型，不抽 |
| POST | `/api/v1/sim/char/mc` | `{state, runs}` → 蒙特卡洛 |

`n` 会被夹到 `[1, 200]`；`state` 缺失时按「从零开始」处理。

### 一次十连

```bash
curl -s -X POST http://127.0.0.1:8506/api/v1/sim/char/draw \
  -H 'Content-Type: application/json' -d '{"n": 10}'
```

```jsonc
{
  "ok": true,
  "data": {
    "batch": [{"name": "角色03", "rarity": 4, "up": false, "free": false}],
    "rows":  [{"抽数": 1, "来源": "常规", "星级": "4 星", "角色": "角色03", "备注": ""}],
    "six_rows": [],
    "state": {"total": 10, "small_pity": 10, "five_pity": 10, "up_pity": 10, "...": "..."},
    "view": {
      "pity": [{"label": "集齐 UP", "value": "0 / 6", "ratio": 0.0},
               {"label": "小保底", "value": "10 / 80", "ratio": 0.125},
               {"label": "大保底", "value": "10 / 120", "ratio": 0.083},
               {"label": "240 抽赠兑换券", "value": "× 0 · 10 / 240", "ratio": 0.042}],
      "drops": [], "kpis": [], "donut": null,
      "free_ten": {"trigger": 30, "pending": false}, "pending": false
    }
  },
  "meta": {"pool": "char", "n": 10}
}
```

- `batch` 是本次抽到的全部结果（前端做逐格揭示动画用）；
- `rows` 是给记录表用的行，**键序就是表头顺序**（服务端关掉了 JSON 键排序）；
- `six_rows` 只放最高稀有度的明细；
- `view` 是展示模型，前端不该自己算保底进度。字段固定为 `pity`（保底进度条）、
  `drops`（出货一览）、`kpis`（关键指标）、`donut`（环形图分档）、`free_ten` / `pending`
  （保底十连状态）。

### 状态字段

| 字段 | 含义 |
| --- | --- |
| `total` | 累计抽数（含免费十连） |
| `small_pity` | 距上次六星的抽数（软/硬保底用） |
| `five_pity` | 距上次五星的抽数（十连保底用） |
| `up_pity` | 距上次 UP 的抽数（大保底用） |
| `big_pity_active` / `big_pity_consumed_at` / `big_pity_reason` | 大保底是否还有效、何时失效、失效原因 |
| `gift_step` | 累计赠送走到第几档 |
| `count_six` / `count_five` / `count_four` / `count_up` / `count_gift` / `count_big_pity` | 各类计数 |
| `free_ten_used` / `free_ten_pending` | 本次保底十连领没领、是否挂起 |
| `free_count_*` | 免费十连内部的独立计数 |

完整清单见 `api.py` 的 `CHAR_STATE`——**只搬这些字段**，其余（随机数生成器、派生属性）
不参与往返。

### 蒙特卡洛

```bash
curl -s -X POST http://127.0.0.1:8506/api/v1/sim/char/mc \
  -H 'Content-Type: application/json' -d '{"runs": 100000}'
```

```jsonc
{"ok": true, "data": {
  "runs": 100000, "target": 6,
  "min": 118, "max": 1187, "mean": 455.2, "median": 468,
  "p10": 312, "p25": 378, "p75": 486, "p90": 596, "per_up": 75.9,
  "elapsed_ms": 230.5, "draws_per_sec": 434000,
  "gems": 227600.0, "limit": 30000,
  "tiers": [{"label": "欧", "lo": null, "hi": 349, "count": 17400, "pct": 0.174}]
}}
```

- `runs` 上限：`X-Client-Tier: member` → 20 万；其余（含不传）→ 3 万。
- 统计口径：中位数与分位用部分排序，**不插值**（取 `sorted[int(n*p)]`），与逐抽实现一致。
- 分档阈值在 `core.MC_TIERS`，改档位只改这一处；颜色由前端映射。
- 矩阵版与逐抽版必须保持一致：改了 `core.py` 的规则，两边都要跑一遍对账。

## 项目结构

```
api.py         HTTP 层：路由、状态搬运、视图组装、令牌与档位
core.py        纯逻辑内核：规则常量 + GachaCore + 矩阵版蒙特卡洛
mc_matrix.py   蒙特卡洛矩阵引擎（通用，四个模拟包各一份，内容相同）
common.py      信封 / 令牌 / 参数收敛 / 状态导出与回填
selftest.py    （可选）自检
```

## 口径说明

规则按公开概率公示建模；名单与文案全是占位符。模拟结果只反映本文件的模型，
**不代表任何具体产品的真实表现**，也不构成消费建议。

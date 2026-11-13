# gacha-budget-mc · 抽数预算的蒙特卡洛服务

> 位于 [jiannan614/oss-backend](https://github.com/jiannan614/oss-backend) 的 `gacha-budget-mc/` 目录，可单独拷出去用。

一个**纯计算**的 JSON 服务：把「手上这些资源」折算成抽数，再按卡池机制跑 5000 组模拟，
最后回答「这批抽大概能拿到什么、折成多少副产物」。
**核心只用标准库**；装了 NumPy 会自动走矩阵引擎（快 8~20 倍），没装也能跑。

它和另外几个包的分工：

| 包 | 回答什么 |
| --- | --- |
| `gacha-planner` | 资源能抽多少、攒到目标日要多久（算期望，不抽样） |
| `gacha-sim-*` | 一次抽卡的结果与状态推进（给界面用） |
| **`gacha-budget-mc`** | **把「这一批抽」整体跑很多遍，给出收益区间与副产物** |

## 两种调用方式

**A. 直接调用（同步）** —— 适合前端、curl、脚本：

```
GET|POST /api/v1/budget/run
```

**B. 任务式调用（带签名）** —— 适合把计算放到独立机器 / 独立进程，由网关调度：

```
POST /internal/jobs/<job_id>
```

请求头要带 `X-API-Token` 与 `X-API-Signature`。签名算法：

```python
payload = f"{job_id}|{tool}|{user_id}|{created_at}|{canonical_params(params)}"
signature = hmac_sha256(WORKER_SECRET, payload).hex()

# canonical_params：键排序、紧凑分隔符、不转义中文
json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

`GET /internal/jobs/<id>` 用 `status|<job_id>` 签名回放结果（同步模式只保留最近 200 条）；
`POST /internal/jobs/<id>/cancel` 恒回 `409 too_late`——秒级计算没有可取消的中间态。

## 跑起来

```bash
pip install -r requirements.txt      # 只有 flask 与 waitress
python api.py --port 8510            # 开发
python api.py --port 8510 --prod     # waitress

curl -s http://127.0.0.1:8510/api/health
# {"ok": true, "data": {"service": "budget-api", "engine": "budget_core v1.0.0", ...}}
```

```bash
# 15 万抽卡货币 = 300 抽，新玩家口径
curl -s -X POST http://127.0.0.1:8510/api/v1/budget/run \
  -H "X-API-Token: dev-worker-token" -H 'Content-Type: application/json' \
  -d '{"orundum": 150000, "profile": "new"}' | head -c 600
```

> 直接调用也必须带令牌，除非 `WORKER_TOKEN` 留空。令牌默认值是 `dev-worker-token`，
> 上线前务必改成随机串，并让 `WORKER_SECRET` 与调用方一致。

不想起服务也可以直接跑内核：

```bash
python budget_core.py --orundum 150000
python budget_core.py --orundum 150000 --profile veteran --pity 20
python budget_core.py --json --orundum 150000
```

## 参数

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `orundum` | 抽卡货币数量 | 0 |
| `originite` | 高级货币数量（1 颗 = 75 抽卡货币） | 0 |
| `tickets` | 单抽券（1 张 = 1 抽） | 0 |
| `ten` | 十连券（1 张 = 10 抽） | 0 |
| `profile` | 玩家档位：`new` / `veteran` / `whale` | `new` |
| `pity` | 当前垫抽数（距上次 6★，0~79） | 0 |
| `runs` | 模拟组数 | 5000（上限也是 5000） |
| `seed` | 随机种子，传了就能复现 | 无 |

`runs` 之所以固定：这个参数对使用者没有决策价值，调大调小只会让同一次查询给出不同数字。
5000 组下「平均每个 6★」已经稳定到小数点后一位；耗时见下。

## 响应长什么样

返回的是**视图模型**，不是裸数据——前端拿到就能排版：

```jsonc
{"ok": true, "data": {
  "kpis": [
    {"label": "折算抽数", "value": "300 抽", "hint": "150,000 抽卡货币 ÷ 500"},
    {"label": "平均 6★", "value": "5.4 个", "hint": "每 55.4 抽一个"},
    {"label": "运气指数", "value": "97", "hint": "100 = 长期平均"},
    {"label": "保底可折", "value": "20 抽", "hint": "保底配额 503 ÷ 25"}
  ],
  "donut": {"caption": "档位概率 · 5,000 组", "total": 5000,
            "items": [{"label": "正常", "pulls": 3160, "percent": 63.2, "color": "#EF9F27"}]},
  "tables": [{"title": "收益明细", "columns": ["指标", "Max", "平均", "Min"],
              "rows": [["6★", "10", "5.4", "4"], ["保底配额", "720（可折 29 抽）", "..."…]],
              "collapsed": true}],
  "notes": ["理想情况会落在平均值附近：约 5.4 个 6★、42.9 个 5★、261.7 个 4★…………"],
  "convert": {"total": 150000, "draws": 300.0, "full_draws": 300, "remainder": 0},
  "summary": {"mean": {}, "max": {}, "min": {}, "tiers": [], "percentiles": {}},
  "engine": "budget_core v1.0.0", "elapsed_ms": 1400
}}
```

- `tables[].collapsed = true` 表示这张表明细建议默认折叠（很多前端会渲染成 `<details>`）。
- `notes` 只有一条：一段人话（均值 → 浮动范围 → 保底下限 → 免责）。

## 机制与口径

规则写在 `budget_core.py` 顶部，改常量即可：

| 项 | 现值 |
| --- | --- |
| 6★ | 基础 0.8%；第 66 抽起每抽 +5%；第 80 抽必出 |
| 5★ | 8%；连续 10 抽没有 5★ 则强制一个 |
| 大保底 | 前 120 抽内必得 UP，同卡池一次，消耗即失效 |
| 赠送 | 每累计 240 抽赠 1 个 UP 凭据；累计 30 抽触发一次免费十连 |
| 副产物 | 装备配额 4★20 / 5★200 / 6★2000；保底配额 第 2 份起每份 5★10 / 6★50；满阶溢出 4★5 / 5★20 记通用配额、6★10 记高级配额 |
| 兑换 | 25 保底配额 = 1 抽（响应里直接给「可折抽数」） |
| 档位 | 按每组「抽数 ÷ 6★ 数」：<50 欧 / 50~67 正常 / ≥67 非酋 |

- **满阶** = 同一个角色 6 份（1 本体 + 5 凭据）。
- **玩家档位**影响保底 / 通用 / 高级三项：新玩家全部从 0 份起算；
  老玩家 4★ / 5★ 视为已满阶、6★ 不含本体；
  `whale`（满图鉴）的 7 位非 UP 6★ 也已满阶，抽到任何一份都直接进高级配额，
  当期 UP 仍从 0 份起算（`PROFILES[*].six_owned` 是长 8 的元组）。
- **运气指数** = 长期平均间隔（53.9 抽）÷ 这组实际间隔 × 100，100 = 平均水平。
- 单组只抽「折算出来的完整抽数」就停，不追求满阶；余下的货币不参与模拟。
- 240 抽赠送的凭据：计入 6★ 数与「其中 UP」，也计入高级配额的溢出份数；
  但不给装备 / 保底 / 通用配额（保底只按抽到的份数判「第 2 份」）。

## 性能

两套引擎，规则完全一致，只是推进方式不同（5000 组实测）：

| 引擎 | 300 抽 | 600 抽 | 1200 抽 | 3000 抽 |
| --- | --- | --- | --- | --- |
| `python-loop`（逐抽） | 1.32s | 2.76s | 5.90s | 14.77s |
| `numpy-matrix`（矩阵） | 0.16s | 0.15s | 0.30s | 0.71s |

`--engine auto`（默认）有 NumPy 就用矩阵，没有就退回逐抽；`matrix` / `loop` 强制指定。
矩阵版把 5000 组摊平成数组，循环只走抽数这一维：状态是向量、出货只记索引对、
每 64 抽用 `bincount` 落成份数矩阵、配额整块算。随机数流不同（`default_rng` 批量取
vs `Random` 逐个取），**逐组结果不可能相同**，只保证分布一致 ——
`selftest.py` 里有矩阵 / 逐抽的均值对账断言。

单机只有一份算力，进程内有一把互斥锁：同一时刻只让一个请求真的算，其余立刻收到
`503 busy` + `retry_after`。要接更多并发就起多个进程，在网关层排队。

## 项目结构

```
budget_core.py    纯逻辑内核：折算 / 逐抽模拟 / 副产物账本 / 视图模型 / 命令行
mc_matrix.py      可选的 NumPy 矩阵引擎（同一套规则，快 8~20 倍）
api.py            HTTP 层：路由、信封、令牌与签名、算力闸
selftest.py       完整自检（纯断言，不需要起服务）
```

```bash
python selftest.py                    # 全绿才算改对了
python budget_core.py --selftest      # 同上（内核自带的入口）
python budget_core.py --orundum 150000 --engine loop   # 强制逐抽版（对账用）
```

## 数据与合规

- 概率、保底、赠送都写在 `budget_core.py` 顶部，来源是公开的概率公示与社区整理；
  规则是机制描述，不涉及任何厂商的素材或名单。
- 角色名一律是占位（`六星01`、`五星03`、`四星05`），四种配额的名字（装备 / 保底 / 通用 / 高级）
  也是中性占位，按你自己的产品叫法改常量与文案即可。
- 结果是概率上的平均推算，**不代表任何具体产品的真实表现**，也不构成消费建议。

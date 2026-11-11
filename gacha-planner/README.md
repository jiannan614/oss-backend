# gacha-planner · 抽数规划与概率计算服务

一个**纯计算**的 JSON 服务：把「手上有什么资源」折算成「还能抽多少」，把「想抽到多少」
折算成「还要攒多久」，再把概率分布摊开给你看。**不依赖 NumPy，只用标准库 + Flask。**

它回答四类问题：

1. **现在能抽多少** —— 抽卡货币 / 高级货币 / 单抽券 / 十连券 / 兑换凭证 → 总抽数
2. **攒到目标日够不够** —— 日收入（日常、周常、月卡、签到、活动）逐日进账，二分出「还差几天」
3. **这个池子有多大概率抽到** —— N 抽内至少出 k 个最高稀有度的概率与期望
4. **兑换凭证怎么换最划算** —— 阶梯档位（10 / 18 / 40 / 70 / 120 …），本月能换多少、要几个月

## 两种调用方式

**A. 直接调用（同步）** —— 适合前端、curl、脚本：

```
GET|POST /api/v1/gacha/<mode>
```

`mode` 五个：`count`（资源折算）、`calc`（攒抽预测）、`rate`（爆率曲线）、
`yellow`（凭证阶梯）、`event`（活动窗口）。

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
`POST /internal/jobs/<id>/cancel` 恒回 `409 too_late`——毫秒级计算没有可取消的中间态。

## 跑起来

```bash
pip install -r requirements.txt      # 只有 flask 与 waitress
python api.py --port 8502            # 开发
python api.py --port 8502 --prod     # waitress

curl -s http://127.0.0.1:8502/api/health
# {"ok": true, "data": {"service": "planner-api", "engine": "planner_core v3.1.1", ...}}
```

```bash
# 资源折算：8.4 万抽卡货币 + 12 高级货币 + 5 张单抽券 + 258 兑换凭证
curl -s -X POST http://127.0.0.1:8502/api/v1/gacha/count \
  -H "X-API-Token: dev-worker-token" -H 'Content-Type: application/json' \
  -d '{"orundum": 84000, "originite": 12, "tickets": 5, "yellow": 258}'
```

> 直接调用也必须带令牌，除非 `WORKER_TOKEN` 留空。令牌默认值是 `dev-worker-token`，
> 上线前务必改成随机串。

## 参数

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| `orundum` | 抽卡货币数量 | 0 |
| `originite` | 高级货币数量（可折算成抽卡货币） | 0 |
| `tickets` | 单抽券 | 0 |
| `ten` | 十连券 | 0 |
| `yellow` | 兑换凭证数量 | 0 |
| `yellow_stage` | 兑换凭证已领到第几档 | 10 |
| `keep` | 预留不折算的高级货币（想买外观时用） | 0 |
| `pity` | 当前垫抽数（距上次最高稀有度） | 0 |
| `use_originite` | 是否允许把高级货币折算成抽数 | true |
| `craft_jade` | 是否开启「自造货币」（每月额外产出） | false |
| `monthly_card` | 是否买月卡 | false |
| `profile` | 玩家档位，影响一次性收入与复刻补偿 | 见 `planner_core` |
| `extra` | 额外一次性收入（抽） | 0 |

`count` 只用资源类参数，`calc` 还会用到收入类参数，`rate` 用 `pity` 与目标数量。

## 响应长什么样

返回的是**视图模型**，不是裸数据——前端拿到就能排版：

```jsonc
{"ok": true, "data": {
  "mode": "count",
  "kpis":  [{"label": "总计", "value": "512 抽"}],
  "tables": [{"columns": ["档位", "可换", "兑换点数消耗"],
              "rows": [["300 兑换点数 · 当期限定 6★", "1 个", "300"]]}],
  "cards": [{"title": "100 抽", "stats": [{"label": "抽卡货币", "unit": "货币", "value": "37,300"}]}],
  "donut": {"caption": "现有资源", "items": [], "total": 512, "unit": "抽"},
  "notes": ["..."],
  "elapsed_ms": 3, "engine": "planner_core v3.1.1"
}}
```

同一份参数在 `count` / `calc` / `rate` 下的 `kpis` 结构一致，所以一个前端可以复用渲染。

## 单机算力只有一份

计算是 CPU 密集的（攒抽预测要一天一天扫到目标日，最长 3650 天），所以进程内有一把互斥锁：
同一时刻只让一个请求真的算，其余立刻收到 `503 busy` + `retry_after`。这是刻意的取舍——
排队等待会让所有请求一起变慢，快速失败 + 客户端隔一两秒重试更好。

要接更多并发，起多个进程并在网关层做队列，不要试图去掉这把锁。

## 项目结构

```
api.py            HTTP 层：路由、信封、令牌与签名、视图组装、算力闸
planner_core.py   纯逻辑内核：资源折算 / 概率分布 / 攒抽预测 / 活动窗口
selftest.py       完整自检（纯断言，不需要起服务）
```

```bash
python selftest.py      # 全绿才算改对了
```

自检覆盖：资源折算、阶梯兑换逐档、跨月/跨年边界、春节与周年窗口、概率单调性、
分位数口径、参数反向传参等。**改 `planner_core.py` 的任何常量之后都要跑一遍。**

## 口径说明

规则与收入项按公开信息建模，货币与凭证的具体名称在代码里是中性占位。
计算结果是估算，**不代表任何具体产品的真实收益**，也不构成消费建议。

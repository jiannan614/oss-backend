# 抽卡后端 · 六个可独立部署的 JSON 服务

> 仓库：<https://github.com/jiannan614/oss-backend>

一组「只算数、不画界面」的抽卡机制后端：输入一个卡池状态，输出这一抽的结果、
新的状态，以及一份描述「该怎么展示」的视图模型。**没有任何游戏素材、角色名单或前端代码。**

六个目录互相独立，可以只拿走你要的那一个——每个目录都是完整可跑的包：

| 包 | 机制 | 默认端口 | 依赖 |
| --- | --- | --- | --- |
| [`gacha-planner`](gacha-planner/) | 资源折算、攒抽预测、爆率曲线、阶梯兑换 | 8502 | 仅标准库 + Flask |
| [`gacha-sim-char-pity`](gacha-sim-char-pity/) | 软保底 + 硬保底 + 「N 抽内必得 UP」大保底 + 累计赠送 | 8506 | + NumPy |
| [`gacha-sim-weapon-pity`](gacha-sim-weapon-pity/) | 恒定概率 + 循环保底 + 里程碑赠送 | 8506 | + NumPy |
| [`gacha-sim-soft-pity`](gacha-sim-soft-pity/) | 软保底逐抽加息 + 双 UP 分摊 + 满额赠送 | 8507 | + NumPy |
| [`gacha-sim-stage-pity`](gacha-sim-stage-pity/) | 三段式软保底 + 歪了下次必中 | 8508 | + NumPy |
| [`gacha-budget-mc`](gacha-budget/) | 资源折算 + 整批蒙特卡洛 + 抽卡副产物 | 8510 | 仅标准库 + Flask |

## 共同约定

这六个服务遵守同一套约定，所以一个前端可以同时接它们。

**1. 统一信封。** 成功 `{"ok": true, "data": {...}}`，失败 `{"ok": false, "error": "code", "message": "..."}`。
前端只判 `ok`，不用猜 HTTP 状态码的含义（状态码仍然是对的，只是不承担语义）。

**2. 服务端无状态。** 抽卡进度（累计抽数、保底计数、已出货明细）由**客户端**保存，
每次请求带上来，算完还回去：

```jsonc
POST /api/v1/sim/char/draw
{"state": {...}, "n": 10}
→ {"ok": true, "data": {"batch": [...], "rows": [...], "state": {...}, "view": {...}}}
```

好处是进程随时可以重启、多线程不用加锁、同一份请求可以重放复现。
代价是客户端必须原样回传 `state`——漏字段就会被当成「从头开始」。

**3. 令牌是可选的。** 配了 `WORKER_TOKEN` 就必须带 `X-API-Token`；不配即放行（方便本机裸跑）。
正式部署请务必配上，并把服务绑在 `127.0.0.1` 后面。

**4. 档位只影响蒙特卡洛上限。** 四个模拟服务读 `X-Client-Tier: member|guest`，
分别把单次蒙特卡洛封顶在 20 万 / 3 万轮。这个头应当由**你自己的网关**按登录态注入——
不要让浏览器直传，否则等于没有限制。

**5. 展示模型由后端给。** 响应里的 `view` 描述「保底进度几段、指标几行、出货几格、环形图分档」，
前端只负责排版和动画。这样换一套界面不用改后端，改一条规则也不用改前端。

## 快速开始

任选一个包：

```bash
cd gacha-sim-char-pity
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python api.py --port 8506                       # 开发模式（Flask 自带服务器）
python api.py --port 8506 --prod                # waitress（需已安装）
```

```bash
curl -s http://127.0.0.1:8506/api/health
curl -s http://127.0.0.1:8506/api/v1/sim/meta | head -c 400

# 抽十连：第一次不带 state，服务端用默认值（即从零开始）
curl -s -X POST http://127.0.0.1:8506/api/v1/sim/char/draw \
  -H 'Content-Type: application/json' -d '{"n": 10}'
```

`gacha-planner` 的接口是 `/api/v1/gacha/<mode>`，`gacha-budget-mc` 是
`/api/v1/budget/run`，用法见各自的 README。

## 包之间是什么关系

```
gacha-planner          独立：纯标准库，连 NumPy 都不需要
gacha-budget        独立：纯标准库 + Flask（逐抽模拟，不用矩阵引擎）
gacha-sim-char-pity    api.py ── core.py ── mc_matrix.py
gacha-sim-weapon-pity  api.py ── core.py ── mc_matrix.py
gacha-sim-soft-pity    api.py ── core.py ── mc_matrix.py
gacha-sim-stage-pity   api.py ── core.py ── mc_matrix.py
```

- 每个包**自包含**：`api.py` 只管 HTTP 与状态搬运，`core.py` 是纯逻辑（无 IO），
  `mc_matrix.py` 是蒙特卡洛矩阵引擎，`common.py` 是信封 / 令牌 / 参数收敛。
- 四个模拟包各带一份 `mc_matrix.py`（内容相同）。这是刻意的取舍：包可以单独下载、
  单独部署，不依赖兄弟目录。**如果你同时维护多份，改一处要同步其余几份。**
- 服务之间**不通信**，也没有数据库。想串起来由你的网关或前端决定。

## 部署建议

- **只绑回环**：`python api.py --port 8506`（默认 `127.0.0.1`），前面放 Nginx / Caddy 反代。
  `gacha-planner` 要监听非回环地址时必须显式加 `--allow-remote` **且**设
  `GACHA_ALLOW_REMOTE=1`，防止手滑把计算接口挂到公网。
- **配上令牌**，并在网关上封掉 `/internal/*`（只有服务间调用需要它）。
- **并发**：单核跑 20 万轮蒙特卡洛约 0.1~1.2 秒、峰值内存 12~68 MB（按池子不同）。
  一个 4 核机器同时接 4 个请求是安全的；再多请自行压测。
  `gacha-budget-mc` 固定 5000 组 × 单组抽数，300 抽约 1.4 秒、1200 抽约 5.7 秒。
- **别把 `runs` 交给用户随便填**：服务端有 `MAX_RUNS_GUEST/MEMBER` 兜底，
  但真正的额度记账（日 / 月）应该在网关侧做，这里只管单次上限。

## 数据与合规

- 所有**概率、保底、赠送**规则都写在各自 `core.py` 顶部的常量里，来源是公开的概率公示与
  社区整理。规则是机制描述，不是任何厂商的机密。
- 所有**名单与文案都是占位符**（`角色01`、`武器03`、`示例卡池`），仓库里不含任何真实
  角色名、卡池名、立绘或图标。
- 数值仅用于机制演示与程序练手，**不代表任何具体产品的实际表现**，也不构成任何消费建议。

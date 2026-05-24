# Omni-ICT Algo Trading Bot 项目总结分析

生成日期：2026-05-20  
分析范围：当前工作区源码与配置，重点覆盖 `python/`、`mql5/`、`server.py`、`rules.json`、部署与运维文档。

## 1. 项目定位

该项目是一个围绕 ICT / SMC 交易方法构建的自动化交易系统，主要目标是：

- 从 MetaTrader 5 导出多周期行情、账户、持仓和历史成交数据。
- 用 Python 侧策略引擎扫描 ICT/SMC 结构，生成交易信号。
- 将信号输出给 MT5 图表覆盖层、TradingView Pine 脚本、Web Dashboard，以及自动/半自动执行模块。
- 默认以 paper mode 运行，只有显式设置 `OMNI_PAPER_MODE=false` 或配置 `paper_mode=false` 后才进入真实下单。

项目主交易品种偏向贵金属，特别是 `XAUUSD`、`XAGUSD`，同时 watchlist 中也包含部分外汇对和指数。

## 2. 总体架构

当前代码库存在两套相关但侧重点不同的运行路径。

### 2.1 传统主循环路径

传统路径以 `python/auto_trader.py` 为核心：

```text
MT5 OmniExport_v4.mq5
  -> omni_data.json
  -> python/auto_trader.py
      -> ict_precision.py 扫描 ICT setup
      -> dual_tf_selector.py 合并双周期信号
      -> 风控、仓位计算、订单生成
      -> omni_cmd.txt
  -> MT5 EA 读取命令并执行
```

`auto_trader.py` 是功能最重的文件，包含数据读取、策略扫描调用、风险模式、频率模式、仓位计算、订单命令写入、持仓管理、移动止损、分批止盈、复利风险调整、Telegram 通知和交易记忆调用。

### 2.2 当前 autonomy / swarm 路径

`python/watchdog.py` 当前默认启动的是：

```text
watchdog.py
  -> server.py
  -> swarm.py
  -> orchestrator.py --loop 60
  -> telegram_bot.py 或 omni_bridge.py
```

其中 `swarm.py` 会启动多个异步 agent：

- `SignalAgent`：读取 `shared/signals.json`，将可交易信号路由到风控。
- `RiskAgent`：做资金、回撤、RR、持仓数量等风控检查。
- `ExecutionAgent`：执行通过风控的信号，调用 `auto_trader` 中的下单函数。
- `JournalAgent`：记录交易开平仓与表现。
- `LearningAgent`：根据交易结果调整信号偏好。
- `RegimeAgent`：检测市场状态。
- `MonitorAgent`：监控服务和信号新鲜度。
- `AnalystAgent`：尝试用 LLM/规则分析表现并提出参数调整。
- `TrailingManager`：独立管理持仓追踪止损与部分平仓。

agent 之间通过 `shared/agent_tasks.json`、`shared/agent_events.json`、`shared/agent_state.json` 进行文件式任务队列和事件通信。

需要注意：`agent_bus.py` 使用了 `fcntl` 文件锁，这是类 Unix/macOS 方案；在 Windows 原生 Python 中可能不可用。项目文档本身也主要按 macOS/Wine/launchd 部署编写。

## 3. 数据流与文件接口

### 3.1 MT5 数据导出

`mql5/OmniExport_v4.mq5` 是主数据桥。它会周期性写出 `omni_data.json`，内容包括：

- 账户信息：余额、净值、保证金、杠杆、浮盈亏。
- 当前持仓：ticket、symbol、方向、volume、开仓价、SL、TP、盈亏、magic。
- 交易历史：近 90 天 deal 数据。
- 多周期 K 线：W1、D1、H4、H1、M15、M5、M1。
- 当前报价、spread、tick_size、tick_value、contract_size、lot step 等交易规格。
- 关键流动性参考：PDH/PDL、PWH/PWL、PMH/PML、NDOG/NWOG gap。
- 简化版趋势、RSI、OB、FVG、结构字段。

EA 写文件时使用临时文件再替换，降低 Python 读取半写入 JSON 的概率。

### 3.2 Python 数据读取

主要读取层有：

- `python/config.py`：集中解析 `.env`、`config.json` 和默认路径。
- `python/mt5_connector.py`：读取 MT5 JSON，做 symbol resolve、broker 时间偏移修正、K 线转换。
- `python/data_router.py`：在 MT5 数据和 TradingView webhook 数据之间选择最新数据源，优先 MT5。

### 3.3 信号输出

`python/orchestrator.py` 每轮对 watchlist 扫描后写：

- `shared/signals.json`：标准信号数组，供 MT5 overlay、agent swarm、dashboard 使用。
- `pine/omni_pine_overlay.pine`：TradingView Pine 覆盖脚本。

信号结构来自 `python/signal_writers.py`，核心字段包括：

- `symbol`
- `timeframe`
- `direction`: `BULL` / `BEAR` / `NEUTRAL`
- `entry_type`: `ob_mitigation` / `fvg_fill` / `sweep_choch` / `none`
- `entry_price`
- `sl`
- `tp`
- `confidence`
- `reasons`
- `scale_action`
- `metadata`

`mql5/OmniSignalOverlay.mq5` 负责在 MT5 图表读取 `signals.json` 并画 Entry、SL、TP 线。

## 4. 核心交易技术

项目采用的交易方法以 ICT / SMC 为核心，辅以传统技术形态、市场状态识别和在线学习。

### 4.1 多周期框架

`rules.json` 中当前双周期配置为：

- macro timeframe：`H4`
- HTF timeframe：`H1`
- LTF timeframe：`M5`
- 传统 `ict_precision.py` 扫描还会使用 W1、D1、H4、H1、M15、M5、M1。

策略意图是：

```text
W1 / D1：宏观方向与区间位置
H4：主结构、关键 OB/FVG/流动性
H1：执行结构、setup zone
M15 / M5：MSS、FVG、扫流动性后的精确触发
M1：局部确认或更细执行
```

`dual_tf_selector.py` 的逻辑更简洁：

1. HTF 根据最新 BOS/CHoCH 或 swing trend 判断方向。
2. LTF 寻找同方向 OB mitigation、FVG fill、sweep + CHoCH。
3. 只有方向一致且 confidence 达标，才生成 actionable signal。

### 4.2 SMC 结构识别

`python/smc_engine.py` 是较纯净的 SMC 检测模块，负责：

- fractal swing high / low
- BOS / CHoCH
- Order Block
- Fair Value Gap
- Liquidity Pool
- Liquidity Sweep
- ATR

该模块无 MT5 I/O，便于单元测试和被 orchestrator 组合调用。

OB 检测逻辑：寻找强 displacement 前的最后一根反向 candle，且只有价格用收盘价穿透 OB body 后才标记 mitigated。  
FVG 检测逻辑：三根 K 线不平衡，且小于 ATR 分数阈值的 gap 会被过滤。  
Sweep 检测逻辑：价格刺破等高/等低流动性池并收回。

### 4.3 ICT 精细扫描

`python/ict_precision.py` 是更完整的 ICT 策略扫描器，覆盖内容更多：

- D1 bias、H4 structure。
- 等高/等低流动性。
- 高低点 sweep、Turtle Soup、Judas Swing。
- bullish / bearish OB。
- bullish / bearish FVG，以及 FVG Consequent Encroachment。
- Breaker Block。
- Power of 3：Asia accumulation、London manipulation、NY distribution。
- AMD phase。
- Quarter Theory。
- OTE 50%、62%、79% 精确入场。
- Kill Zone 与 Silver Bullet 时段。
- SMT divergence。
- 技术形态：双顶/双底、头肩、楔形、旗形、三角形、矩形、吞没、pin bar、doji 等。
- push / exhaustion 动能状态。
- 支撑阻力与 pivot levels。

该文件会把候选机会封装成 `ICTSetup`，字段包含 symbol、direction、entry_type、entry、SL、TP1/TP2/TP3、confidence、RR、session、AMD phase、grade、confluence 等。

### 4.4 AMD 与 Power of 3

项目内有两个层次的 AMD 逻辑：

- `ict_precision.py`：根据时间段和 Asia range / London sweep / NY move 判断 Power of 3。
- `amd_engine.py`：更结构化的 AMD 检测器，输出 `ACCUMULATION`、`MANIPULATION`、`DISTRIBUTION`、`CONTINUATION` 等上下文，并在 orchestrator 中附加到信号 metadata。

当前规则倾向：

- Asia/Accumulation：主要识别区间，非 sweep setup 会被阻断，除非显式启用 scalp。
- London/Manipulation：等待 Asia high/low 被扫。
- NY/Distribution：倾向顺着 manipulation 后的真实方向推进，并允许 scale-in。

### 4.5 入场模型

主要入场模型包括：

- OB mitigation：价格回到未失效 OB，优先使用 OB body midpoint，也就是 OTE 50%。
- FVG fill：价格回补 imbalance，优先 CE 或当前价格，且会被 sweep gate 约束。
- Sweep + CHoCH：扫流动性后结构转向，是高权重触发。
- Breaker Block retest：原 OB 被突破后角色反转。
- Silver Bullet：特定时间窗口内 OB + FVG + sweep 聚合。
- CISD / Inverted FVG：价格强收盘穿过相反 FVG，作为状态交付变化。
- Accumulation scalp：需要手动文件开关启用，用于 Asia 阶段高频小目标交易。

### 4.6 出场与持仓管理

`auto_trader.py` 和 `position_trailing_manager.py` 实现多层出场：

- TP1：通常 1.5R，平 50%。
- TP2：通常 2.5R，平 30%。
- TP3：通常 4R 或更远流动性，剩余 20% runner。
- 到 1R 后移动止损到 breakeven 或带 buffer。
- 到 2R 后锁定至少 1R 左右收益。
- 到更高 R 倍后进一步收紧。
- 出现 LTF/HTF 反向结构时可减仓或关闭。
- `smart_trailing_stop.py` 用 ATR、结构低/高、流动性避让、profit lock ladder 生成更精细的 SL proposal。

## 5. 风控体系

项目风控分为配置级、交易前、交易中、系统级四层。

### 5.1 风险模式

`auto_trader.py` 内置三个 RiskProfile：

- `LOW`：0.5% base risk，日亏 2%，峰值回撤 5%。
- `MODERATE`：1.0% base risk，日亏 3%，峰值回撤 10%，默认。
- `HIGH`：2.0% base risk，日亏 5%，峰值回撤 15%。

同时有 FrequencyProfile：

- `CONSERVATIVE`：只做 A+/A，confidence 更高，最多 2 单。
- `NORMAL`：默认，A+/A/B+，最多 3 单。
- `AGGRESSIVE`：机会更多，最多 5 单，但噪声风险更高。

### 5.2 仓位计算

仓位计算公式：

```text
risk_amount = equity * risk_pct
risk_per_lot = abs(entry - sl) / tick_size * tick_value
lot_size = risk_amount / risk_per_lot
```

随后会根据 `min_lot`、`max_lot`、`lot_step`、账户净值分层 lot ceiling 做裁剪，避免小账户因为极短 SL 得到异常大仓位。

### 5.3 交易前过滤

主要过滤项：

- confidence 未达当前 session / symbol / learned threshold。
- Asia accumulation 阶段阻断非 sweep entry。
- 逆 D1 bias 且 confidence < 72 阻断。
- 周五 15:00 UTC 后阻断新单。
- 同一 symbol 当日连续亏损达到阈值后暂停。
- SL 过近阻断。
- spread 超过 symbol 类别阈值阻断。
- RR 低于最小值阻断。
- 最大持仓数限制。
- USD 相关敞口集中度限制。

### 5.4 系统级保护

- `python/HALT`：kill switch。
- `TRADING_DISABLED`：Telegram 或外部命令禁用交易。
- `check_daily_limits()`：日亏和峰值回撤停止交易。
- `recovery_protocol`：回撤暂停后满足恢复条件再恢复。
- `watchdog.py`：服务异常退出后自动重启，超出最大次数后停机并写 alert。
- `mt5_max_stale_sec` / `MAX_DATA_AGE_SECS`：MT5 数据过旧时暂停交易。

## 6. AI / 学习模块

项目内有多种“学习”或“记忆”机制：

- `trade_memory.py`：记录 entry type、session、AMD phase、pattern、quarter、sweep 等维度的表现，样本数足够后对 confidence 做加减。
- `feature_store.py`、`online_learner.py`、`parameter_optimizer.py`：记录特征并尝试生成 learned parameters。
- `pattern_recognition_model.py`：为 setup 增加小幅胜率预测调整。
- `LearningAgent`：根据交易关闭事件给 SignalAgent 反馈 symbol confidence delta。
- `AnalystAgent`：读取 journal、rules、recent signals，生成参数调整建议，并带边界限制。

这些模块属于锦上添花层，不是最底层入场信号的必要依赖；多数导入都做了 graceful degrade。

## 7. Dashboard 与控制面

`server.py` 是 FastAPI 服务，提供：

- `GET /`：Web dashboard。
- `GET /tg`：Telegram mini app 页面。
- `GET /smart-trail`：smart trailing 页面。
- `GET /api/accounts`
- `GET /api/data/{account_id}`
- `GET /api/rules`
- `GET /api/smart_trail/{account_id}`
- `GET /api/status`
- `POST /api/control/halt`
- `POST /api/control/resume`
- `POST /api/control/trading/{state}`
- `POST /api/control/setrisk/{mode}`
- `POST /api/control/setfreq/{mode}`
- `POST /api/control/close/{account_id}/{ticket}`
- `WebSocket /ws/{account_id}`

前端主要是 `webapp/index.html` 和 `webapp/omni_ict_trading_dashboard.jsx`，另有 Telegram 和 smart trail 单页。

## 8. 配置重点

### 8.1 `python/rules.json`

这是策略大脑，关键区块包括：

- `watchlist`
- `dual_tf`
- `amd`
- `scaling`
- `smart_trail`
- `risk_rules`
- `filter_rules`
- `confidence_thresholds`
- `symbol_overrides`
- `session_rules`
- `kill_zone_rules`
- `sweep_gate_rules`
- `ai_learning_rules`

当前重要默认值：

- `dual_tf.enabled = true`
- `dual_tf.tp_rr = 2.5`
- `scaling.enabled = true`
- `smart_trail.enabled = true`
- `risk_rules.base_risk_pct = 1.0`
- `risk_rules.max_open_positions = 3`
- `risk_rules.min_rr_ratio = 2.0`
- `symbol_overrides.XAGUSD.min_confidence = 75`

### 8.2 `config.json` / `.env`

`config.example.json` 支持多账户，每个账户配置 data path、cmd path、state path、log path、memory path、journal path。  
`python/config.py` 的优先级为：

```text
环境变量 > config.json > 自动探测路径 > 硬编码默认值
```

真实交易开关是：

```text
OMNI_PAPER_MODE=false
```

同时 MT5 EA 端还需要 `AutoTradeEnabled=true` 才会真正处理命令。

## 9. 部署与运维

项目主要按 macOS + Wine / MetaTrader 5 + launchd 设计。

常用命令：

```bash
python python/watchdog.py --status
python python/watchdog.py --stop
python python/orchestrator.py --dry-run --symbols XAUUSD
python server.py
```

日志位置：

- `logs/server.log`
- `logs/orchestrator.log`
- `logs/swarm.log` 或 watchdog 子进程日志
- `logs/watchdog_state.json`
- `python/trader.log`
- `shared/signals.json`
- `shared/swarm_state.json`
- `shared/agent_state.json`

## 10. 当前源码观察到的问题与风险

1. 文档与源码存在偏差。README 中提到的 `advanced_risk_manager.py`、`ict_engine.py` 当前不在工作树内；实际风险和策略扫描分散在 `auto_trader.py`、`ict_precision.py`、`smc_engine.py`、`dual_tf_selector.py`。

2. `watchdog.py` 的注释与 `OMNI_AUTONOMY.md` 有历史差异。当前 watchdog 默认不直接启动 `auto_trader.py`，而是启动 `swarm.py`，再由 agent 协作执行。

3. 跨平台风险明显。`agent_bus.py` 依赖 `fcntl`，在 Windows 环境可能失败；而当前 workspace 在 Windows 路径下。如果目标是在 Windows 原生运行，需要替换文件锁实现或只使用非 swarm 路径。

4. `auto_trader.py` 过大，职责过多。它同时承担配置、风控、执行、状态、通知、学习 hook、持仓管理。长期维护建议拆分成 execution、risk、position management、notification、state store。

5. 策略复杂度高，过拟合风险高。ICT、形态、AI 调整、session、AMD、SMT、pivot、push/exhaustion、symbol override 叠加后，真实行为不容易解释。实盘前必须用稳定的 paper-forward 记录验证。

6. 信号生成链路有两套入口：`ict_precision.py` 和 `orchestrator.py + smc_engine.py + dual_tf_selector.py`。二者可能产生不同信号口径，需要明确哪条是生产权威。

7. 文件 IPC 简单可靠，但并发一致性有限。MT5 数据与 signals 用 atomic replace 较好；agent bus 的任务队列在 macOS 下有锁，但 Windows 下风险较高。

8. 执行安全依赖双开关：Python paper mode 与 MT5 EA `AutoTradeEnabled`。这是安全的，但也容易让用户误判为什么信号出现而订单不执行。

## 11. 建议的学习顺序

如果要继续维护或改造，建议按下面顺序阅读：

1. `python/rules.json`：先理解策略开关和参数。
2. `mql5/OmniExport_v4.mq5`：理解 MT5 输出的数据形态。
3. `python/config.py`、`python/mt5_connector.py`：理解路径与数据读取。
4. `python/smc_engine.py`：理解基础 SMC 检测。
5. `python/dual_tf_selector.py`：理解简化信号生成。
6. `python/orchestrator.py`、`python/signal_writers.py`：理解 signal 输出。
7. `python/auto_trader.py`：理解完整执行与风控。
8. `python/swarm.py`、`python/agents/*.py`：理解多 agent 生产路径。
9. `server.py`：理解 dashboard 和控制面。

## 12. 结论

这是一个功能覆盖非常广的 ICT/SMC 自动交易系统，已经具备数据桥、策略扫描、信号标准化、图表覆盖、自动执行、风险控制、持仓管理、Dashboard、Telegram、agent swarm、学习反馈和运维监督等完整组件。

它的优势是功能完整、交易理念覆盖细、保护机制多、默认 paper mode 较安全。主要短板是系统复杂度偏高、历史文档与当前代码不完全同步、Windows 原生运行兼容性不足，以及核心执行文件职责过于集中。

若用于真实资金，建议先明确生产链路采用 `auto_trader` 单体模式还是 `orchestrator + swarm` 模式，然后固定一套信号口径，在 paper mode 下至少跑完整交易周，重点验证：信号新鲜度、实际 spread、SL/TP 距离、订单类型、分批止盈、移动止损、最大回撤和暂停/恢复机制。

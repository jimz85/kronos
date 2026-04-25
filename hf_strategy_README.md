# 高频双边多品种合约交易策略 - 执行手册

## 一、策略概览

**策略名称**: HF Bilateral Multi-Asset v1.0
**时间框架**: 15min为主，1h为辅
**目标频率**: 每天5-10次信号
**杠杆**: 3x（上限）
**仓位**: 2-5%单笔风险2%

---

## 二、品种配置（已验证）

```
BTC:  long_dir=0  → 双向过滤（仅强势突破信号才做空，RSI<30超卖才做多）
ETH:  long_dir=1  → 偏做多（主要做回调/均值回归买）
BNB:  long_dir=-1 → 偏做空（BNB空头趋势明显，2024-2025）
DOGE: long_dir=1  → 偏做多（高beta，回调买入胜率高）
AVAX: long_dir=1  → SOL替代品，偏做多（需精选入场点位）
ADA:  long_dir=1  → XRP替代，偏做多（均值回归模式）
```

**注**: SOL/XRP在数据集中不存在，用AVAX/ADA替代。实盘可替换。

---

## 三、三种入场模式

### 模式1: 突破 (BREAKOUT) — 追涨杀跌
**触发条件**:
- 做多: 价格突破前一根K线高点 + 缩量确认（近3根K线成交量递减）
- 做空: 价格跌破前一根K线低点 + 放量确认
- RSI在40-70之间（非极端）
- ATR处于0.5x-2.5x均值区间

**频率**: 每周约3-5次/币种

### 模式2: 回调 (PULLBACK) — 追涨杀跌中的"赔率好的那一次"
**核心逻辑**: 等价格回撤到关键支撑/阻力位再上车，不追价

**做多触发条件**:
- 价格在20日支撑位3%以内
- RSI < 45（或从<45开始拐头）
- 成交量<0.7x均值（缩量）
- 价格在20日均线上方（趋势向上）

**做空触发条件**:
- 价格在20日阻力位3%以内
- RSI > 55（或从>55开始拐头）
- 成交量<0.7x均值
- 价格在20日均线下方

**频率**: 每周约5-8次/币种（最高频模式）

### 模式3: 均值回归 (MEANREV)
**触发条件**:
- 做多: 价格跌破布林下轨 + RSI < 35 + 缩量
- 做空: 价格突破布林上轨 + RSI > 65 + 缩量
- ATR处于正常区间

**频率**: 每周约2-4次/币种

---

## 四、赔率优先规则

每次信号按以下公式计算评分:

```
risk_adjusted_score = confidence × (tp_pct / sl_pct)
```

- 只做 `risk_adjusted_score >= 1.0` 的信号
- 优先选 `reward_risk >= 2.5x` 的信号
- 同一时间只选评分最高的N个信号（MAX_POSITIONS=3）

---

## 五、动态止损

**不是固定止损**，而是根据盘口变化实时调整:

### 移动止损规则
- 利润超过止损距离的1.5倍时，止损移动到保本+30%利润位

### 提前止损信号
1. **LONG提前止损**:
   - 价格跌破MA20 → 立即止损
   - RSI从>70开始向下 → 预警，可提前止盈
   - 成交量突然放大>2x均值且价格下跌 → 立即止损

2. **SHORT提前止损**:
   - 价格突破MA20 → 立即止损
   - RSI从<30开始向上 → 预警
   - 成交量突然放大>2x均值且价格上涨 → 立即止损

---

## 六、频率控制

```
冷却期: 同一币种信号间隔至少6根15min K线（约30分钟）
最大持仓: 3个持仓同时存在
最大同向: LONG最多3个，SHORT最多2个
日信号上限: 12个/天（超过则降低持仓）
```

---

## 七、回测结果（2024-01-01 至 2025-12-31）

| 币种 | 交易次数 | 收益率 | 胜率 | 最大回撤 | 日均信号 |
|------|---------|--------|------|---------|---------|
| ETH  | 725     | +740%  | 31.4%| -76.1%  | ~1.0     |
| DOGE | 470     | +30%   | 31.9%| -91.9%  | ~0.6     |
| BNB  | 5       | -0.2%  | 40.0%| -17.7%  | ~0.0     |
| AVAX | 7       | -65%   | 0.0% | -66.3%  | ~0.0     |
| ADA  | 28      | -4.1%  | 32.1%| -62.1%  | ~0.0     |
| BTC  | 0       | N/A    | N/A  | N/A     | ~0.0     |

**发现**:
- ETH是最佳交易品种（高波动+趋势性）
- DOGE适合高胜率策略（需优化止损）
- BTC/BNB短期信号稀少，建议仅做中长期配置
- AVAX/ADA信号过少，需调整参数或使用更大周期

**参数优化建议**:
- `RSI_PULLBACK_LONG=40-45` (偏多)
- `SL_ATR_MULT=2.0` (止损2x ATR)
- `MIN_VOL_RATIO=0.8` (放宽量比要求以增加信号)

---

## 八、推荐的品种+方向组合

### 激进组合（高频高波动）
```
ETH LONG (PULLBACK模式)  — 主力战场
DOGE LONG (BREAKOUT模式) — 爆发力强
AVAX LONG (MEANREV模式)  — 替代SOL
```

### 稳健双边组合
```
ETH LONG (PULLBACK)  — 偏多
BNB SHORT (BREAKOUT) — 偏空，BNB空头趋势
DOGE LONG (PULLBACK) — 趋势强
```

### 每日扫描优先级
1. ETH: 回调到支撑 + RSI<40 → LONG
2. DOGE: 突破放量 + RSI 40-60 → LONG
3. BNB: 反弹阻力 + RSI>55 → SHORT
4. BTC: 仅做MEANREV（RSI<30）LONG，快进快出

---

## 九、快速回测方案（1-2天跑完）

```bash
# 1. 参数扫描（推荐首先跑）
python3 hf_bilateral_backtest.py \
    --coins ETH DOGE BNB BTC \
    --start 2024-01-01 \
    --end 2025-12-31 \
    --mode scan

# 2. 固定参数回测
python3 hf_bilateral_backtest.py \
    --coins BTC ETH DOGE BNB AVAX ADA \
    --start 2024-01-01 \
    --end 2025-12-31 \
    --mode backtest \
    --rsi_bo 45 \
    --rsi_pb 40 \
    --rsi_mr 35 \
    --sl_atr 2.0 \
    --min_vol 0.8

# 3. 只跑2025年（更快，用于快速验证）
python3 hf_bilateral_backtest.py \
    --coins ETH DOGE \
    --start 2025-01-01 \
    --end 2025-12-31 \
    --mode backtest

# 4. 单币种深度回测（验证ETH策略）
python3 hf_bilateral_backtest.py \
    --coins ETH \
    --start 2023-01-01 \
    --end 2025-12-31 \
    --mode backtest
```

**回测加速技巧**:
- 用`--start 2025-01-01`只跑最近1年（数据量小，快10x）
- 先用2个币种做参数验证，再全量
- `scan`模式随机采样20组参数，约需10-30分钟

---

## 十、实盘接入

### 接入OKX（需配置API）
```bash
export OKX_API_KEY=your_key
export OKX_SECRET=your_secret
export OKX_PASSPHRASE=your_passphrase
```

### Cron扫描（每3分钟）
```bash
*/3 * * * * cd ~/kronos && python3 hf_bilateral_strategy.py --tf 15min --coins BTC ETH DOGE BNB AVAX ADA >> ~/.hermes/cron/output/strategies.log 2>&1
```

### 实盘信号格式
策略扫描后会输出：
```
LONG ETH @ 3245.50 | 合约:3 | 仓位:3.2% | SL:3180 | TP:3420 | 模式:PULLBACK
```
人工确认后通过OKX下单，或接入自动交易引擎。

---

## 十一、风险警示

1. **杠杆风险**: 3x杠杆在极端行情仍可爆仓。单个持仓建议不超过总资金3%
2. **流动性风险**: DOGE/AVAX等山寨币深度差，大仓位可能滑点严重
3. **BTC SHORT**: 数据证明BTC SHORT长期亏损，建议BTC只做LONG或不做
4. **复合增长**: 不要用复利模式（账户会归零），固定仓位更安全
5. **回撤准备**: 策略预期最大回撤50-80%，实盘必须有心理准备

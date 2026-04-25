# 链上数据研究文档

## 价格数据路径关闭声明

**日期**：2026-04-14
**结论**：经过《策略实盘资格认证考试》六项测试及两项结构性优化重考，基于公开价格数据（OHLCV）的 BTC 趋势策略被正式判定为**不具备实盘资格**。

**核心证据**：
- Walk-Forward 测试集年化收益从训练集的 51.7% 衰减至 8.0%，市场结构变迁导致 Alpha 不可持续
- 实盘摩擦成本将微薄利润侵蚀至盈亏边缘
- 失败模式存在可预测的系统性聚集（特定年份、低波动环境）

**路径状态**：**永久关闭**。不再对任何基于 OHLCV 的新指标组合进行回测研究。

---

## 下一阶段目标

获取链上估值数据（MVRV Z-Score、SOPR、交易所余额），用同一套考试系统验证链上过滤器能否解决收益衰减和失败聚集问题。

## 链上因子挖掘框架（参考 crypto-kol-quant）

**参考来源**：https://github.com/0xquqi/crypto-kol-quant

crypto-kol-quant 的研究范式：
```
非结构化信息 → LLM蒸馏 → 470条能力 → 87个因子 → IC回测 → 共识输出
```

我们的链上研究复刻：
```
链上原始数据 → 假设库 → 因子 → live_ready_exam验证 → 策略结论
```

### 项目结构

```
~/kronos/onchain_research/          ← 已建立
├── capabilities/                   ← 假设因子库（CAP_REGISTRY模式）
│   ├── __init__.py                ← 注册中心
│   ├── mvrv.py                    ← 6个MVRV假设
│   ├── sopr.py                    ← 4个SOPR假设
│   └── exchange_flows.py          ← 3个交易所资金流假设
├── feature_engine.py              ← 特征工程
├── backtest.py                   ← IC回测（已验证可运行）
└── data/                         ← 数据目录
```

### 当前状态

- 13个假设因子已注册并通过IC回测验证
- 框架可运行（模拟数据测试通过）
- 等待接入真实链上数据（Glassnode API）

### 运行方式

```bash
cd ~/kronos/onchain_research
python3 backtest.py              # IC回测（已有13个因子）
python3 feature_engine.py         # 构建特征面板
```

### 添加新假设

在 `capabilities/` 下新建文件，参考 `mvrv.py` 的 `@register` 装饰器模式。

### 与现有工具的关系

- backtest.py：IC粗筛（快速验证假设方向）
- live_ready_exam.py：六项严格考试（策略准入标准）
- 流程：IC筛选 → 有潜力因子 → live_ready_exam → PASS/FAIL

---

## 待验证假设

1. **主假设**：当 BTC MVRV Z-Score < 1.5 时，周线趋势策略的测试集年化收益能否提升至 15% 以上？
2. **辅助假设**：交易所 BTC 余额持续下降期间，趋势策略的失败率是否显著降低？

## 数据源待办

- [ ] BGeometrics 免费 API（监控其是否开放 MVRV 历史数据）
- [ ] Glassnode 付费方案价格调研
- [ ] Coin Metrics 社区版 API 申请

---

## 链上策略考试通过标准（草案）

在获得链上数据后，新策略需通过同一套六项考试，总分 ≥ 70 且无单题零分。
具体改进目标：

| 指标 | 当前价格策略 | 目标链上增强 |
|------|------------|-------------|
| 测试集年化 | 8.0% | ≥ 15% |
| 第五大题聚集 | 2个维度聚集 | 无明显聚集 |
| 最大回撤 | -13.5% | ≤ -20% |

---

## MVRV Z-Score 详解

### 什么是 MVRV？

MVRV（Market Value to Realized Value）是比特币市值与实现市值的比率。

- **市值（Market Value）**：流通中所有比特币的当前价格总和
  `市值 = 比特币价格 × 流通量`

- **实现市值（Realized Value）**：每个 UTXO 的持有成本总和
  `实现市值 = Σ(每个UTXO的持有数量 × 创建该UTXO时的价格)`
  - 已知的 Coinbase 输出的 UTXO 按 0 成本计算
  - 丢失币的 UTXO 也按 0 成本计算（这部分难以精确识别）

- **MVRV 比率** = 市值 / 实现市值
  - MVRV > 1：当前价格高于平均持有成本，市场整体盈利
  - MVRV < 1：当前价格低于平均持有成本，市场整体亏损

### MVRV Z-Score

MVRV 比率本身的波动范围很大，直接使用不够直观。MVRV Z-Score 将其标准化：

```
MVRV Z-Score = (MVRV比率 - MVRV均值) / MVRV标准差
```

含义：
- Z-Score > 3：市场极度高估，历史对应顶部区域
- Z-Score < 0：市场低估，历史对应底部区域
- Z-Score 在 0~1 之间：相对均衡

### 关键阈值（经验值）

| Z-Score 区间 | 市场状态 | 策略含义 |
|------------|---------|---------|
| > 3.5 | 极度高估 | 禁止开仓，持仓应止盈 |
| 1.5 ~ 3.5 | 正常偏高 | 可持仓，减少新开仓 |
| 0 ~ 1.5 | 相对均衡 | 允许开仓 |
| < 0 | 低估 | 积极寻找买入机会 |

### 为什么 MVRV 能预测市场？

1. **均值回归**：实现市值代表市场整体的"真实成本"。当价格远离成本时，市场参与者集体非理性，最终价格会向成本回归。
2. **资金效率**：MVRV 高时，说明资金涌入投机，现有持币者平均持有时间缩短，市场微观结构趋近顶部。
3. **链上行为**：持有者成本分布在 MVRV 低时（大量持币者亏损）提供支撑，在 MVRV 高时（持币者大量获利）提供抛压。

### 数据来源

- Glassnode（付费）：直接提供 MVRV、Z-Score、SOPR 等指标
- 自建需求：需要完整的 UTXO 集快照（每个 UTXO 的创建时间+金额）和历史价格数据

---

## 验证框架准备（模拟测试）

目的：确保一旦获得真实数据，5 分钟内出结果。

### 模拟 MVRV 生成逻辑

使用价格数据模拟一个近似的 MVRV 指标（验证框架用）：

```python
def simulate_mvrv(btc_prices, window_short=30, window_long=365):
    """
    用移动平均比值模拟 MVRV 方向
    短期MA/长期MA的比值可以近似捕捉市场热度
    """
    short_ma = btc_prices.rolling(window_short).mean()
    long_ma = btc_prices.rolling(window_long).mean()
    simulated_mvrv = short_ma / long_ma
    return simulated_mvrv

# Z-Score 化
mvrv_mean = simulated_mvrv.rolling(365).mean()
mvrv_std = simulated_mvrv.rolling(365).std()
mvrv_z = (simulated_mvrv - mvrv_mean) / mvrv_std
```

### 验证框架叠加逻辑

```python
# MVRV + EMA30 叠加策略
FILTER_THRESHOLD = 1.5  # 只在 Z-Score < 1.5 时允许开仓

for bar in weekly_data:
    mvrv_signal = mvrv_z[bar.date]
    
    if not in_position and price > ema30 and mvrv_signal < FILTER_THRESHOLD:
        signal = "BUY"  # 趋势确认 + 估值低
    elif in_position and (price < ema30 or mvrv_signal > 3.5):
        signal = "SELL"  # 趋势破坏 或 极度高估
```

### 待完成

- [ ] 当 Glassnode API 可用时，验证模拟结果与真实 MVRV 的相关性
- [ ] 校准 Z-Score 阈值

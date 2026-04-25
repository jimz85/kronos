# 市场状态分类器方案设计（Regime Detection）
> **Kronos Engine_Beta 趋势跟踪引擎前置模块**
> 版本: v1.0 | 日期: 2026-04-25

---

## 一、核心问题定义

### 1.1 任务目标
在 **5分钟(5m)** 和 **1小时(1h)** 级别判定市场状态：
- **TREND**: 明确单边趋势（上涨/下跌）
- **RANGE**: 无序震荡（无明显方向）

### 1.2 关键挑战
| 问题 | 描述 | 影响 |
|------|------|------|
| **假突破** | 震荡中的短暂突破被误判为趋势启动 | 趋势策略在震荡中频繁止损 |
| **滞后性** | 分类器确认趋势时，趋势已走完30-50% | 买入时已无足够利润空间 |
| **周期敏感性** | 同一指标在5m和1h上的表现差异大 | 需要周期适配参数 |

### 1.3 输出目标
```python
# 期望的信号格式
{
    "regime": "TREND_UP" | "TREND_DOWN" | "RANGE",
    "confidence": 0.0-1.0,
    "regime_score": -100 to +100,  # +100=强多, -100=强空, 0=中线
    "trend_strength": 0.0-1.0,     # 趋势强度
    "volatility_regime": "HIGH" | "NORMAL" | "LOW",
    "breakout_confirmed": bool,    # 突破是否经过验证
    "timestamp": "...",
    "source": "regime_classifier"
}
```

---

## 二、方案对比总表

| 维度 | ① ADX+DMI | ② Volatility Cone | ③ MA密集度 | ④ 趋势动量综合 |
|------|-----------|-------------------|------------|----------------|
| **原理** | 方向性指数 | 历史波动率分位数 | 均线收敛程度 | 多指标共振 |
| **计算复杂度** | 低 | 中 | 低 | 中 |
| **5m适用性** | ⭐⭐⭐ 一般 | ⭐⭐ 差 | ⭐⭐⭐ 一般 | ⭐⭐⭐⭐ 优 |
| **1h适用性** | ⭐⭐⭐⭐ 优 | ⭐⭐⭐⭐ 优 | ⭐⭐⭐⭐ 优 | ⭐⭐⭐⭐ 优 |
| **假突破过滤** | 中等 | 强 | 弱 | 强 |
| **滞后性** | 中等(3-5根K线) | 低(基于分位数) | 低(即时) | 低(多指标确认) |
| **趋势强度量化** | ✅ ADX值 | ✅ 分位数 | ⚠️ 间接 | ✅ 多维度 |
| **参数数量** | 2个(ADX周期,+DI/-DI阈值) | 3个(分位数,回溯周期) | 2个(均线数量,收敛阈值) | 5个 |
| **对噪音的鲁棒性** | 一般 | 好 | 差 | 好 |

---

## 三、方案详解

### 方案①：ADX + DMI（经典趋势方案）

#### 3.1.1 原理
- **ADX (Average Directional Index)**: 衡量趋势强度，值越大趋势越强（不论方向）
- **DMI (Directional Movement Index)**: +DI和-DI的差值确定方向
- **核心逻辑**: ADX > 阈值 且 +DI > -DI → 趋势确认

#### 3.1.2 计算公式

```
TR = max(H - L, |H - PrevClose|, |L - PrevClose|)
+DM = H - PrevH (if H-PrevH > PrevL-L and H-PrevH > 0)
-DM = PrevL - L (if PrevL-L > H-PrevH and PrevL-L > 0)

ATR = SMA(TR, period=14)
+DI = SMA(+DM, period=14) / ATR * 100
-DI = SMA(-DM, period=14) / ATR * 100
DX = |+DI - -DI| / (+DI + -DI) * 100
ADX = SMA(DX, period=14)

# 趋势方向
DI_Spread = +DI - (-DI)  # 正=多头, 负=空头
```

#### 3.1.3 阈值选择

| 参数 | 5m推荐值 | 1h推荐值 | 依据 |
|------|---------|---------|------|
| ADX阈值 | **23** | **25** | ADX>20表明趋势开始,>25确认趋势 |
| +DI > -DI 阈值 | **5** | **5** | 确保方向明确,避免在转折点误判 |
| 趋势确认需ADX连续 | **3根K线** | **2根K线** | 减少噪音,5m噪音多需更多确认 |

#### 3.1.4 状态判断逻辑

```python
if ADX > 25 and DI_Spread > 5:
    regime = "TREND_UP"
    confidence = min(ADX / 40, 1.0)  # ADX 40=100%置信度
elif ADX > 25 and DI_Spread < -5:
    regime = "TREND_DOWN"
    confidence = min(ADX / 40, 1.0)
elif ADX < 20:
    regime = "RANGE"
    confidence = 0.7
else:
    regime = "TRANSITION"  # 过渡状态
    confidence = 0.5
```

#### 3.1.5 5m/1h适用性分析

**1h周期**: 表现优秀，ADX对1h级别的趋势过滤效果好，历史数据验证稳定。

**5m周期**: 噪音较多，ADX容易在短期波动中产生误判。需要：
- 提高ADX阈值到27-30
- 增加确认K线数量到4-5根
- 结合成交量放大验证

#### 3.1.6 优点
- 经典方案，经过广泛验证
- 计算简单，实时性好
- 同时提供方向和强度信息

#### 3.1.7 局限性
- 对震荡市的识别不够精确
- 滞后性中等（需要等待ADX确认）
- 盘整期间的ADX下降可能导致过早退出

#### 3.1.8 推荐硬编码参数

```python
ADX_DMI_PARAMS = {
    "adx_period": 14,
    "adx_threshold_trend": 25,       # 1h趋势确认阈值
    "adx_threshold_5m": 27,         # 5m趋势确认阈值（更高）
    "di_spread_threshold": 5,       # DI差值阈值
    "confirm_bars_1h": 2,           # 1h确认K线数
    "confirm_bars_5m": 4,           # 5m确认K线数（更多噪音）
    "range_adx_max": 20,            # 震荡市ADX上限
}
```

---

### 方案②：波动率锥（Volatility Cone）

#### 3.2.1 原理
- 在不同时间周期上计算历史波动率
- 比较当前波动率在历史波动率分布中的分位数位置
- 趋势市场中波动率通常持续偏高；震荡市场中波动率在低位收敛

#### 3.2.2 计算公式

```python
# 收益率波动率（多种周期）
vol_5m  = std(log(P_t / P_{t-1})) * sqrt(288)   # 年化（5m）
vol_1h  = std(log(P_t / P_{t-1})) * sqrt(24)    # 年化（1h）
vol_1d  = std(log(P_t / P_{t-1})) * sqrt(365)    # 年化（1d）

# 波动率锥：不同回溯周期的波动率分布
vol_cone = {
    "5m_10": percentile(vol_series_10,  current_vol),   # 10根5mK线的波动率分位数
    "5m_30": percentile(vol_series_30,  current_vol),
    "5m_60": percentile(vol_series_60,  current_vol),
    "1h_10": percentile(vol_1h_series_10, current_vol_1h),
    "1h_30": percentile(vol_1h_series_30, current_vol_1h),
    "1h_60": percentile(vol_1h_series_60, current_vol_1h),
}

# 趋势判断：
# - 如果短期波动率分位数 > 70% 且 长期波动率分位数 > 50% → 趋势市场（波动率扩散）
# - 如果短期波动率分位数 < 40% 且 长期波动率分位数 < 40% → 震荡市场（波动率收缩）
# - 其他 → 过渡状态

vol_regime_score = (short_term_percentile + long_term_percentile) / 2  # 0-100
```

#### 3.2.3 阈值选择

| 参数 | 5m推荐值 | 1h推荐值 | 依据 |
|------|---------|---------|------|
| 短期回溯周期 | 20根K线 | 20根K线 | 足够样本又不过时 |
| 长期回溯周期 | 60根K线 | 60根K线 | 约1天(5m)/2.5天(1h) |
| 趋势确认分位数 | >65% | >60% | 波动率在高位扩散 |
| 震荡确认分位数 | <40% | <35% | 波动率收缩收敛 |

#### 3.2.4 状态判断逻辑

```python
short_vol_pct = get_vol_percentile(vol_5m, lookback_short=20)
long_vol_pct  = get_vol_percentile(vol_5m, lookback_long=60)

if short_vol_pct > 65 and long_vol_pct > 50:
    regime = "TREND"
    # 但无法判断方向，需结合其他指标
elif short_vol_pct < 40 and long_vol_pct < 40:
    regime = "RANGE"
    confidence = 0.8
else:
    regime = "TRANSITION"
    confidence = 0.5
```

#### 3.2.5 5m/1h适用性分析

**1h周期**: 非常适合，波动率变化相对缓慢，锥形分析稳定可靠。

**5m周期**: 波动率变化剧烈，锥形边界不稳定。需要：
- 使用更短的短期回溯（10根K线）
- 结合ATR比率而非纯波动率

#### 3.2.6 优点
- 对震荡市的识别特别有效
- 不依赖价格方向，纯量化
- 可以预测波动率回归（均值回归特性）

#### 3.2.7 局限性
- 不提供方向信息（需结合其他指标）
- 计算量较大
- 在极端波动事件（如黑天鹅）时失效

#### 3.2.8 推荐硬编码参数

```python
VOL_CONE_PARAMS = {
    "short_lookback": 20,        # 短期回溯K线数
    "long_lookback": 60,         # 长期回溯K线数
    "trend_percentile_low": 60, # 趋势确认下限（1h）
    "trend_percentile_low_5m": 65, # 趋势确认下限（5m，更严格）
    "range_percentile_high": 35, # 震荡确认上限（1h）
    "range_percentile_high_5m": 40, # 震荡确认上限（5m）
    "min_samples": 30,           # 最小样本要求
}
```

---

### 方案③：均线密集度/MA状态方案

#### 3.3.1 原理
- 多条均线的收敛程度反映市场状态
- **趋势市**: 均线间距扩大，多头排列（上涨）或空头排列（下跌）
- **震荡市**: 均线密集纠缠，价格在窄幅范围内波动

#### 3.3.2 计算公式

```python
# 均线系统
ma5   = EMA(close, 5)
ma20  = EMA(close, 20)
ma60  = EMA(close, 60)
ma120 = EMA(close, 120)

# 均线密集度指标
ma_spread_5_20  = abs(ma5 - ma20) / close * 100
ma_spread_20_60 = abs(ma20 - ma60) / close * 100
ma_spread_60_120 = abs(ma60 - ma120) / close * 100

# 总密集度分数（0=完全纠缠, 100=完全发散）
avg_spread = (ma_spread_5_20 + ma_spread_20_60 + ma_spread_60_120) / 3

# 均线方向一致性
ma_direction_score = count_of_ma_aligned_in_same_direction / 3
# 例如: ma5>ma20>ma60>ma120 → 4条全部多头对齐 → score=1.0

# 完整判断
combined_ma_score = avg_spread * ma_direction_score  # 0-100
```

#### 3.3.3 阈值选择

| 参数 | 5m推荐值 | 1h推荐值 | 依据 |
|------|---------|---------|------|
| 密集度趋势阈值 | >1.5% | >2.0% | 均线间距需足够大 |
| 密集度震荡阈值 | <0.5% | <0.8% | 均线几乎重叠 |
| 方向一致性要求 | 3/4条对齐 | 3/4条对齐 | 避免部分对齐的模糊状态 |

#### 3.3.4 状态判断逻辑

```python
def classify_by_ma(ma5, ma20, ma60, ma120, close):
    spread = (abs(ma5-ma20) + abs(ma20-ma60) + abs(ma60-ma120)) / 3 / close

    # 方向判断
    if ma5 > ma20 > ma60 > ma120:
        direction = "up"
        alignment = 4
    elif ma5 < ma20 < ma60 < ma120:
        direction = "down"
        alignment = 4
    elif ma20 > ma60:
        direction = "up_weakening"
        alignment = 3
    elif ma20 < ma60:
        direction = "down_weakening"
        alignment = 3
    else:
        direction = "neutral"
        alignment = 2

    # 状态判断
    if spread > 0.02 and alignment >= 3:
        regime = f"TREND_{direction.upper()}"
        confidence = min(spread / 0.05, 1.0) * (alignment / 4)
    elif spread < 0.008:
        regime = "RANGE"
        confidence = 0.8
    else:
        regime = "TRANSITION"
        confidence = 0.5

    return regime, confidence, spread, alignment
```

#### 3.3.5 5m/1h适用性分析

**1h周期**: 表现良好，均线系统稳定，假信号较少。

**5m周期**: 表现较差，5m级别噪音多，均线频繁交叉。需要：
- 使用更长期的均线（ma20, ma60, ma120, ma240）替代短周期
- 增加方向一致性要求（4/4条对齐）

#### 3.3.6 优点
- 计算极简，几乎零延迟
- 方向判断直观
- 可以实时监控均线收敛/发散过程

#### 3.3.7 局限性
- 对均线参数选择敏感
- 盘整期的均线收敛不一定带来上涨
- 滞后性较高（EMA的固有特性）

#### 3.3.8 推荐硬编码参数

```python
MA_DENSITY_PARAMS = {
    "ma_periods": [5, 20, 60, 120],  # 5m建议 [20, 60, 120, 240]
    "trend_spread_1h": 0.02,         # 1h趋势均线间距阈值
    "trend_spread_5m": 0.015,        # 5m趋势均线间距阈值（更保守）
    "range_spread_1h": 0.008,        # 1h震荡均线间距上限
    "range_spread_5m": 0.006,        # 5m震荡均线间距上限
    "min_alignment": 3,               # 最少对齐均线数
    "use_ema": True,                  # 使用EMA而非SMA
}
```

---

### 方案④：趋势动量综合方案（推荐）

#### 3.4.1 原理
结合多种指标的优势，通过多维度确认来识别趋势启动早期：
1. **RSI动量**: 超卖/超买状态 + RSI趋势线斜率
2. **成交量突破**: 价格+成交量双重确认
3. **趋势结构**: 更高高/更高低（上涨）或更低低/更高低（下跌）
4. **波动率适配**: 根据波动率调整判断标准

#### 3.4.2 计算公式

```python
# 1. RSI动量指标
rsi = ta.RSI(close, 14)
rsi_slope = (rsi - rsi[-10]) / 10  # RSI 10根K线斜率

# RSI超卖区间反弹识别（趋势启动早期）
rsi_oversold_bounce = rsi[-1] < 40 and rsi[-1] > rsi[-3] > rsi[-5]

# RSI超买区间回落识别（下跌趋势启动）
rsi_overbought_drop = rsi[-1] > 60 and rsi[-1] < rsi[-3] < rsi[-5]

# 2. 成交量突破
vol_ma20 = SMA(volume, 20)
vol_surge = volume > vol_ma20 * 1.5  # 成交量放大1.5倍

# 3. 趋势结构（更高高/更高低）
higher_highs = sum(1 for i in range(5) if high[-i] > high[-(i+1)]) >= 3
higher_lows  = sum(1 for i in range(5) if low[-i] > low[-(i+1)]) >= 3
lower_lows   = sum(1 for i in range(5) if low[-i] < low[-(i+1)]) >= 3
lower_highs  = sum(1 for i in range(5) if high[-i] < high[-(i+1)]) >= 3

# 4. 波动率调整（动态阈值）
atr = ta.ATR(high, low, close, 14)
atr_ma20 = SMA(atr_series, 20)
atr_ratio = atr / atr_ma20  # >1 表示波动率扩大

# 综合评分
trend_score = 0
if rsi_oversold_bounce: trend_score += 30
if vol_surge: trend_score += 25
if higher_highs and higher_lows: trend_score += 25
if atr_ratio > 1.0: trend_score += 20  # 波动率扩大配合

# 趋势确认阈值
TREND_CONFIRM_SCORE = 60   # 综合得分超过60确认趋势
RANGE_CONFIRM_SCORE = 30   # 综合得分低于30确认震荡
```

#### 3.4.3 阈值选择

| 参数 | 5m推荐值 | 1h推荐值 | 依据 |
|------|---------|---------|------|
| RSI超卖反弹阈值 | <40 | <35 | 1h需要更强的超卖才可能反弹 |
| RSI超买回落阈值 | >60 | >65 | 1h需要更高的超买才确认下跌 |
| 成交量放大倍数 | 1.5x | 1.5x | 趋势启动需要量能配合 |
| 综合得分趋势阈值 | 60 | 55 | 多指标确认需达到一定分数 |
| 综合得分震荡阈值 | 30 | 25 | 低分才能确认震荡 |

#### 3.4.4 状态判断逻辑

```python
def classify_momentum综合(df):
    score = 0
    details = {}

    # RSI条件
    rsi = df['rsi'].iloc[-1]
    rsi_slope = df['rsi'].iloc[-1] - df['rsi'].iloc[-5]
    if rsi < 35 and rsi_slope > 2:
        score += 30
        details['rsi_signal'] = 'oversold_bounce'
    elif rsi > 65 and rsi_slope < -2:
        score += 30
        details['rsi_signal'] = 'overbought_drop'
    else:
        details['rsi_signal'] = 'neutral'

    # 成交量条件
    vol_ma = df['volume'].rolling(20).mean().iloc[-1]
    if df['volume'].iloc[-1] > vol_ma * 1.5:
        score += 25
        details['vol_signal'] = 'surge'
    else:
        details['vol_signal'] = 'normal'

    # 结构条件
    recent_5_highs = df['high'].iloc[-5:].values
    recent_5_lows = df['low'].iloc[-5:].values
    if all(recent_5_highs[i] > recent_5_highs[i+1] for i in range(4)):
        score += 25
        details['structure'] = 'higher_highs'
    elif all(recent_5_lows[i] < recent_5_lows[i+1] for i in range(4)):
        score += 25
        details['structure'] = 'lower_lows'
    else:
        details['structure'] = 'mixed'

    # 趋势判断
    if score >= 60:
        regime = "TREND_UP" if details['rsi_signal'] == 'oversold_bounce' else "TREND_DOWN"
        confidence = score / 100
    elif score <= 30:
        regime = "RANGE"
        confidence = (30 - score) / 30
    else:
        regime = "TRANSITION"
        confidence = 0.5

    return regime, confidence, score, details
```

#### 3.4.5 5m/1h适用性分析

**1h周期**: 优秀，综合多个维度，1h级别的结构更稳定。

**5m周期**: 优秀，通过多指标过滤减少噪音，但需要更严格的参数。

#### 3.4.6 优点
- 多维度确认，过滤假信号能力强
- 可以识别趋势启动早期（RSI超卖反弹）
- 适应性强，可根据市场调整

#### 3.4.7 局限性
- 参数较多，调参复杂
- 需要多个指标同时满足条件
- 计算量较大

#### 3.4.8 推荐硬编码参数

```python
MOMENTUM_COMPOSITE_PARAMS = {
    "rsi_period": 14,
    "rsi_oversold_1h": 35,
    "rsi_oversold_5m": 40,
    "rsi_overbought_1h": 65,
    "rsi_overbought_5m": 60,
    "rsi_slope_period": 5,
    "rsi_slope_threshold": 2,
    "volume_surge_multiplier": 1.5,
    "volume_ma_period": 20,
    "structure_lookback": 5,
    "atr_period": 14,
    "atr_ratio_threshold": 1.0,
    "trend_score_threshold_1h": 55,
    "trend_score_threshold_5m": 60,
    "range_score_threshold_1h": 25,
    "range_score_threshold_5m": 30,
}
```

---

## 四、假突破问题解决方案

### 4.1 假突破的识别特征

震荡市场中的假突破通常有以下特征：
1. **价格快速刺破后迅速回落**（影线较长）
2. **成交量未能配合放大**
3. **突破后未能站稳**（收盘价在原区间内）
4. **ADX未能跟随上升**（趋势未确认）

### 4.2 过滤假突破的机制

#### 双重确认机制
```python
def is_valid_breakout(df, direction='up'):
    """真假突破辨别"""
    # 条件1: 突破幅度足够（>0.5%）
    if direction == 'up':
        breakout = close[-1] > high[-20:-1].max()
        breakout_pct = (close[-1] - high[-20:-1].max()) / close[-1]
    else:
        breakout = close[-1] < low[-20:-1].min()
        breakout_pct = (low[-20:-1].min() - close[-1]) / close[-1]

    # 条件2: 成交量放大确认
    vol_confirm = volume[-1] > volume_ma20 * 1.5

    # 条件3: 站稳（不快速回落）
    if direction == 'up':
       站稳 = close[-1] > close[-2] > close[-3]  # 连续3根K线收高
    else:
        站稳 = close[-1] < close[-2] < close[-3]

    # 条件4: ADX配合
    adx_confirm = adx[-1] > 25

    valid = breakout and vol_confirm and 站稳 and adx_confirm
    return valid, {
        'breakout': breakout,
        'breakout_pct': breakout_pct,
        'vol_confirm': vol_confirm,
        'price_stable': 站稳,
        'adx_confirm': adx_confirm
    }
```

#### 时间过滤法
```python
def breakout_with_time_filter(df, direction='up', hold_bars=3):
    """
    突破后需要连续N根K线保持在新区间内
    防止假突破后立即回落
    """
    if direction == 'up':
        level = high[-20:-1].max()
        holds = all(df['close'].iloc[-(hold_bars):] > level)
    else:
        level = low[-20:-1].min()
        holds = all(df['close'].iloc[-(hold_bars):] < level)

    return holds
```

### 4.3 推荐假突破过滤流程

```
价格突破检测
    ↓
条件1: 突破幅度 > 0.5%? ──否──→ 噪音，忽略
    ↓是
条件2: 成交量放大 > 1.5x? ──否──→ 疑似假突破，标记观察
    ↓是
条件3: 连续3根K线站稳? ──否──→ 假突破，不确认
    ↓是
条件4: ADX > 25? ──否──→ 趋势未形成，不确认
    ↓是
✅ 确认为有效突破
```

---

## 五、滞后性问题解决方案

### 5.1 滞后性来源分析

| 来源 | 影响程度 | 描述 |
|------|---------|------|
| ADX计算本质 | 高 | ADX需要趋势形成后才能上升 |
| 均线的EMA延迟 | 中 | EMA对价格变化的响应有延迟 |
| 确认K线要求 | 高 | 需要N根K线确认进一步延迟 |
| 指标平滑处理 | 中 | 多次平均放大延迟 |

### 5.2 缓解滞后性的策略

#### 策略1：预测性阈值
```python
"""
不使用"确认后入场"，而是"接近确认时预判"
在ADX从15向25突破的过程中，当ADX>20时开始关注
当ADX>23且+DI>-DI时，预判趋势即将确认
"""
adx_early = adx[-1] > 20 and adx[-1] > adx[-2]  # 上升趋势中
adx_approaching = adx[-1] > 23  # 接近确认阈值

if adx_early and adx_approaching:
    regime = "TREND_IMMINENT"  # 趋势即将确认
    confidence = adx[-1] / 30  # 置信度基于ADX接近程度
```

#### 策略2：前置指标辅助
```python
"""
使用更快速的指标作为ADX的前置确认
RSI超卖反弹往往先于ADX上升
当RSI从超卖区间反弹时，趋势可能已经在形成中
"""
rsi_recovery = rsi[-1] > 35 and rsi[-2] < 35  # RSI刚从超卖反弹
adx_rising = adx[-1] > adx[-3]  # ADX在上升通道

if rsi_recovery and adx_rising:
    # ADX尚未确认，但RSI已发出早期信号
    regime = "TREND_EARLY"
    confidence = 0.5  # 降低置信度，但提供早期预警
```

#### 策略3：多周期交叉确认
```python
"""
利用大周期趋势方向作为过滤器/确认
1h处于上升趋势时，5m的回调是更好的买入机会
减少在逆大周期方向上的假信号
"""
# 检查1h趋势
if regime_1h == "TREND_UP" and current_5m_regime == "RANGE":
    # 5m的震荡可能是大趋势中的回调
    # 当5m出现TREND信号时，优先判断为有效
    final_regime = "TREND_UP_CONTINUATION"
elif regime_1h == "TREND_DOWN":
    final_regime = "TREND_DOWN"
```

#### 策略4：动态确认阈值
```python
"""
在市场波动性增大时，降低确认阈值以加快响应
在市场波动性降低时，提高确认阈值以减少假信号
"""
atr_current = atr[-1]
atr_ma = sma(atr_series[-20:])
atr_ratio = atr_current / atr_ma

# 波动率高时，降低ADX阈值（更快确认）
if atr_ratio > 1.5:
    adx_threshold = 20  # 从25降低到20
elif atr_ratio < 0.8:
    adx_threshold = 28  # 从25提高到28
else:
    adx_threshold = 25  # 标准阈值
```

### 5.3 滞后性补偿的实际建议

1. **接受一定的滞后性**：趋势跟踪的核心是"宁可错过也不追错"
2. **使用多指标组合**：不同指标的滞后性可以互补
3. **分批建仓**：在趋势确认初期建半仓，趋势确认后加仓
4. **使用追踪止损**：让利润奔跑，用利润空间弥补入场滞后

---

## 六、Python伪代码

### 6.1 ADX+DMI 方案

```python
import pandas as pd
import numpy as np

class ADXDMIRegimeClassifier:
    """
    ADX + DMI 市场状态分类器
    基于方向性运动指标判断市场趋势/震荡状态
    """

    def __init__(self, adx_period=14, adx_threshold=25,
                 confirm_bars=2, di_spread_threshold=5):
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.confirm_bars = confirm_bars
        self.di_spread_threshold = di_spread_threshold

    def calculate_adx_dmi(self, high, low, close):
        """计算ADX和DMI指标"""
        n = self.adx_period

        # True Range
        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            )
        )

        # Directional Movement
        up_move = high - np.roll(high, 1)
        down_move = np.roll(low, 1) - low

        plus_dm = np.where(
            (up_move > down_move) & (up_move > 0),
            up_move, 0
        )
        minus_dm = np.where(
            (down_move > up_move) & (down_move > 0),
            down_move, 0
        )

        # Smooth
        atr = pd.Series(tr).rolling(n).mean()
        plus_di = pd.Series(plus_dm).rolling(n).mean() / (atr + 1e-10) * 100
        minus_di = pd.Series(minus_dm).rolling(n).mean() / (atr + 1e-10) * 100

        dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
        adx = dx.rolling(n).mean()

        return adx.values, plus_di.values, minus_di.values

    def classify(self, df):
        """
        分类市场状态

        Args:
            df: DataFrame with 'high', 'low', 'close' columns

        Returns:
            dict: {
                'regime': 'TREND_UP'/'TREND_DOWN'/'RANGE'/'TRANSITION',
                'confidence': 0.0-1.0,
                'adx': float,
                'di_spread': float,
                'trend_strength': 0.0-1.0
            }
        """
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values

        adx, plus_di, minus_di = self.calculate_adx_dmi(high, low, close)

        adx_current = adx[-1]
        plus_di_current = plus_di[-1]
        minus_di_current = minus_di[-1]
        di_spread = plus_di_current - minus_di_current

        # 检查连续确认
        adx_above = sum(adx[-(self.confirm_bars):] > self.adx_threshold)
        di_confirm = (di_spread > self.di_spread_threshold) or \
                     (di_spread < -self.di_spread_threshold)

        # 趋势强度归一化
        trend_strength = min(adx_current / 40, 1.0)

        # 状态判断
        if adx_above >= self.confirm_bars and di_confirm:
            if di_spread > 0:
                regime = "TREND_UP"
                confidence = trend_strength
            else:
                regime = "TREND_DOWN"
                confidence = trend_strength
        elif adx_current < 20:
            regime = "RANGE"
            confidence = max(0, 1.0 - adx_current / 20) * 0.8
        else:
            regime = "TRANSITION"
            confidence = 0.5

        return {
            'regime': regime,
            'confidence': confidence,
            'adx': adx_current,
            'di_spread': di_spread,
            'trend_strength': trend_strength,
            'plus_di': plus_di_current,
            'minus_di': minus_di_current,
            'timestamp': df.index[-1]
        }
```

### 6.2 Volatility Cone 方案

```python
class VolatilityConeClassifier:
    """
    波动率锥市场状态分类器
    通过比较不同回溯周期的波动率分位数判断市场状态
    """

    def __init__(self, short_lookback=20, long_lookback=60,
                 trend_percentile=60, range_percentile=35):
        self.short_lookback = short_lookback
        self.long_lookback = long_lookback
        self.trend_percentile = trend_percentile
        self.range_percentile = range_percentile

    def calculate_volatility(self, close, lookback):
        """计算历史波动率"""
        returns = np.diff(np.log(close))
        vol = np.std(returns[-lookback:]) * np.sqrt(365)  # 年化
        return vol

    def calculate_vol_percentile(self, vol_series, current_vol):
        """计算当前波动率在历史分布中的分位数"""
        if len(vol_series) < 30:
            return 50  # 数据不足返回中位数
        percentile = (vol_series < current_vol).sum() / len(vol_series) * 100
        return percentile

    def classify(self, df, price_col='close'):
        """
        分类市场状态

        Returns:
            dict: {
                'regime': 'TREND'/'RANGE'/'TRANSITION',
                'confidence': 0.0-1.0,
                'short_vol_pct': float,
                'long_vol_pct': float,
                'vol_regime_score': 0-100,
                'atr_ratio': float
            }
        """
        close = df[price_col].values

        # 计算短期和长期波动率
        vol_short = self.calculate_volatility(close, self.short_lookback)
        vol_long = self.calculate_volatility(close, self.long_lookback)

        # 计算ATR用于补充
        high = df['high'].values
        low = df['low'].values
        atr = self._calculate_atr(high, low, close, 14)
        atr_ma = pd.Series(atr).rolling(20).mean().iloc[-1]
        atr_ratio = atr[-1] / atr_ma if atr_ma > 0 else 1.0

        # 计算波动率序列（用于分位数计算）
        vol_series = []
        for i in range(self.long_lookback, len(close)):
            vol_i = self.calculate_volatility(close[:i], self.short_lookback)
            vol_series.append(vol_i)
        vol_series = np.array(vol_series)

        short_vol_pct = self.calculate_vol_percentile(vol_series, vol_short)
        long_vol_pct = self.calculate_vol_percentile(vol_series, vol_long)

        vol_regime_score = (short_vol_pct + long_vol_pct) / 2

        # 状态判断
        if short_vol_pct > self.trend_percentile and long_vol_pct > 50:
            regime = "TREND"  # 注意：不提供方向
            confidence = min(vol_regime_score / 80, 1.0)
        elif short_vol_pct < self.range_percentile and long_vol_pct < self.range_percentile:
            regime = "RANGE"
            confidence = max(0, (self.range_percentile - vol_regime_score) / self.range_percentile) * 0.8
        else:
            regime = "TRANSITION"
            confidence = 0.5

        return {
            'regime': regime,
            'confidence': confidence,
            'short_vol_pct': short_vol_pct,
            'long_vol_pct': long_vol_pct,
            'vol_regime_score': vol_regime_score,
            'atr_ratio': atr_ratio,
            'timestamp': df.index[-1]
        }

    def _calculate_atr(self, high, low, close, period=14):
        """计算ATR"""
        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            )
        )
        return pd.Series(tr).rolling(period).mean().values
```

### 6.3 MA密度方案

```python
class MADensityRegimeClassifier:
    """
    均线密集度市场状态分类器
    通过多条均线的收敛/发散程度判断市场状态
    """

    def __init__(self, ma_periods=[5, 20, 60, 120],
                 trend_spread=0.02, range_spread=0.008,
                 min_alignment=3):
        self.ma_periods = ma_periods
        self.trend_spread = trend_spread
        self.range_spread = range_spread
        self.min_alignment = min_alignment

    def calculate_emas(self, close):
        """计算各周期EMA"""
        emas = {}
        for period in self.ma_periods:
            emas[period] = pd.Series(close).ewm(span=period, adjust=False).mean().values[-1]
        return emas

    def calculate_spread(self, emas, close):
        """计算均线间距"""
        spreads = []
        periods = sorted(emas.keys())
        for i in range(len(periods) - 1):
            spread = abs(emas[periods[i]] - emas[periods[i+1]]) / close
            spreads.append(spread)
        return np.mean(spreads)

    def calculate_alignment(self, emas):
        """计算均线方向一致性"""
        periods = sorted(emas.keys())
        if len(periods) < 2:
            return 0

        # 检查是否多头排列
        all_up = all(emas[periods[i]] > emas[periods[i+1]]
                     for i in range(len(periods)-1))
        all_down = all(emas[periods[i]] < emas[periods[i+1]]
                       for i in range(len(periods)-1))

        if all_up:
            return 4, "up"
        elif all_down:
            return 4, "down"
        else:
            # 部分对齐
            up_count = sum(1 for i in range(len(periods)-1)
                          if emas[periods[i]] > emas[periods[i+1]])
            return up_count, "mixed"

    def classify(self, df, price_col='close'):
        """
        分类市场状态

        Returns:
            dict: {
                'regime': 'TREND_UP'/'TREND_DOWN'/'RANGE'/'TRANSITION',
                'confidence': 0.0-1.0,
                'avg_spread': float,
                'alignment': int,
                'direction': str
            }
        """
        close = df[price_col].values[-1]
        close_series = df[price_col].values

        emas = self.calculate_emas(close_series)
        avg_spread = self.calculate_spread(emas, close)
        alignment, direction = self.calculate_alignment(emas)

        # 状态判断
        if avg_spread > self.trend_spread and alignment >= self.min_alignment:
            if direction == "up":
                regime = "TREND_UP"
            elif direction == "down":
                regime = "TREND_DOWN"
            else:
                regime = "TRANSITION"
            confidence = min(avg_spread / (self.trend_spread * 2), 1.0) * (alignment / 4)
        elif avg_spread < self.range_spread:
            regime = "RANGE"
            confidence = max(0, (self.range_spread - avg_spread) / self.range_spread) * 0.8
        else:
            regime = "TRANSITION"
            confidence = 0.5

        return {
            'regime': regime,
            'confidence': confidence,
            'avg_spread': avg_spread,
            'alignment': alignment,
            'direction': direction,
            'timestamp': df.index[-1]
        }
```

### 6.4 趋势动量综合方案（推荐）

```python
class MomentumCompositeRegimeClassifier:
    """
    趋势动量综合分类器
    结合RSI、成交量、趋势结构、波动率多个维度
    推荐用于5m和1h周期
    """

    def __init__(self,
                 rsi_period=14,
                 rsi_oversold=35,
                 rsi_overbought=65,
                 volume_surge=1.5,
                 volume_ma_period=20,
                 structure_lookback=5,
                 atr_period=14,
                 trend_score_threshold=55,
                 range_score_threshold=25):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.volume_surge = volume_surge
        self.volume_ma_period = volume_ma_period
        self.structure_lookback = structure_lookback
        self.atr_period = atr_period
        self.trend_score_threshold = trend_score_threshold
        self.range_score_threshold = range_score_threshold

    def calculate_rsi(self, close, period=14):
        """计算RSI"""
        delta = np.diff(close, prepend=close[0])
        gain = np.where(delta > 0, delta, 0)
        loss = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gain).rolling(period).mean()
        avg_loss = pd.Series(loss).rolling(period).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi.values

    def calculate_atr(self, high, low, close, period=14):
        """计算ATR"""
        tr = np.maximum(
            high - low,
            np.maximum(
                np.abs(high - np.roll(close, 1)),
                np.abs(low - np.roll(close, 1))
            )
        )
        return pd.Series(tr).rolling(period).mean().values

    def calculate_structure(self, high, low, lookback=5):
        """计算趋势结构"""
        recent_highs = high[-lookback:]
        recent_lows = low[-lookback:]

        # 更高高
        higher_highs = sum(1 for i in range(lookback-1)
                          if recent_highs[i] > recent_highs[i+1])
        # 更高低
        higher_lows = sum(1 for i in range(lookback-1)
                         if recent_lows[i] > recent_lows[i+1])
        # 更低低
        lower_lows = sum(1 for i in range(lookback-1)
                        if recent_lows[i] < recent_lows[i+1])
        # 更低高
        lower_highs = sum(1 for i in range(lookback-1)
                         if recent_highs[i] < recent_highs[i+1])

        return {
            'higher_highs': higher_highs >= 3,
            'higher_lows': higher_lows >= 3,
            'lower_lows': lower_lows >= 3,
            'lower_highs': lower_highs >= 3
        }

    def calculate_score(self, df):
        """计算综合得分"""
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values
        volume = df['volume'].values

        score = 0
        details = {}

        # RSI条件
        rsi = self.calculate_rsi(close, self.rsi_period)
        rsi_current = rsi[-1]
        rsi_slope = rsi[-1] - rsi[-5] if len(rsi) >= 5 else 0

        if rsi_current < self.rsi_oversold and rsi_slope > 2:
            score += 30
            details['rsi_signal'] = 'oversold_bounce'
        elif rsi_current > self.rsi_overbought and rsi_slope < -2:
            score += 30
            details['rsi_signal'] = 'overbought_drop'
        else:
            details['rsi_signal'] = 'neutral'

        # 成交量条件
        vol_ma = pd.Series(volume).rolling(self.volume_ma_period).mean().iloc[-1]
        if volume[-1] > vol_ma * self.volume_surge:
            score += 25
            details['vol_signal'] = 'surge'
        else:
            details['vol_signal'] = 'normal'

        # 结构条件
        structure = self.calculate_structure(high, low, self.structure_lookback)
        if structure['higher_highs'] and structure['higher_lows']:
            score += 25
            details['structure'] = 'uptrend'
        elif structure['lower_lows'] and structure['lower_highs']:
            score += 25
            details['structure'] = 'downtrend'
        else:
            details['structure'] = 'mixed'

        # ATR条件
        atr = self.calculate_atr(high, low, close, self.atr_period)
        atr_ma = pd.Series(atr).rolling(20).mean().iloc[-1]
        atr_ratio = atr[-1] / atr_ma if atr_ma > 0 else 1.0
        if atr_ratio > 1.0:
            score += 20
            details['atr_signal'] = 'expanding'
        else:
            details['atr_signal'] = 'contracting'

        details['total_score'] = score
        return score, details

    def classify(self, df):
        """
        分类市场状态

        Returns:
            dict: {
                'regime': 'TREND_UP'/'TREND_DOWN'/'RANGE'/'TRANSITION',
                'confidence': 0.0-1.0,
                'regime_score': -100 to +100,
                'trend_strength': 0.0-1.0,
                'breakout_confirmed': bool,
                'details': dict
            }
        """
        score, details = self.calculate_score(df)
        close = df['close'].values
        high = df['high'].values
        low = df['low'].values

        # 计算趋势方向得分（-100到+100）
        if details['rsi_signal'] == 'oversold_bounce':
            regime_score = score * 0.5  # 正分
        elif details['rsi_signal'] == 'overbought_drop':
            regime_score = -score * 0.5  # 负分
        else:
            regime_score = 0

        # 状态判断
        if score >= self.trend_score_threshold:
            if details['rsi_signal'] == 'oversold_bounce':
                regime = "TREND_UP"
            else:
                regime = "TREND_DOWN"
            confidence = score / 100
            breakout_confirmed = True
        elif score <= self.range_score_threshold:
            regime = "RANGE"
            confidence = (self.range_score_threshold - score) / self.range_score_threshold
            breakout_confirmed = False
        else:
            regime = "TRANSITION"
            confidence = 0.5
            breakout_confirmed = False

        # 趋势强度
        trend_strength = max(0, (score - 25) / 75) if regime.startswith("TREND") else 0

        return {
            'regime': regime,
            'confidence': confidence,
            'regime_score': regime_score,
            'trend_strength': trend_strength,
            'breakout_confirmed': breakout_confirmed,
            'volatility_regime': 'HIGH' if details.get('atr_signal') == 'expanding' else 'NORMAL',
            'details': details,
            'timestamp': df.index[-1]
        }
```

---

## 七、regime_classifier 信号输出格式设计

### 7.1 信号Schema

```python
@dataclass
class RegimeSignal:
    """
    市场状态分类器的标准输出格式
    """
    # === 标识 ===
    source: str = "regime_classifier"      # 固定值，标识来源
    schema_version: str = "1.0"            # Schema版本

    # === 主信号 ===
    regime: str = "UNKNOWN"                # TREND_UP/TREND_DOWN/RANGE/TRANSITION/UNKNOWN
    regime_detailed: str = "UNKNOWN"       # 更详细的状态描述

    # === 置信度和强度 ===
    confidence: float = 0.0                # 置信度 0.0-1.0
    regime_score: int = 0                  # 趋势强度 -100 to +100
                                            # +100 = 强多头, -100 = 强空头, 0 = 中线
    trend_strength: float = 0.0             # 趋势强度 0.0-1.0

    # === 辅助信息 ===
    volatility_regime: str = "NORMAL"       # HIGH/NORMAL/LOW
    breakout_confirmed: bool = False        # 突破是否经过验证
    breakout_direction: str = "NONE"        # UP/DOWN/NONE
    early_warning: bool = False             # 是否有早期预警

    # === 各方案得分 ===
    adx_dmi_score: int = 0                  # ADX+DMI方案得分 0-100
    vol_cone_score: int = 0                 # 波动率锥方案得分 0-100
    ma_density_score: int = 0              # 均线密集度方案得分 0-100
    momentum_score: int = 0                # 动量综合方案得分 0-100

    # === 原始指标值 ===
    adx: float = 0.0                       # 当前ADX值
    di_spread: float = 0.0                 # DI差值 (+DI - -DI)
    rsi: float = 50.0                      # 当前RSI值
    atr_ratio: float = 1.0                 # ATR/ATR_MA比率
    volume_ratio: float = 1.0               # 成交量/成交量MA比率
    ma_spread: float = 0.0                 # 均线平均间距百分比

    # === 时间信息 ===
    timestamp: str = ""                     # ISO格式时间戳
    bar_timeframe: str = "1h"              # K线周期 5m/1h/4h
    bar_count: int = 0                     # 用于计算的K线数量

    # === 多周期确认 ===
    higher_tf_regime: str = "UNKNOWN"      # 大周期市场状态
    higher_tf_confirmed: bool = False      # 大周期是否确认

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'RegimeSignal':
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})
```

### 7.2 多方案Ensemble输出

```python
@dataclass
class EnsembleRegimeSignal:
    """
    多方案Ensemble的汇总输出
    """
    # 主判断（加权平均）
    primary_regime: str = "UNKNOWN"
    primary_confidence: float = 0.0

    # 各方案独立判断
    adx_dmi_signal: RegimeSignal = None
    vol_cone_signal: RegimeSignal = None
    ma_density_signal: RegimeSignal = None
    momentum_signal: RegimeSignal = None

    # 投票结果
    vote_trend_up: int = 0
    vote_trend_down: int = 0
    vote_range: int = 0
    vote_transition: int = 0

    # 一致性指标
    agreement_score: float = 0.0  # 各方案一致性 0-1
    disagreement_reason: str = ""  # 不一致的原因

    # 时间戳
    timestamp: str = ""
    bar_timeframe: str = "1h"

    def get_final_signal(self) -> RegimeSignal:
        """
        根据投票和加权得分返回最终信号
        """
        # 多数投票
        votes = {
            "TREND_UP": self.vote_trend_up,
            "TREND_DOWN": self.vote_trend_down,
            "RANGE": self.vote_range,
            "TRANSITION": self.vote_transition
        }
        final_regime = max(votes, key=votes.get)

        # 如果有方案强烈反对（得分极端），可能需要调整
        extreme_scores = [
            self.adx_dmi_signal.regime_score if self.adx_dmi_signal else 0,
            self.momentum_signal.regime_score if self.momentum_signal else 0
        ]

        # 计算最终置信度
        max_vote = max(votes.values())
        total_votes = sum(votes.values())
        vote_confidence = max_vote / total_votes if total_votes > 0 else 0
        agreement_factor = self.agreement_score

        final_confidence = vote_confidence * 0.7 + agreement_factor * 0.3

        return RegimeSignal(
            regime=final_regime,
            confidence=final_confidence,
            timestamp=self.timestamp,
            bar_timeframe=self.bar_timeframe
        )
```

### 7.3 信号输出JSON示例

```json
{
    "source": "regime_classifier",
    "schema_version": "1.0",
    "timestamp": "2026-04-25T09:30:00+08:00",
    "bar_timeframe": "1h",
    "bar_count": 100,

    "primary_regime": "TREND_UP",
    "primary_confidence": 0.78,

    "adx_dmi": {
        "regime": "TREND_UP",
        "confidence": 0.82,
        "adx": 31.5,
        "di_spread": 12.3
    },
    "vol_cone": {
        "regime": "TREND",
        "confidence": 0.71,
        "short_vol_pct": 72.5,
        "long_vol_pct": 58.2
    },
    "ma_density": {
        "regime": "TREND_UP",
        "confidence": 0.75,
        "avg_spread": 0.028,
        "alignment": 4
    },
    "momentum_composite": {
        "regime": "TREND_UP",
        "confidence": 0.80,
        "regime_score": 75,
        "breakout_confirmed": true
    },

    "vote_trend_up": 4,
    "vote_trend_down": 0,
    "vote_range": 0,
    "vote_transition": 0,
    "agreement_score": 1.0
}
```

---

## 八、推荐方案及其理由

### 8.1 最终推荐：Momentum Composite + ADX-DMI Ensemble

**核心方案**：方案④（趋势动量综合）+ 方案①（ADX+DMI）Ensemble

**理由**：

1. **多指标互补**：ADX-DMI提供方向确认，Momentum Composite提供早期信号
2. **假突破过滤能力强**：通过RSI结构+成交量+趋势结构三重过滤
3. **滞后性可控**：Momentum Composite的RSI早期信号可提前2-5根K线预警
4. **5m和1h均适用**：参数可调，5m使用更严格的阈值

### 8.2 参数配置建议

```python
# ============================================================
# regime_classifier 推荐配置
# ============================================================

RECOMMENDED_PARAMS = {
    # === 主分类器参数 ===
    "primary_classifier": "momentum_composite",

    # === 5m周期配置 ===
    "5m": {
        "adx_threshold": 27,           # 更高的ADX阈值
        "confirm_bars": 4,             # 更多确认K线
        "rsi_oversold": 40,
        "rsi_overbought": 60,
        "trend_score_threshold": 60,  # 更严格
        "range_score_threshold": 30,
        "volume_surge": 1.5,
    },

    # === 1h周期配置 ===
    "1h": {
        "adx_threshold": 25,
        "confirm_bars": 2,
        "rsi_oversold": 35,
        "rsi_overbought": 65,
        "trend_score_threshold": 55,
        "range_score_threshold": 25,
        "volume_surge": 1.5,
    },

    # === Ensemble权重 ===
    "ensemble_weights": {
        "adx_dmi": 0.25,
        "vol_cone": 0.15,
        "ma_density": 0.20,
        "momentum_composite": 0.40,  # 动量综合权重最高
    },

    # === 假突破过滤 ===
    "breakout_filter": {
        "min_breakout_pct": 0.5,       # 最小突破幅度%
        "min_volume_ratio": 1.5,       # 最小成交量倍数
        "hold_bars": 3,                # 需站稳K线数
    },

    # === 滞后性缓解 ===
    "early_warning": {
        "enabled": True,
        "adx_early_threshold": 20,     # ADX提前预警阈值
        "rsi_early_threshold": 35,     # RSI提前预警阈值
    }
}
```

### 8.3 使用建议

1. **首次判断**：使用Ensemble投票结果，置信度 > 0.7 时执行
2. **早期预警**：关注 `early_warning=True` 信号，可提前关注
3. **趋势确认**：等待 `breakout_confirmed=True` 再加大仓位
4. **震荡回避**：`RANGE` 状态下禁用趋势策略，切换为均值回归或观望

---

## 九、后续工作建议

1. **回测验证**：在历史数据上验证各方案的准确率、召回率
2. **参数优化**：使用Walk-Forward方法优化阈值参数
3. **实时监控**：集成到 `kronos_multi_coin.py` 的信号流程
4. **多周期联动**：实现5m + 1h + 4h的多周期Regime联动判断

---

*文档版本: v1.0 | 设计者: AI Assistant | 日期: 2026-04-25*

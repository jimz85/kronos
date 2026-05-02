# Layer 1-3 Audit Results
**Date**: 2026-05-01  
**System**: Kronos v5.0  
**Mode**: 模拟盘 (OKX_FLAG=1)

---

## L1: 资金安全

### 1. OKX实时持仓 ✅ PASS
- **API query**: Successful
- **Current positions**: XRP-USDT-SWAP long 291张 @$1.362 (UPL=$259), BNB-USDT-SWAP long 17张 @$615.78 (UPL=$0.04)
- **Account equity**: USDT=$66,547.55, Total=$72,656.57
- **Status**: 模拟盘运行正常

### 2. SL方向 ✅ PASS
- `calc_sl_tp_from_entry()` (kronos_multi_coin.py L152-157):
  - LONG: `sl_price = entry * (1 - sl_pct)` → SL < entry ✅
  - SHORT: `sl_price = entry * (1 + sl_pct)` → SL > entry ✅
- `place_oco()` (L593-610) has additional defensive validation checking direction correctness

### 3. 仓位计算 `/100` 保护 ✅ PASS
- kronos_pilot.py L1238: `contracts = max(1, int(position_usdt / 100))` — uses `/100` ✅
- kronos_multi_coin.py uses `* 100` multiplier for OKX contract size ($100/contract):
  - L1979: `sl_dist_dollar = 100 * entry * sl_pct_check`
  - L2627: `sl_dist_dollar = price * sl_pct_dynamic * 100`
  - L3404: `_formula_sz = int(_eq * RISK_PER_TRADE / (100 * entry * _sl_pct))`
- Both formulas correctly account for OKX contract multiplier ✅

### 4. get_dynamic_limits() reserve ≥ equity×10% ✅ PASS
- L1265: `'reserve': round(equity * 0.20, 2)` → **20% ≥ 10%** ✅
- ⚠️ **Note**: Reserve check at L1308 (`if equity < limits['reserve']`) is self-referentially broken — since `reserve = equity × 0.20`, the condition `equity < equity × 0.20` is never true for positive equity. This makes the reserve check effectively dead code.

---

## L2: 逻辑正确性

### 1. BTC数据源 ✅ PASS
- `get_btc_direction()` (L854): Uses **1H** candles for RSI direction ✅
- `get_btc_market_regime()` (L866): Primary source is **1D (日线)** for MA200 calculation (L879: `get_ohlcv('BTC', '1D', 220)`), with **4H fallback** when daily data insufficient ✅
- Comment at L877 explicitly notes: "使用日线数据计算MA200（不是4H数据，4H×200=33天不是200天）" ✅

### 2. decide_for_position 在 full_scan 中被调用 ✅ PASS
- L3515: Comment "P0 Fix: 调用decide_for_position（之前是死代码，从未被使用）"
- L3523: `action, urgency, detail, should_hold, new_sl, new_tp = decide_for_position(actual_coin, pos, algos, md)` ✅
- Called in full_scan Stage 4 (规则引擎持仓决策)

### 3. alarm(0) 在 finally 里 ✅ PASS
All alarm(0) calls verified in try-finally blocks:
| Location | In finally? | Status |
|---|---|---|
| kronos_multi_coin.py L1825 (signal_factory) | ✅ `try:` → `finally: alarm(0)` | ✅ |
| kronos_multi_coin.py L2212 (voting timeout) | ✅ `try:` → `finally: alarm(0)` | ✅ |
| kronos_multi_coin.py L2480 (gemma4) | ✅ `try:` → `finally: alarm(0)` | ✅ |
| kronos_auto_guard.py L555 (cooldown early exit) | Early cleanup before exit | ✅ |
| kronos_auto_guard.py L582 (main guard) | ✅ Main `finally` block | ✅ |
| signal_factory.py L88 | ✅ `try:` → `finally: alarm(0)` | ✅ |
| core/gemma4_signal_validator.py L192 | ✅ `try:` → `finally: alarm(0)` | ✅ |

---

## L3: 策略一致性

### 1. COINS硬编码 vs _get_allowed_coins() 的比率 ⚠️ FAIL
| Set | Coins | Count |
|---|---|---|
| `ALL_COINS` (hardcoded L95) | AVAX, ETH, BTC, SOL, DOT, LINK, BNB, XRP | 8 |
| `_get_allowed_coins()` (dynamic) | DOGE, ADA, XRP, BNB, SOL (non-excluded from JSON) | 5 |
| Overlap | SOL, BNB, XRP | **3** |

**Issues found:**
- **ALL_COINS includes 5 excluded coins**: AVAX ✅excluded, ETH ✅excluded, BTC ✅excluded, DOT ✅excluded, LINK ✅excluded — all have `excluded: true` in coin_strategy_map.json
- **ALL_COINS misses 2 non-excluded coins**: DOGE (`excluded: false`) and ADA (`excluded: false`) are not in ALL_COINS
- The comment on L95 says "DOGE/ADA下架" but JSON reports them as not excluded
- full_scan() L3427 uses `ALL_COINS` directly instead of `_get_allowed_coins()`

### 2. ATR止损公式 ✅ PASS
- `calc_sl_tp_from_entry()` (kronos_multi_coin.py L146-158):
  - LONG: `entry_price * (1 - sl_pct)` ✅ (= entry*(1-sl_pct))
  - SHORT: `entry_price * (1 + sl_pct)` ✅ (= entry*(1+sl_pct))
- `open_position` execution (L2640-2645):
  - SHORT open: `sl = price * (1 + sl_pct)`, `tp = price * (1 - tp_pct)` ✅
  - LONG open: `sl = price * (1 - sl_pct)`, `tp = price * (1 + tp_pct)` ✅

### 3. 币种过滤一致性 ⚠️ FAIL
**Multiple inconsistencies detected:**

(a) `ALL_COINS` vs `_COIN_SL_ATR` / `_COIN_TP_RATIO`:
- `_COIN_SL_ATR` includes DOGE, ADA (L109-110) but these are NOT in ALL_COINS
- `_COIN_MIN_ATR_PCT` includes DOGE, ADA (L122-124) but NOT in ALL_COINS
- Creates mismatch: ATR config exists for coins that won't be scanned

(b) ALL_COINS vs coin_strategy_map.json exclusion:
- 5 excluded coins (AVAX, ETH, BTC, DOT, LINK) ARE scanned by full_scan
- 2 non-excluded coins (DOGE, ADA) are NOT scanned
- Effect: Wasting API calls on excluded coins, missing tradeable coins

(c) kronos_multi_coin.py has no `_get_allowed_coins()` equivalent:
- Only kronos_pilot.py has this function (L160)
- full_scan() bypasses dynamic filtering entirely

---

## Summary

| Layer | Check | Result |
|---|---|---|
| **L1** | 1. OKX实时持仓 | ✅ PASS |
| **L1** | 2. SL方向 | ✅ PASS |
| **L1** | 3. 仓位计算 `/100` 保护 | ✅ PASS |
| **L1** | 4. reserve ≥ equity×10% | ⚠️ PASS (但reserve检查为死代码) |
| **L2** | 1. BTC数据源 | ✅ PASS |
| **L2** | 2. decide_for_position 调用 | ✅ PASS |
| **L2** | 3. alarm(0) 在 finally | ✅ PASS |
| **L3** | 1. COINS硬编码 vs 动态 | ⚠️ FAIL |
| **L3** | 2. ATR止损公式 | ✅ PASS |
| **L3** | 3. 币种过滤一致性 | ⚠️ FAIL |

**3 Warnings / 2 Fails** — primarily in L3 strategy consistency. The main issues are:
1. `ALL_COINS` hardcoded list is stale and conflicts with `coin_strategy_map.json` exclusion flags
2. full_scan() bypasses dynamic coin filtering, scanning excluded coins and missing allowed ones
3. Reserve check is self-referentially broken (dead code)

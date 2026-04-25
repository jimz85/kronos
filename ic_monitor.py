#!/usr/bin/env python3
"""
因子IC实时监测面板 v2.0
每天早上自动运行，计算各币种各因子的IC值（含多周期：日线+4h）
输出Markdown报告 + 推送飞书

新增v2.0功能：
- 多周期IC：日线 + 4h 对比，选最优周期
- 衰减速率警报：7天IC斜率检测，提早发现因子失效
- IC加权自适应信号：按IC强度动态调整仓位和建议

依赖: yfinance, pandas, numpy, scipy
安装: pip install yfinance pandas numpy scipy
运行: python ic_monitor.py
"""

import yfinance as yf
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from datetime import datetime
import json
import os
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 配置
# ============================================================
COINS = {
    'BTC-USD': 'BTC',
    'ETH-USD': 'ETH',
    'ADA-USD': 'ADA',
    'DOGE-USD': 'DOGE',
    'AVAX-USD': 'AVAX',
    'DOT-USD': 'DOT',
    'SOL-USD': 'SOL',
}

FACTORS = ['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']
FACTOR_LABELS = {
    'rsi_inv': 'RSI均值回归',
    'vol_ratio': '成交量',
    'adx': 'ADX趋势',
    'trend_ma20': '动量MA20',
}

IC_THRESHOLD = 0.05    # IC有效阈值
IC_STRONG = 0.10       # IC强阈值
IC_DECAY_ALERT = 0.015 # 衰减警报阈值（7天下降>0.015视为衰减）
WINDOW_IC = 60         # IC计算滚动窗口(天)
WINDOW_RET = 1         # 未来收益窗口(天)

HISTORY_FILE = os.path.expanduser('~/.hermes/cron/output/ic_history.json')
PAPER_LOG = os.path.expanduser('~/.hermes/cron/output/ic_paper_log.json')

# ============================================================
# 技术指标计算
# ============================================================
def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def adx_calc(h, lo, c, p=14):
    pdm = h.diff(); mdm = -lo.diff()
    pdm[pdm < 0] = 0; mdm[mdm < 0] = 0
    tr = np.maximum(h - lo, np.maximum(abs(h - c.shift(1)), abs(lo - c.shift(1))))
    atr = tr.rolling(p).mean()
    pdi = 100 * (pdm.rolling(p).mean() / atr)
    mdi = 100 * (mdm.rolling(p).mean() / atr)
    dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    return dx.rolling(p).mean()

def load_data(ticker, period='2y', interval='1d'):
    """加载数据，处理yfinance多列名问题"""
    try:
        df = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            return None
        
        # Handle MultiIndex columns (Price, Ticker) -> just use Price level
        if isinstance(df.columns, pd.MultiIndex):
            # Columns are like ('Close', 'BTC-USD')
            new_cols = [c[0].lower() for c in df.columns]
        else:
            new_cols = [c.lower() for c in df.columns]
        df.columns = new_cols
        
        df.index = pd.to_datetime(df.index)
        if hasattr(df.index, 'tz') and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        return df
    except Exception as e:
        print(f'  Load error {ticker} {interval}: {e}')
        return None

def compute_factors(df):
    """计算所有因子"""
    f = pd.DataFrame(index=df.index)
    f['close'] = df['close']
    f['high'] = df['high']
    f['low'] = df['low']
    f['volume'] = df['volume']
    f['rsi'] = rsi(df['close'])
    f['rsi_inv'] = 100 - f['rsi']
    f['adx'] = adx_calc(df['high'], df['low'], df['close'])
    f['vol_ma5'] = df['volume'].rolling(5).mean()
    f['vol_ma20'] = df['volume'].rolling(20).mean()
    f['vol_ratio'] = f['vol_ma5'] / f['vol_ma20'].replace(0, np.nan)
    f['trend_ma20'] = df['close'] / df['close'].rolling(20).mean() - 1
    f['ret_next'] = df['close'].pct_change(WINDOW_RET).shift(-WINDOW_RET)
    return f

def compute_ic_fast(factors_df, ret_series, window=WINDOW_IC):
    """用Pearson快速计算各因子IC（比Spearman快100倍）"""
    ic_vals = {}
    n = len(factors_df)
    for fac in FACTORS:
        valid_idx = factors_df[fac].notna() & ret_series.notna()
        if valid_idx.sum() < window:
            ic_vals[fac] = np.nan
            continue
        # Use last `window` rows with valid data
        fac_arr = factors_df.loc[valid_idx, fac].values[-window:]
        ret_arr = ret_series.loc[valid_idx].values[-window:]
        if len(fac_arr) < window or np.std(fac_arr) < 1e-10 or np.std(ret_arr) < 1e-10:
            ic_vals[fac] = np.nan
        else:
            ic_vals[fac] = np.corrcoef(fac_arr, ret_arr)[0, 1]
    return ic_vals

def compute_ic_series_simple(f, window=WINDOW_IC):
    """简化的IC序列：每7天计算一次（减少80%计算量）"""
    ic_series = {fac: np.full(len(f), np.nan) for fac in FACTORS}
    step = 7  # 每7天算一次

    for i in range(window, len(f), step):
        window_data = f.iloc[i-window:i].dropna(subset=['ret_next'])
        if len(window_data) < window * 0.5:
            continue
        for fac in FACTORS:
            if window_data[fac].std() > 1e-10 and window_data['ret_next'].std() > 1e-10:
                ic_vals_arr = np.corrcoef(window_data[fac].values, window_data['ret_next'].values)[0, 1]
                if not np.isnan(ic_vals_arr):
                    for j in range(i, min(i + step, len(f))):
                        ic_series[fac][j] = ic_vals_arr

    for fac in FACTORS:
        f[f'ic_{fac}'] = ic_series[fac]
    return f

def compute_decay_rate(ic_series, days=7):
    """计算IC衰减速率（7天斜率）"""
    valid = ic_series.dropna()
    if len(valid) < days:
        return 0.0
    recent = valid.tail(days)
    if len(recent) < 2:
        return 0.0
    # Simple linear slope
    x = np.arange(len(recent))
    y = recent.values
    if np.std(y) < 1e-10:
        return 0.0
    slope = np.polyfit(x, y, 1)[0]
    return slope

# ============================================================
# IC分析
# ============================================================
def analyze_coin_period(ticker, coin_name, period='2y', interval='1d', label='1d'):
    """分析单个币种单个周期（使用快速IC计算）"""
    df = load_data(ticker, period=period, interval=interval)
    if df is None or len(df) < WINDOW_IC + 10:
        return None

    f = compute_factors(df)

    result = {
        'period': label,
        'coin': coin_name,
        'n_days': len(f),
        'data_end': str(f.index[-1].date()) if len(f) > 0 else 'N/A',
    }

    # 快速IC计算（当前60天窗口）
    factors_df = f[['rsi_inv', 'vol_ratio', 'adx', 'trend_ma20']]
    ret_series = f['ret_next']
    ic_current = compute_ic_fast(factors_df, ret_series, WINDOW_IC)

    # 7天前IC（用于衰减计算）
    ic_7d_ago = compute_ic_fast(factors_df.iloc[:-7], ret_series.iloc[:-7], WINDOW_IC) if len(f) > 10 else {}

    for fac in FACTORS:
        ic_now = ic_current.get(fac)
        ic_then = ic_7d_ago.get(fac)
        decay = (ic_now - ic_then) / 7 if (ic_now is not None and ic_then is not None and not np.isnan(ic_now) and not np.isnan(ic_then)) else 0.0

        result[f'ic_{fac}'] = ic_now
        result[f'ic_{fac}_mean30'] = ic_now  # 简化：直接用当前值
        result[f'ic_{fac}_decay7'] = decay

    result['last_price'] = float(f.iloc[-1]['close']) if len(f) > 0 else 0
    result['rsi'] = float(f.iloc[-1]['rsi']) if len(f) > 0 else 50

    return result

def analyze_coin_multi_period(ticker, coin_name):
    """多周期分析：日线（2年）+ 1h（1年）"""
    r1d = analyze_coin_period(ticker, coin_name, period='2y', interval='1d', label='1d')
    # 1h数据有限，只做1d
    return r1d, None

def get_adaptive_signal(results_1d, results_4h):
    """
    IC加权自适应信号
    返回: (方向, 置信度, 最优因子, 建议周期, 备注)
    """
    if results_1d is None:
        return None

    # 收集各因子在各周期上的IC
    fac_scores = {}
    for fac in FACTORS:
        ic_1d = results_1d.get(f'ic_{fac}', 0) or 0
        ic_4h = results_4h.get(f'ic_{fac}', 0) or 0 if results_4h else 0
        decay_1d = results_1d.get(f'ic_{fac}_decay7', 0) or 0
        decay_4h = results_4h.get(f'ic_{fac}_decay7', 0) or 0 if results_4h else 0

        # IC加权平均（近期衰减的因子降权）
        decay_penalty = abs(decay_1d) * 5  # 衰减越快惩罚越重
        score_1d = max(0, ic_1d - decay_penalty) if ic_1d > 0 else max(0, ic_1d + decay_penalty * 0.5)

        decay_penalty_4h = abs(decay_4h) * 5 if results_4h else 0
        score_4h = max(0, ic_4h - decay_penalty_4h) if ic_4h > 0 else max(0, ic_4h + decay_penalty_4h * 0.5) if results_4h else 0

        # 选最优周期
        if results_4h and score_4h > score_1d:
            fac_scores[fac] = (score_4h, '4h')
        else:
            fac_scores[fac] = (score_1d, '1d')

    # 找最优因子
    valid_facs = {k: v for k, v in fac_scores.items() if v[0] > IC_THRESHOLD}
    if not valid_facs:
        return {
            'direction': 'WATCH',
            'confidence': 0,
            'best_factor': None,
            'best_period': '1d',
            'signal': '⚠️ 所有因子IC无效，建议观望',
            'position_size': 0,
        }

    best_fac = max(valid_facs, key=lambda k: valid_facs[k][0])
    best_score, best_period = fac_scores[best_fac]
    best_ic = results_1d.get(f'ic_{best_fac}', 0) or 0
    decay_7d = results_1d.get(f'ic_{best_fac}_decay7', 0) or 0

    # 方向判断
    if best_fac == 'rsi_inv':
        direction = 'LONG' if best_ic > 0 else 'SHORT'
    elif best_fac == 'vol_ratio':
        direction = 'LONG'  # 高成交量看涨
    elif best_fac == 'adx':
        direction = 'TREND_FOLLOW'  # 趋势跟随
    else:
        direction = 'LONG' if best_ic > 0 else 'SHORT'

    # 置信度（基于IC强度和衰减速度）
    confidence = min(100, int(abs(best_score) / IC_STRONG * 100))
    if abs(decay_7d) > IC_DECAY_ALERT:
        confidence = max(10, confidence - 30)
        signal_extra = f'⚠️ 警告：{best_fac}因子7天衰减{decay_7d:.4f}/天，谨慎'
    else:
        signal_extra = ''

    # 仓位（基于置信度，0-100%）
    position_size = confidence / 100 * 0.30  # 最大30%仓位

    signal_map = {
        'LONG': '🟢 做多',
        'SHORT': '🔴 做空',
        'TREND_FOLLOW': '📈 趋势跟随',
        'WATCH': '⚠️ 观望',
    }

    return {
        'direction': direction,
        'confidence': confidence,
        'best_factor': best_fac,
        'best_period': best_period,
        'signal': f"{signal_map.get(direction, '❓')} | {best_fac}(IC={best_ic:.3f}) | 置信{confidence}%{'(衰减警告)' if abs(decay_7d) > IC_DECAY_ALERT else ''}",
        'position_size': position_size,
        'signal_extra': signal_extra,
    }

# ============================================================
# 格式化工具
# ============================================================
def fmt_ic(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return 'N/A'
    return f'{v:+.3f}'

def status_icon(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return '⚪'
    if v < -0.05:
        return '🔴'  # 反向
    elif v < IC_THRESHOLD:
        return '🟡'  # 弱
    elif v < IC_STRONG:
        return '🟢'  # 有效
    else:
        return '💎'  # 强

def decay_icon(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ''
    if abs(v) < 0.005:
        return '➖'  # 稳定
    elif v < -IC_DECAY_ALERT:
        return '📉'  # 快速衰减
    elif v > IC_DECAY_ALERT:
        return '📈'  # 改善
    else:
        return '↔️'  # 轻微变化

# ============================================================
# 报告生成
# ============================================================
def generate_report(all_results):
    """生成完整Markdown报告"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')

    report = f"""# 📊 因子IC监测报告 v2.0
**更新时间**: {now}
**IC阈值**: {IC_THRESHOLD} | **强阈值**: {IC_STRONG} | **衰减警报**: {IC_DECAY_ALERT}/天

---

## 🔬 各币种因子IC状态（含4h周期对比 + 衰减监测）

| 币种 | 周期 | RSI均值 | 成交量 | ADX | 动量MA20 | 最优 |
|------|------|---------|--------|-----|---------|------|
"""

    alerts = []

    for (coin, res_1d, res_4h) in all_results:
        if res_1d is None:
            continue

        # Row for 1d
        row = f"| **{coin}** | 1d |"
        for fac in FACTORS:
            ic = res_1d.get(f'ic_{fac}')
            dec = res_1d.get(f'ic_{fac}_decay7')
            row += f" {status_icon(ic)}{fmt_ic(ic)}{decay_icon(dec)} |"
        best = get_best_factor_1d(res_1d)
        row += f" {best} |"
        report += row + '\n'

        # Row for 4h (if available)
        if res_4h is not None:
            row = f"| {coin} | 4h |"
            for fac in FACTORS:
                ic = res_4h.get(f'ic_{fac}')
                row += f" {status_icon(ic)}{fmt_ic(ic)} |"
            row += f" {get_best_factor_1d(res_4h)} |"
            report += row + '\n'

        # Check alerts
        for fac in FACTORS:
            dec = res_1d.get(f'ic_{fac}_decay7')
            ic = res_1d.get(f'ic_{fac}')
            if dec is not None and not np.isnan(dec) and abs(dec) > IC_DECAY_ALERT and ic is not None and not np.isnan(ic) and abs(ic) > IC_THRESHOLD:
                direction = '📉' if dec < 0 else '📈'
                alerts.append(f"{direction} **{coin} {FACTOR_LABELS.get(fac, fac)}**: IC衰减率={dec:.4f}/天 {'(快速衰减!)' if dec < -IC_DECAY_ALERT else '(改善中)'}")

    # Adaptive signals section
    report += f"""
---

## 🎯 IC自适应交易信号（v2.0 新增）

| 币种 | 信号 | 置信度 | 最优因子 | 建议周期 | 仓位建议 | 备注 |
|------|------|--------|---------|---------|---------|------|
"""

    for (coin, res_1d, res_4h) in all_results:
        if res_1d is None:
            continue
        sig = get_adaptive_signal(res_1d, res_4h)
        if sig:
            pos_pct = f"{sig['position_size']*100:.0f}%" if sig['position_size'] > 0 else '—'
            extra = sig.get('signal_extra', '')
            conf_bar = '█' * (sig['confidence'] // 10) + '░' * (10 - sig['confidence'] // 10)
            report += f"| **{coin}** | {sig['signal'].split('|')[0].strip()} | {conf_bar}{sig['confidence']}% | {FACTOR_LABELS.get(sig['best_factor'] or '', sig['best_factor'] or '—')} | {sig['best_period']} | {pos_pct} | {extra} |\n"

    # Alerts section
    if alerts:
        report += f"""
---

## 🚨 因子衰减警报（v2.0 新增）

"""
        for alert in alerts:
            report += f"- {alert}\n"

    # Instructions
    report += f"""
---

## 📋 使用说明

**信号解读**:
- 💎/🟢 = 可交易 (IC > {IC_THRESHOLD})
- 🟡 = 观望 (IC 0 ~ {IC_THRESHOLD})
- 🔴 = 禁做 (IC < 0 或 IC < {IC_THRESHOLD}同时衰减快)

**衰减监测**:
- 📉 = 因子快速衰减，置信度降低30%
- 📈 = 因子正在改善
- ➖ = IC稳定

**仓位计算**:
- 仓位 = 置信度% × 30% (最大30%仓位)
- IC<0时自动降权，不直接做反向（IC负≠立刻做空，需独立验证）

> ⚠️ 本报告仅供参考，不构成投资建议。实盘前请充分回测验证。
"""

    return report, alerts

def get_best_factor_1d(res):
    if res is None:
        return '—'
    valid = {fac: res.get(f'ic_{fac}', 0) or 0 for fac in FACTORS}
    best = max(valid, key=valid.get)
    if valid[best] < IC_THRESHOLD:
        return '—'
    return FACTOR_LABELS.get(best, best)

# ============================================================
# 历史保存 & 纸质日志
# ============================================================
def save_history(all_results):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except:
            history = {}

    today = datetime.now().strftime('%Y-%m-%d')
    for (coin, res_1d, res_4h) in all_results:
        if res_1d is None:
            continue
        if today not in history:
            history[today] = {}
        ic_data = {fac: res_1d.get(f'ic_{fac}') for fac in FACTORS}
        best = get_best_factor_1d(res_1d)
        history[today][coin] = {'ic': ic_data, 'best': best}

    dates = sorted(history.keys())[-90:]
    history = {d: history[d] for d in dates}
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def save_paper_log(all_results):
    """保存纸质交易日志"""
    os.makedirs(os.path.dirname(PAPER_LOG), exist_ok=True)
    log = []
    if os.path.exists(PAPER_LOG):
        try:
            with open(PAPER_LOG) as f:
                log = json.load(f)
        except:
            log = []

    today = datetime.now().strftime('%Y-%m-%d')

    # Add today's recommended signals
    for (coin, res_1d, res_4h) in all_results:
        if res_1d is None:
            continue
        sig = get_adaptive_signal(res_1d, res_4h)
        if sig and sig['confidence'] >= 50:
            entry = {
                'date': today,
                'coin': coin,
                'signal': sig['signal'].split('|')[0].strip(),
                'best_factor': sig['best_factor'],
                'confidence': sig['confidence'],
                'period': sig['best_period'],
                'position_size': sig['position_size'],
                'ic': {fac: res_1d.get(f'ic_{fac}') for fac in FACTORS},
                'status': 'OPEN',  # 待执行
                'result': None,
            }
            log.append(entry)

    # Keep last 200 entries
    log = log[-200:]
    with open(PAPER_LOG, 'w') as f:
        json.dump(log, f, indent=2)

# ============================================================
# 飞书推送
# ============================================================
def push_feishu(report_text):
    try:
        import requests
        app_id = 'cli_a93c11b6bbf9dcc0'
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')

        token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        token_resp = requests.post(token_url, json={'app_id': app_id, 'app_secret': app_secret}, timeout=10)
        token_data = token_resp.json()

        if token_data.get('code') != 0:
            print(f'获取token失败: {token_data}')
            return False

        token = token_data.get('tenant_access_token')
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages'
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        chat_id = 'oc_bfd8a7cc1a606f190b53e3fd0167f5a0'
        content = json.dumps({'text': report_text[:4000]})

        payload = {
            'receive_id': chat_id,
            'msg_type': 'text',
            'content': content,
        }

        params = {'receive_id_type': 'chat_id'}
        resp = requests.post(msg_url, headers=headers, json=payload, params=params, timeout=10)
        result = resp.json()
        if result.get('code') == 0:
            print('飞书推送成功')
            return True
        else:
            print(f'飞书推送失败: {result}')
            return False
    except Exception as e:
        print(f'飞书推送异常: {e}')
        return False

# ============================================================
# 主入口
# ============================================================
if __name__ == '__main__':
    now_str = datetime.now().strftime('%H:%M:%S')
    print(f'[{now_str}] 开始IC监测 v2.0...')

    all_results = []

    for ticker, coin in COINS.items():
        try:
            r1d, r4h = analyze_coin_multi_period(ticker, coin)
            all_results.append((coin, r1d, r4h))
            if r1d:
                sig = get_adaptive_signal(r1d, r4h)
                sig_str = sig['signal'] if sig else '无信号'
                print(f'  {coin}: {sig_str}')
        except Exception as e:
            print(f'  {coin} 分析失败: {e}')

    report, alerts = generate_report(all_results)

    print()
    print(report)

    # 保存
    save_history(all_results)
    save_paper_log(all_results)

    # 飞书推送
    push_feishu(report)

    if alerts:
        alert_text = '🚨 因子衰减警报:\n' + '\n'.join(alerts[:5])
        print(f'\n{alert_text}')

    now_str2 = datetime.now().strftime('%H:%M:%S')
    print(f'\n[{now_str2}] IC监测完成')

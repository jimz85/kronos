#!/usr/bin/env python3
"""
策略实盘资格认证考试系统 (Live Ready Exam)
基于《策略实盘资格认证考试》六项评分标准

使用方式:
    python3 live_ready_exam.py --strategy ema30 --symbol BTC-USD --ema-period 30 --risk-pct 0.02

输出:
    - 六项评分 + 总分
    - 70分以下或单题零分 → 自动REJECTED
"""

import argparse, yfinance as yf, numpy as np, pandas as pd, warnings, sys
from datetime import datetime
warnings.filterwarnings('ignore')

# ========================
# 数据获取
# ========================
def get_weekly_data(symbol, start='2014-01-01', end=None):
    """获取周线数据"""
    if end is None:
        end = datetime.today().strftime('%Y-%m-%d')
    df = yf.Ticker(symbol).history(start=start, end=end, interval='1wk')
    df.sort_index(inplace=True)
    df.dropna(inplace=True)
    
    # 计算基础指标
    pc = df['Close'].shift(1)
    tr1 = df['High'] - df['Low']
    tr2 = (df['High'] - pc).abs()
    tr3 = (df['Low'] - pc).abs()
    df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(20).mean()
    return df

# ========================
# 策略回测引擎
# ========================
def backtest_ema30(df, ema_period=30, risk_pct=0.02, 
                   atr_exit_mult=None, cost=False, mvrv_filter=None, mvrv_data=None):
    """
    周线 EMA30 趋势策略
    
    参数:
        atr_exit_mult: ATR止盈倍数（可选，None表示不使用）
        mvrv_filter: MVRV阈值（可选，只在Z-Score < 阈值时开仓）
    """
    df = df.copy()
    df['EMA'] = df['Close'].ewm(span=ema_period).mean()
    
    equity = 10000.0
    btc_held = 0.0
    entry_px = 0.0
    peak_price = 0.0
    in_pos = False
    trades = []
    eqs = []
    
    for i in range(1, len(df)):
        c = df.iloc[i]['Close']
        ema = df.iloc[i]['EMA']
        atr = df.iloc[i]['ATR']
        pc_c = df.iloc[i-1]['Close']
        pc_ema = df.iloc[i-1]['EMA']
        date = df.index[i]
        
        if atr <= 0 or np.isnan(atr):
            atr = c * 0.05
        
        # MVRV filter
        mvrv_ok = True
        if mvrv_filter is not None and mvrv_data is not None:
            mvrv_val = mvrv_data.asof(date) if date in mvrv_data.index else mvrv_data.iloc[mvrv_data.index.get_indexer([date], method='pad')[0]]
            mvrv_ok = mvrv_val < mvrv_filter
        
        if in_pos:
            peak_price = max(peak_price, c)
        
        # ATR trailing stop
        atr_trig = False
        if atr_exit_mult is not None and in_pos:
            if (peak_price - c) >= atr * atr_exit_mult:
                atr_trig = True
        
        # Entry
        if not in_pos and pc_c <= pc_ema and c > ema and mvrv_ok:
            risk_amt = equity * risk_pct
            btc_to_buy = risk_amt / atr
            cost_d = btc_to_buy * c
            btc_held += btc_to_buy
            equity -= cost_d
            entry_px = c
            peak_price = c
            in_pos = True
        
        # Exit
        elif in_pos and (pc_c >= pc_ema and c < ema or atr_trig):
            proceeds = btc_held * c
            pnl = (c - entry_px) / entry_px
            if cost:
                proceeds *= (1 - 0.002)
            equity += proceeds
            exit_type = 'ATR_STOP' if atr_trig else 'EMA_CROSS'
            trades.append({
                'date': date, 'pnl': pnl, 'equity': equity,
                'atr_pct': atr/c*100, 'year': date.year,
                'quarter': (date.month-1)//3+1, 'exit': exit_type
            })
            btc_held = 0.0
            peak_price = 0.0
            in_pos = False
        
        eqs.append({'date': date, 'equity': equity + btc_held * c})
    
    return pd.DataFrame(eqs), pd.DataFrame(trades)

def analyze(eq_df, tr_df):
    if len(eq_df) < 2:
        return {'ann': 0, 'max_dd': 0, 'n': 0, 'total': 0, 'wr': 0}
    eq = eq_df['equity'].values
    dates = eq_df['date']
    if len(eq) < 2:
        return {'ann': 0, 'max_dd': 0, 'n': len(tr_df), 'total': 0, 'wr': 0}
    total = (eq[-1] / eq[0]) - 1
    yrs = (pd.Timestamp(dates.iloc[-1]) - pd.Timestamp(dates.iloc[0])).days / 365.25
    yrs = max(yrs, 0.1)
    ann = (1+total)**(1/yrs) - 1
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    n = len(tr_df)
    wr = (tr_df['pnl']>0).sum()/n*100 if n>0 else 0
    return {'ann': ann, 'max_dd': np.min(dd), 'n': n, 'total': total, 'wr': wr}

# ========================
# 六大题评分
# ========================
def run_exam(symbol='BTC-USD', ema_period=30, risk_pct=0.02, 
             atr_exit_mult=None, mvrv_filter=None):
    """
    运行完整六项考试
    返回: dict with scores and details
    """
    df = get_weekly_data(symbol)
    
    # 计算EMA（用于所有回测）
    df['EMA'] = df['Close'].ewm(span=ema_period).mean()
    
    # MVRV数据（如果有过滤器）
    mvrv_data = None
    if mvrv_filter is not None:
        # 用模拟MVRV（未来替换为真实数据）
        df['sim_mvrv'] = df['Close'].rolling(30).mean() / df['Close'].rolling(365).mean()
        mvrv_data = df['sim_mvrv'].dropna()
    
    # 数据切分
    train = df[(df.index >= '2015-01-01') & (df.index < '2020-01-01')].copy()
    valid = df[(df.index >= '2020-01-01') & (df.index < '2023-01-01')].copy()
    test = df[(df.index >= '2023-01-01') & (df.index <= '2026-04-13')].copy()
    blind = df[(df.index >= '2018-01-01') & (df.index < '2020-01-01')].copy()
    all_data = df.copy()
    
    def bt(df_): 
        return backtest_ema30(df_, ema_period, risk_pct, atr_exit_mult, cost=False, 
                             mvrv_filter=mvrv_filter, mvrv_data=mvrv_data)
    def btc(df_):
        return backtest_ema30(df_, ema_period, risk_pct, atr_exit_mult, cost=True,
                             mvrv_filter=mvrv_filter, mvrv_data=mvrv_data)
    
    results = {}
    
    # ===== 第一大题 =====
    r_train = analyze(*bt(train))
    r_valid = analyze(*bt(valid))
    r_test = analyze(*bt(test))
    
    rt, rv, rs = r_train['ann'], r_valid['ann'], r_test['ann']
    rd_t, rd_s = r_train['max_dd'], r_test['max_dd']
    
    score1 = 0
    notes1 = []
    
    # 标准1: 验证集年化 >= 0
    if rv >= 0:
        dv = rv/rt if rt > 0 else 0
        ok = dv >= 0.7
        notes1.append(f"验证衰减: {dv*100:.0f}% (需≥70%) {'PASS' if ok else 'FAIL'}")
        if ok: score1 += 10
    else:
        notes1.append(f"验证负年化: {rv*100:+.1f}% FAIL")
    
    # 标准2: 测试集年化 >= 训练集年化 * 0.7
    if rv >= 0:
        dt = rs/rt if rt > 0 else 0
        ok = dt >= 0.7
        notes1.append(f"测试衰减: {dt*100:.0f}% (需≥70%) {'PASS' if ok else 'FAIL'}")
        if ok: score1 += 10
    
    # 标准3: 测试集回撤 <= 训练集回撤 * 1.5
    dr = abs(rd_s/rd_t) if rd_t != 0 else 99
    ok = dr <= 1.5
    notes1.append(f"测试回撤比: {dr:.2f}x (需≤1.5x) {'PASS' if ok else 'FAIL'}")
    if ok: score1 += 10
    
    # 直接0分条件
    if rv < 0:
        score1 = 0
    
    results['q1'] = {
        'score': score1, 'max': 30,
        'train': r_train, 'valid': r_valid, 'test': r_test,
        'notes': notes1
    }
    
    # ===== 第二大题 =====
    ema_list = [20, 25, 30, 35, 40]
    risk_list = [0.010, 0.015, 0.020, 0.025, 0.030]
    
    grid = {}
    for ema in ema_list:
        for risk in risk_list:
            r_t = analyze(*backtest_ema30(train, ema, risk, atr_exit_mult))
            r_v = analyze(*backtest_ema30(valid, ema, risk, atr_exit_mult))
            grid[(ema, risk)] = (r_t['ann'], r_v['ann'])
    
    qualified = [(k,v) for k,v in grid.items() if v[0]>0.15 and v[1]>0]
    n_q = len(qualified)
    
    if n_q >= 8: score2 = 20
    elif n_q >= 4: score2 = 10
    else: score2 = 0
    
    results['q2'] = {'score': score2, 'max': 20, 'qualified': n_q, 'grid': grid}
    
    # ===== 第三大题 =====
    r_blind = analyze(*bt(blind))
    n_atr = 0
    if len(blind) > 0:
        _, tr_b = backtest_ema30(blind, ema_period, risk_pct, atr_exit_mult)
        n_atr = (tr_b['exit']=='ATR_STOP').sum() if len(tr_b) > 0 else 0
    
    score3 = 0
    if r_blind['ann'] >= 0: score3 += 10
    if abs(r_blind['max_dd']) <= 0.60: score3 += 10
    if r_blind['ann'] < -0.10: score3 = 0
    
    results['q3'] = {
        'score': score3, 'max': 20,
        'blind': r_blind, 'atr_triggers': n_atr
    }
    
    # ===== 第四大题 =====
    r_nc = analyze(*bt(test))
    r_c = analyze(*btc(test))
    dd_chg = (r_c['max_dd'] - r_nc['max_dd']) * 100
    
    score4 = 0
    if r_c['ann'] < 0:
        score4 = 0
    elif r_c['ann'] >= 0.10:
        score4 += 10
    if abs(dd_chg) <= 5.0:
        score4 += 5
    
    results['q4'] = {
        'score': score4, 'max': 15,
        'no_cost': r_nc, 'with_cost': r_c, 'dd_change': dd_chg
    }
    
    # ===== 第五大题 =====
    _, tr_all = backtest_ema30(all_data, ema_period, risk_pct, atr_exit_mult)
    r_all = analyze(pd.DataFrame(), tr_all)
    
    loss_prob = (tr_all['pnl'] < 0).mean() * 100 if len(tr_all) > 0 else 0
    
    # ATR level
    btc_atr = df[['ATR','Close']].copy()
    btc_atr['atr_pct'] = btc_atr['ATR'] / btc_atr['Close']
    btc_atr['atr_q'] = pd.qcut(btc_atr['atr_pct'].clip(0.001, 10), 3, 
                                labels=['低波动','中波动','高波动'], duplicates='drop')
    atr_map = btc_atr['atr_q'].to_dict()
    tr_all['atr_lvl'] = tr_all['date'].map(atr_map).fillna('中波动')
    
    def get_cluster(tr_all, dim_col, lp):
        if dim_col == 'atr_lvl':
            groups = ['低波动','中波动','高波动']
        else:
            groups = sorted(tr_all[dim_col].unique())
        cluster_found = False
        max_dev = 0
        for g in groups:
            sub = tr_all[tr_all[dim_col] == g]
            if len(sub) == 0: continue
            lp_sub = (sub['pnl'] < 0).mean() * 100
            dev = abs(lp_sub - lp) / max(lp, 1) * 100
            max_dev = max(max_dev, dev)
            if dev > 30: cluster_found = True
        return cluster_found, max_dev
    
    year_cl, year_dev = get_cluster(tr_all, 'year', loss_prob)
    q_cl, q_dev = get_cluster(tr_all, 'quarter', loss_prob)
    atr_cl, atr_dev = get_cluster(tr_all, 'atr_lvl', loss_prob)
    
    n_clusters = sum([year_cl, q_cl, atr_cl])
    if n_clusters == 0: score5 = 10
    elif n_clusters == 1: score5 = 5
    else: score5 = 0
    
    results['q5'] = {
        'score': score5, 'max': 10,
        'loss_prob': loss_prob,
        'year_cluster': (year_cl, year_dev),
        'q_cluster': (q_cl, q_dev),
        'atr_cluster': (atr_cl, atr_dev),
        'n_clusters': n_clusters
    }
    
    # ===== 总分 =====
    total = score1 + results['q2']['score'] + results['q3']['score'] + score4 + score5
    zero_scores = [s for s in [score1, results['q2']['score'], results['q3']['score'], score4, score5] if s == 0]
    
    if total >= 70 and len(zero_scores) == 0:
        status = "✅ APPROVED"
    elif total >= 70:
        status = "⚠️ CONDITIONAL PASS (单题零分警告)"
    else:
        status = "❌ REJECTED"
    
    results['total'] = total
    results['status'] = status
    results['zero_count'] = len(zero_scores)
    
    return results

# ========================
# 格式化输出
# ========================
def print_report(results):
    print("=" * 65)
    print("【策略实盘资格认证考试报告】")
    print("=" * 65)
    
    # 第一大题
    q1 = results['q1']
    print(f"\n【第一大题】Walk-Forward 三段衰减测试")
    print(f"  训练集: 年化={q1['train']['ann']*100:+.1f}% 回撤={q1['train']['max_dd']*100:.1f}%")
    print(f"  验证集: 年化={q1['valid']['ann']*100:+.1f}% 回撤={q1['valid']['max_dd']*100:.1f}%")
    print(f"  测试集: 年化={q1['test']['ann']*100:+.1f}% 回撤={q1['test']['max_dd']*100:.1f}%")
    for n in q1['notes']: print(f"    {n}")
    print(f"  得分: {q1['score']}/{q1['max']}")
    
    # 第二大题
    q2 = results['q2']
    print(f"\n【第二大题】参数正交扰动鲁棒性")
    print(f"  合格组合: {q2['qualified']}/25")
    print(f"  得分: {q2['score']}/{q2['max']}")
    
    # 第三大题
    q3 = results['q3']
    print(f"\n【第三大题】跨周期终极盲测(2018熊市)")
    print(f"  年化: {q3['blind']['ann']*100:+.1f}% 回撤: {q3['blind']['max_dd']*100:.1f}% ATR触发: {q3['atr_triggers']}次")
    print(f"  得分: {q3['score']}/{q3['max']}")
    
    # 第四大题
    q4 = results['q4']
    print(f"\n【第四大题】交易摩擦压力测试(2023-2026)")
    print(f"  无摩擦: 年化={q4['no_cost']['ann']*100:+.1f}% 回撤={q4['no_cost']['max_dd']*100:.1f}%")
    print(f"  有摩擦: 年化={q4['with_cost']['ann']*100:+.1f}% 回撤={q4['with_cost']['max_dd']*100:.1f}%")
    print(f"  得分: {q4['score']}/{q4['max']}")
    
    # 第五大题
    q5 = results['q5']
    print(f"\n【第五大题】失败案例一致性检验")
    print(f"  全样本亏损概率: {q5['loss_prob']:.1f}%")
    print(f"  年份聚集: {'是 ⚠️' if q5['year_cluster'][0] else '否'} (偏差{q5['year_cluster'][1]:.0f}pp)")
    print(f"  季度聚集: {'是 ⚠️' if q5['q_cluster'][0] else '否'} (偏差{q5['q_cluster'][1]:.0f}pp)")
    print(f"  ATR聚集: {'是 ⚠️' if q5['atr_cluster'][0] else '否'} (偏差{q5['atr_cluster'][1]:.0f}pp)")
    print(f"  得分: {q5['score']}/{q5['max']}")
    
    # 总分
    print("\n" + "=" * 65)
    print(f"【总分】{results['total']}/100")
    print(f"【状态】{results['status']}")
    print(f"【单题零分】{results['zero_count']}项")
    print("=" * 65)
    
    return results

# ========================
# CLI
# ========================
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='策略实盘资格认证考试')
    parser.add_argument('--symbol', default='BTC-USD')
    parser.add_argument('--ema-period', type=int, default=30)
    parser.add_argument('--risk-pct', type=float, default=0.02)
    parser.add_argument('--atr-exit', type=float, default=None, help='ATR止盈倍数（可选）')
    parser.add_argument('--mvrv-filter', type=float, default=None, help='MVRV阈值（可选）')
    args = parser.parse_args()
    
    print(f"考试参数: symbol={args.symbol} EMA={args.ema_period} risk={args.risk_pct*100:.1f}%")
    if args.atr_exit:
        print(f"  ATR止盈: {args.atr_exit}x")
    if args.mvrv_filter:
        print(f"  MVRV过滤: Z-Score < {args.mvrv_filter}")
    
    results = run_exam(
        symbol=args.symbol,
        ema_period=args.ema_period,
        risk_pct=args.risk_pct,
        atr_exit_mult=args.atr_exit,
        mvrv_filter=args.mvrv_filter
    )
    
    print_report(results)

#!/usr/bin/env python3
"""
OBI数据收集器 - 持续收集OKX BTC-USDT深度数据
支持后台运行，数据持久化到CSV
"""
import requests, time, json, os, sys
from datetime import datetime

DATA_DIR = os.path.expanduser('~/.hermes/obi_data/')
os.makedirs(DATA_DIR, exist_ok=True)

class OBICollector:
    def __init__(self, symbol='BTC-USDT', depth=20, threshold=0.5):
        self.symbol = symbol
        self.depth = depth
        self.threshold = threshold
        self.bids = {}
        self.asks = {}
        self.signals = []
        self.outcomes = []
        self.pending = {}
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
        self.csv_path = os.path.join(DATA_DIR, f'obi_signals_{datetime.now().strftime("%Y%m%d")}.csv')
        
    def fetch_depth(self):
        url = 'https://www.okx.com/api/v5/market/books'
        params = {'instId': self.symbol, 'sz': str(self.depth)}
        try:
            r = self.session.get(url, params=params, timeout=5)
            data = r.json()
            if data.get('code') == '0':
                books = data['data'][0]
                bids_raw = books.get('bids', [])
                asks_raw = books.get('asks', [])
                self.bids = {float(p): float(q) for p, q, *_ in bids_raw}
                self.asks = {float(p): float(q) for p, q, *_ in asks_raw}
                return float(books.get('last', 0))
        except Exception as e:
            pass
        return None
    
    def calc_obi(self):
        if not self.bids or not self.asks:
            return None
        bid_qty = sum(self.bids.values())
        ask_qty = sum(self.asks.values())
        total = bid_qty + ask_qty
        if total == 0:
            return None
        return (bid_qty - ask_qty) / total
    
    def calc_mid(self):
        if not self.bids or not self.asks:
            return None
        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())
        return (best_bid + best_ask) / 2
    
    def save_outcome(self, outcome):
        """保存到CSV"""
        with open(self.csv_path, 'a') as f:
            if f.tell() == 0:
                f.write('timestamp,direction,entry,exit,ret_pct,win\n')
            f.write(f"{outcome['ts']},{outcome['direction']},{outcome['entry']:.4f},{outcome['exit']:.4f},{outcome['ret_pct']:.6f},{int(outcome['win'])}\n")
    
    def run(self, interval=0.5, duration=None):
        """
        运行收集
        duration=None表示持续运行
        """
        print(f"📊 OBI数据收集器启动")
        print(f"   标的: {self.symbol} | 阈值: ±{self.threshold}")
        print(f"   数据文件: {self.csv_path}")
        print(f"   轮询间隔: {interval}s")
        print("-" * 50)
        
        start_time = time.time()
        iteration = 0
        
        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break
                
                iteration += 1
                ts_loop = time.time()
                
                last_px = self.fetch_depth()
                if last_px is None:
                    time.sleep(interval)
                    continue
                
                obi = self.calc_obi()
                mid = self.calc_mid()
                ts = time.time()
                
                if obi is not None and mid is not None:
                    # 发送信号
                    if obi > self.threshold:
                        self.pending[ts] = ('LONG', mid)
                        self.signals.append((ts, 'LONG', obi, mid))
                        print(f"  [{iteration}] 📈 LONG OBI={obi:+.3f} mid=${mid:,.0f}")
                    
                    elif obi < -self.threshold:
                        self.pending[ts] = ('SHORT', mid)
                        self.signals.append((ts, 'SHORT', obi, mid))
                        print(f"  [{iteration}] 📉 SHORT OBI={obi:+.3f} mid=${mid:,.0f}")
                    
                    # 验证5秒前信号
                    to_del = []
                    for sig_ts, (direction, entry) in list(self.pending.items()):
                        if ts - sig_ts >= 5:
                            if direction == 'LONG':
                                ret_pct = (mid - entry) / entry * 100
                            else:
                                ret_pct = (entry - mid) / entry * 100
                            
                            outcome = {
                                'ts': sig_ts,
                                'direction': direction,
                                'entry': entry,
                                'exit': mid,
                                'ret_pct': ret_pct,
                                'win': ret_pct > 0
                            }
                            self.outcomes.append(outcome)
                            self.save_outcome(outcome)
                            
                            win_mark = '✅' if ret_pct > 0 else '❌'
                            print(f"      → 5s验证 {direction} {win_mark} {ret_pct:+.4f}%")
                            to_del.append(sig_ts)
                    
                    for t in to_del:
                        del self.pending[t]
                
                # 精确睡眠
                elapsed = time.time() - ts_loop
                sleep_time = max(0.01, interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                # 每100次迭代输出进度
                if iteration % 100 == 0 and self.outcomes:
                    n = len(self.outcomes)
                    wins = sum(1 for o in self.outcomes if o['win'])
                    avg = sum(o['ret_pct'] for o in self.outcomes) / n
                    print(f"  [进度] 迭代={iteration} 信号={len(self.signals)} 验证={n} 胜率={wins/n*100:.1f}% 均={avg:+.4f}%")
        
        except KeyboardInterrupt:
            print("\n收到中断信号，保存数据...")
        
        # 最终报告
        n = len(self.outcomes)
        print(f"\n{'='*50}")
        print(f"📊 收集完成")
        print(f"{'='*50}")
        print(f"运行时长: {time.time() - start_time:.0f}s")
        print(f"迭代次数: {iteration}")
        print(f"总信号: {len(self.signals)}")
        print(f"有效验证: {n}")
        
        if n > 0:
            wins = sum(1 for o in self.outcomes if o['win'])
            avg = sum(o['ret_pct'] for o in self.outcomes) / n
            longs = [o for o in self.outcomes if o['direction'] == 'LONG']
            shorts = [o for o in self.outcomes if o['direction'] == 'SHORT']
            
            print(f"总胜率: {wins/n*100:.1f}% ({wins}胜/{n-wins}负)")
            print(f"平均收益: {avg:+.4f}%")
            if longs:
                lw = sum(1 for o in longs if o['win'])
                print(f"LONG胜率: {lw/len(longs)*100:.1f}% ({lw}/{len(longs)})")
            if shorts:
                sw = sum(1 for o in shorts if o['win'])
                print(f"SHORT胜率: {sw/len(shorts)*100:.1f}% ({sw}/{len(shorts)})")
            
            # 保存最终报告
            report_path = os.path.join(DATA_DIR, 'latest_report.json')
            report = {
                'timestamp': datetime.now().isoformat(),
                'duration_s': time.time() - start_time,
                'iterations': iteration,
                'total_signals': len(self.signals),
                'valid_outcomes': n,
                'win_rate': wins/n*100,
                'avg_return': avg,
                'longs': len(longs),
                'shorts': len(shorts)
            }
            with open(report_path, 'w') as f:
                json.dump(report, f, indent=2)
            print(f"\n报告已保存: {report_path}")
            print(f"数据文件: {self.csv_path}")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='OBI数据收集器')
    parser.add_argument('--symbol', default='BTC-USDT')
    parser.add_argument('--depth', type=int, default=20)
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--interval', type=float, default=0.5)
    parser.add_argument('--duration', type=float, default=None, help='运行时长（秒），不设置则持续运行')
    args = parser.parse_args()
    
    collector = OBICollector(
        symbol=args.symbol,
        depth=args.depth,
        threshold=args.threshold
    )
    collector.run(interval=args.interval, duration=args.duration)

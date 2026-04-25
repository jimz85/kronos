#!/usr/bin/env python3
"""
跨交易所BTC套利扫描器
Binance vs OKX 实时价差监控
"""
import requests, time, json
from datetime import datetime

class ArbitrageScanner:
    """
    监控 Binance 和 OKX 的BTC/USDT价差
    
    计算维度:
    1. 成交价价差: (Binance_last - OKX_last) / Binance_last
    2. 买入价差(在OKX买,在Binance卖): (Binance_bid - OKX_ask) / Binance_bid
    3. 卖出价差(在Binance买,在OKX卖): (OKX_bid - Binance_ask) / OKX_bid
    """
    
    def __init__(self):
        self.binance_ticker_url = 'https://api.binance.com/api/v3/ticker/price'
        self.binance_depth_url = 'https://api.binance.com/api/v3/depth'
        self.okx_books_url = 'https://www.okx.com/api/v5/market/books'
        
        self.spreads = []  # (timestamp, spread_type, spread_pct, price_bnb, price_okx)
        self.thresholds = [0.05, 0.10, 0.15, 0.20, 0.30]  # 阈值列表
        self.threshold_counts = {t: 0 for t in self.thresholds}  # 每次超过阈值计数
        self.last_prices = []  # (ts, b_price, o_price, spread_pct)
        self.running = False
    
    def get_binance(self):
        """获取Binance数据"""
        try:
            # ticker
            r_ticker = requests.get(self.binance_ticker_url, params={'symbol': 'BTCUSDT'}, timeout=5)
            ticker = r_ticker.json()
            last_price = float(ticker['price'])
            
            # depth (买一卖一)
            r_depth = requests.get(self.binance_depth_url, params={'symbol': 'BTCUSDT', 'limit': 1}, timeout=5)
            depth = r_depth.json()
            bid = float(depth['bids'][0][0])
            ask = float(depth['asks'][0][0])
            
            return {'last': last_price, 'bid': bid, 'ask': ask}
        except Exception as e:
            return None
    
    def get_okx(self):
        """获取OKX数据"""
        try:
            r = requests.get(self.okx_books_url, params={'instId': 'BTC-USDT', 'sz': '1'}, timeout=5)
            data = r.json()
            if data.get('code') != '0':
                return None
            books = data['data'][0]
            ask = float(books['asks'][0][0])  # 卖一价(你买入的价格)
            bid = float(books['bids'][0][0])  # 买一价(你卖出的价格)
            
            return {'bid': bid, 'ask': ask}
        except Exception as e:
            return None
    
    def run(self, duration=3600, interval=1):
        """
        运行扫描
        duration: 秒（默认1小时）
        interval: 轮询间隔（秒）
        """
        self.running = True
        start = time.time()
        start_dt = datetime.now().strftime('%H:%M:%S')
        
        print(f"\n{'='*65}")
        print(f"跨交易所BTC套利扫描 | {start_dt} 开始 | 运行时长={duration}秒")
        print(f"{'='*65}")
        print(f"{'时间':>8} {'BNB最后':>12} {'OKX最后':>12} {'价差':>10} {'OKX买/BNB卖':>14} {'BNX买/OKX卖':>14}")
        print(f"{'-'*65}")
        
        consecutive_errors = 0
        
        while self.running and (time.time() - start) < duration:
            elapsed = time.time() - start
            bnb = self.get_binance()
            okx = self.get_okx()
            
            if bnb is None or okx is None:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    print(f"[{elapsed:.0f}s] 连续获取失败，退出")
                    break
                time.sleep(interval)
                continue
            
            consecutive_errors = 0
            
            b_last, b_bid, b_ask = bnb['last'], bnb['bid'], bnb['ask']
            o_bid, o_ask = okx['bid'], okx['ask']
            
            # 计算各种价差
            # 1. 成交价价差
            last_spread = (b_last - o_bid) / b_last * 100  # 用OKX的bid作为OKX的"当前价格"参考
            
            # 2. OKX买 / Binance卖 的机会 (Binance卖价 > OKX买价 = 你在OKX买入,在Binance卖出)
            # 在OKX买入(支付ask), 在Binance卖出(收取bid)
            oki_buy_bnb_sell = (b_bid - o_ask) / b_bid * 100
            
            # 3. Binance买 / OKX卖 的机会 (OKX卖价 > Binance买价 = 你在Binance买入,在OKX卖出)
            bnb_buy_oki_sell = (o_bid - b_ask) / o_bid * 100
            
            ts = time.time()
            self.last_prices.append({
                'ts': ts,
                'elapsed': elapsed,
                'b_last': b_last, 'o_bid': o_bid, 'o_ask': o_ask,
                'b_bid': b_bid, 'b_ask': b_ask,
                'last_spread': last_spread,
                'oki_buy_bnb_sell': oki_buy_bnb_sell,
                'bnb_buy_oki_sell': bnb_buy_oki_sell
            })
            
            # 超过阈值计数
            for t in self.thresholds:
                if abs(oki_buy_bnb_sell) > t:
                    self.threshold_counts[t] += 1
                if abs(bnb_buy_oki_sell) > t:
                    self.threshold_counts[t] += 1
            
            # 每10秒打印一次
            if len(self.last_prices) % 10 == 0:
                print(f"{elapsed:>7.0f}s {b_last:>12,.0f} {o_bid:>12,.0f} {last_spread:>+9.3f}% {oki_buy_bnb_sell:>+13.3f}% {bnb_buy_oki_sell:>+13.3f}%")
            
            time.sleep(interval)
        
        self.running = False
        self.generate_report(start, duration)
    
    def generate_report(self, start, duration):
        """生成报告"""
        elapsed = time.time() - start
        n = len(self.last_prices)
        
        print(f"\n{'='*65}")
        print(f"📊 套利扫描报告 ({elapsed:.0f}秒, {n}个数据点)")
        print(f"{'='*65}")
        
        if n == 0:
            print("无有效数据")
            return
        
        # 提取各类型价差
        last_spreads = [p['last_spread'] for p in self.last_prices]
        dir1 = [p['oki_buy_bnb_sell'] for p in self.last_prices]  # OKX买/Binance卖
        dir2 = [p['bnb_buy_oki_sell'] for p in self.last_prices]  # Binance买/OKX卖
        
        # 基本统计
        print(f"\n成交价价差 (Binance_last - OKX_bid) / Binance_last:")
        print(f"  均值: {sum(last_spreads)/n:+.4f}%")
        print(f"  最大: {max(last_spreads):+.4f}%")
        print(f"  最小: {min(last_spreads):+.4f}%")
        
        # 方向1: OKX买 / Binance卖
        print(f"\n方向1: 在OKX买入(ask) → 在Binance卖出(bid)")
        print(f"  均值: {sum(dir1)/n:+.4f}%")
        print(f"  最大: {max(dir1):+.4f}%")
        print(f"  最小: {min(dir1):+.4f}%")
        opportunities_dir1 = [x for x in dir1 if x > 0]
        print(f"  正收益机会: {len(opportunities_dir1)}/{n} ({len(opportunities_dir1)/n*100:.1f}%)")
        print(f"  正收益均值: {sum(opportunities_dir1)/max(len(opportunities_dir1),1):+.4f}%")
        
        # 方向2: Binance买 / OKX卖
        print(f"\n方向2: 在Binance买入(ask) → 在OKX卖出(bid)")
        print(f"  均值: {sum(dir2)/n:+.4f}%")
        print(f"  最大: {max(dir2):+.4f}%")
        print(f"  最小: {min(dir2):+.4f}%")
        opportunities_dir2 = [x for x in dir2 if x > 0]
        print(f"  正收益机会: {len(opportunities_dir2)}/{n} ({len(opportunities_dir2)/n*100:.1f}%)")
        print(f"  正收益均值: {sum(opportunities_dir2)/max(len(opportunities_dir2),1):+.4f}%")
        
        # 阈值统计
        print(f"\n超过阈值的次数:")
        for t in self.thresholds:
            cnt = self.threshold_counts[t]
            print(f"  ±{t}%: {cnt}次 ({cnt/n*100:.1f}%)")
        
        # 手续费核算
        fee_bnb_taker = 0.001  # Binance 0.1%
        fee_oki_taker = 0.001  # OKX 0.1%
        total_fee = (fee_bnb_taker + fee_oki_taker) * 2  # 双向手续费*2(开平仓)
        withdrawal_fee_btc = 0.0001  # BTC提币费约$1 at $75000
        
        print(f"\n{'='*50}")
        print(f"💰 手续费核算")
        print(f"{'='*50}")
        print(f"双边Taker手续费: {total_fee*100:.2f}%")
        print(f"BTC提币费(估算): ~{withdrawal_fee_btc*100:.3f}% (按$75,000/BTC)")
        print(f"盈亏平衡所需最小价差: {total_fee*100 + withdrawal_fee_btc*100:.3f}%")
        
        breakeven = total_fee + withdrawal_fee_btc * (1/75000) * 75000 / 75000  # 简化
        breakeven_pct = total_fee * 100 + 0.01  # 约0.21%
        print(f"盈亏平衡点: ~{breakeven_pct:.2f}%")
        
        # 计算实际盈利机会
        profitable_dir1 = [x for x in dir1 if x > breakeven_pct]
        profitable_dir2 = [x for x in dir2 if x > breakeven_pct]
        total_profitable = len(profitable_dir1) + len(profitable_dir2)
        
        print(f"\n扣除手续费后盈利机会:")
        print(f"  方向1(OKX买/BNB卖): {len(profitable_dir1)}次")
        print(f"  方向2(BNB买/OKX卖): {len(profitable_dir2)}次")
        print(f"  总计: {total_profitable}次 / {n*2}次 ({total_profitable/max(n*2,1)*100:.2f}%)")
        
        if total_profitable > 0:
            all_profitable = profitable_dir1 + profitable_dir2
            print(f"  平均盈利: {sum(all_profitable)/len(all_profitable):+.4f}%")
        
        return {
            'n': n,
            'dir1_opportunities': len(profitable_dir1),
            'dir2_opportunities': len(profitable_dir2),
            'total_profitable': total_profitable,
            'breakeven_pct': breakeven_pct
        }
    
    def stop(self):
        self.running = False

if __name__ == '__main__':
    import sys
    dur = int(sys.argv[1]) if len(sys.argv) > 1 else 3600  # 默认1小时
    
    scanner = ArbitrageScanner()
    scanner.run(duration=dur, interval=1)

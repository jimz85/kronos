"""
Kronos价格预测模块
用法:
  from kronos_price import kronos_check, kronos_signal_label
  price, bias = kronos_check('BTC', hours=48)
"""
import os, time
import pandas as pd

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'

_MODEL = None
_TOKENIZER = None
_CACHE = {}  # {(coin, hour): (price, timestamp, bias)}


def _load_model():
    global _MODEL, _TOKENIZER
    if _MODEL is None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from model import Kronos, KronosTokenizer
            _TOKENIZER = KronosTokenizer.from_pretrained('models/tokenizer-base')
            _MODEL = Kronos.from_pretrained('models/kronos-small')
    return _MODEL, _TOKENIZER


def kronos_check(coin, hours=48):
    """
    返回 (current_price, bias_pct)
    bias_pct > 2 → 当前价高于预测 → 高估
    bias_pct < -2 → 当前价低于预测 → 低估
    """
    cache_key = (coin, hours)
    now = time.time()
    
    # 10分钟缓存
    if cache_key in _CACHE:
        cached_price, cached_ts, cached_bias = _CACHE[cache_key]
        if now - cached_ts < 600:
            return cached_price, cached_bias
    
    try:
        model, tokenizer = _load_model()
        from model import KronosPredictor
        predictor = KronosPredictor(model, tokenizer, max_context=512)
        
        fpath = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
        if not os.path.exists(fpath):
            return None, 0.0
        
        df = pd.read_csv(fpath)
        df['ts'] = pd.to_datetime(df.iloc[:, 0], errors='coerce')
        df = df.dropna(subset=['ts'])
        df = df.set_index('ts').sort_index()
        df_1h = df[['open','high','low','close']].resample('1h').agg(
            {'open':'first','high':'max','low':'min','close':'last'}).dropna().tail(400)
        
        if len(df_1h) < 100:
            return None, 0.0
        
        current = float(df_1h['close'].iloc[-1])
        x_ts = pd.Series(df_1h.index, index=df_1h.index)
        y_last = df_1h.index[-1]
        y_ts = pd.Series(
            [y_last + pd.Timedelta(hours=i) for i in range(1, hours+1)],
            index=[y_last + pd.Timedelta(hours=i) for i in range(1, hours+1)]
        )
        
        pred = predictor.predict(
            df=df_1h[['open','high','low','close']].reset_index(drop=True),
            x_timestamp=x_ts, y_timestamp=y_ts,
            pred_len=hours, T=1.0, top_p=0.9, sample_count=1, verbose=False
        )
        
        future_mean = float(pred['close'].mean())
        bias_pct = (future_mean - current) / current * 100
        
        _CACHE[cache_key] = (current, now, bias_pct)
        return current, bias_pct
        
    except Exception:
        return None, 0.0


def kronos_signal_label(bias_pct):
    """把bias_pct转成交易信号标签"""
    if bias_pct > 3:
        return "⚠️ 高估偏空"
    elif bias_pct > 1:
        return "📊 略高估"
    elif bias_pct < -3:
        return "💎 严重低估"
    elif bias_pct < -1:
        return "📈 略低估"
    return "⚖️ 中性"

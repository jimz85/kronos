#!/bin/bash
LOG_FILE="/tmp/okx_trade_log.json"
TRADE_COOLDOWN=1800
MIN_CONFIDENCE=70

cd ~/kronos

RESULT=$(python3 okx_trading_engine.py 2>&1)

# 只解析15m决策信号（>>> 标记）
DIR=$(echo "$RESULT" | grep ">>>" | grep -oE "LONG|SHORT|NEUTRAL" | head -1)
CONF=$(echo "$RESULT" | grep ">>>" | grep -oE "[0-9]+%" | head -1 | tr -d '%')
PRICE=$(echo "$RESULT" | grep "BTC价格:" | grep -oE "[0-9,]+\.[0-9]+" | head -1 | tr -d ',')

echo "[$(date '+%H:%M:%S')] 方向=$DIR 信心=${CONF}%"

if [ "$DIR" != "LONG" ] && [ "$DIR" != "SHORT" ]; then
    echo "⏸ 15m无明确方向，等待机会"
    exit 0
fi

if [ "${CONF:-0}" -lt "$MIN_CONFIDENCE" ]; then
    echo "⏸ 信心不足 (${CONF}% < ${MIN_CONFIDENCE}%)"
    exit 0
fi

# 冷却期
if [ -f "$LOG_FILE" ]; then
    LAST_TIME=$(python3 -c "import json; print(json.load(open('$LOG_FILE')).get('last_trade_time',0))" 2>/dev/null)
    LAST_SIDE=$(python3 -c "import json; print(json.load(open('$LOG_FILE')).get('last_side',''))" 2>/dev/null)
    if [ -n "$LAST_TIME" ] && [ -n "$LAST_SIDE" ]; then
        ELAPSED=$(($(date +%s) - LAST_TIME))
        if [ "$LAST_SIDE" = "$DIR" ] && [ "$ELAPSED" -lt "$TRADE_COOLDOWN" ]; then
            echo "⏸ 同向冷却中 (${DIR}, $((TRADE_COOLDOWN - ELAPSED))秒)"
            exit 0
        fi
    fi
fi

echo "🔥 执行 $DIR (信心${CONF}%)..."
TRADE_OUT=$(python3 okx_trading_engine.py --trade 2>&1)
echo "$TRADE_OUT"

python3 -c "
import json
open('$LOG_FILE','w').write(json.dumps({'last_trade_time':$(date +%s),'last_side':'$DIR','price':'$PRICE','confidence':'${CONF}'}))
" 2>/dev/null

#!/bin/bash
# 多币种自动交易 - 仅在交易事件时通知飞书
LOG_FILE="/tmp/okx_multi_trade_log.json"
COOL_FILE="/tmp/okx_position_state.json"
COOLDOWN=1800
MIN_CONF=70

cd ~/kronos

# ========== 1. 检查是否有持仓被止损/止盈 ==========
check_closed_position() {
    if [ ! -f "$COOL_FILE" ]; then return; fi

    PREV=$(cat "$COOL_FILE")
    PREV_SIDE=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('side',''))" 2>/dev/null)
    PREV_COIN=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('coin',''))" 2>/dev/null)
    PREV_SZ=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('size',''))" 2>/dev/null)
    PREV_ENTRY=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('entry',''))" 2>/dev/null)
    PREV_SL=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sl',''))" 2>/dev/null)
    PREV_TP=$(echo "$PREV" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tp',''))" 2>/dev/null)

    if [ -z "$PREV_COIN" ] || [ -z "$PREV_SIDE" ]; then return; fi

    # 持仓还在，不处理
    CURRENT=$(python3 -c "
import requests, hashlib, hmac, base64, json
from datetime import datetime
API_KEY='8aba4de9-84ef-4632-9c96-f8ca6b92a237'
SECRET_KEY='850C974EE1A1C77851137AEB961FF630'
PASSPHRASE='Jmz123456!'
ts=datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
path='/api/v5/account/positions?instId=${PREV_COIN}'
msg=ts+'GET'+path
mac=hmac.new(SECRET_KEY.encode(),msg.encode(),hashlib.sha256)
h={'OK-ACCESS-KEY':API_KEY,'OK-ACCESS-SIGN':base64.b64encode(mac.digest()).decode(),'OK-ACCESS-TIMESTAMP':ts,'OK-ACCESS-PASSPHRASE':PASSPHRASE,'Content-Type':'application/json','x-simulated-trading':'1'}
r=requests.get('https://www.okx.com'+path,headers=h,timeout=10)
positions=r.json().get('data',[])
for p in positions:
    if float(p.get('pos',0))!=0:
        print(p.get('pos',''))
        break
" 2>/dev/null)

    if [ -n "$CURRENT" ] && [ "$CURRENT" != "None" ]; then
        return  # 持仓还在
    fi

    # 持仓没了 → 触发止损/止盈/手动平仓
    # 读取当前价格判断
    CUR_PRICE=$(python3 -c "
import requests
r=requests.get('https://www.okx.com/api/v5/market/ticker?instId=${PREV_COIN}',timeout=10)
print(r.json()['data'][0]['last'])
" 2>/dev/null)

    # 判断是SL还是TP还是手动
    TRIGGERED="手动平仓"
    if [ -n "$CUR_PRICE" ] && [ -n "$PREV_SL" ] && [ -n "$PREV_TP" ]; then
        if [ "$PREV_SIDE" = "LONG" ]; then
            SL_TRIG=$(python3 -c "print(1 if float('$CUR_PRICE') < float('$PREV_SL') else 0)" 2>/dev/null)
            TP_TRIG=$(python3 -c "print(1 if float('$CUR_PRICE') > float('$PREV_TP') else 0)" 2>/dev/null)
        else
            SL_TRIG=$(python3 -c "print(1 if float('$CUR_PRICE') > float('$PREV_SL') else 0)" 2>/dev/null)
            TP_TRIG=$(python3 -c "print(1 if float('$CUR_PRICE') < float('$PREV_TP') else 0)" 2>/dev/null)
        fi

        if [ "$SL_TRIG" = "1" ]; then
            TRIGGERED="止损"
            EMOJI="🛑"
            COLOR="红"
        elif [ "$TP_TRIG" = "1" ]; then
            TRIGGERED="止盈"
            EMOJI="🎯"
            COLOR="绿"
        else
            TRIGGERED="手动平仓"
            EMOJI="📋"
            COLOR="灰"
        fi
    fi

    # 计算盈亏
    if [ -n "$CUR_PRICE" ] && [ -n "$PREV_ENTRY" ]; then
        if [ "$PREV_SIDE" = "LONG" ]; then
            PNL=$(python3 -c "print(round((float('$CUR_PRICE')-float('$PREV_ENTRY'))/float('$PREV_ENTRY')*100,2))")
        else
            PNL=$(python3 -c "print(round((float('$PREV_ENTRY')-float('$CUR_PRICE'))/float('$PREV_ENTRY')*100,2))")
        fi
        PNL_STR="${PNL}%"
    else
        PNL_STR="?"
    fi

    # 发送飞书
    SIDE_CN="做多"
    [ "$PREV_SIDE" = "SHORT" ] && SIDE_CN="做空"

    MSG="${EMOJI} ${TRIGGERED}通知

📌 币种: ${PREV_COIN}
📌 方向: ${SIDE_CN}
📌 触发价格: \$${CUR_PRICE}
📌 止损: \$${PREV_SL}
📌 止盈: \$${PREV_TP}
📌 收益率: ${PNL_STR}

⏰ $(date '+%Y-%m-%d %H:%M:%S')"

    python3 -c "
import urllib.request, json
url='https://open.feishu.cn/open-apis/bot/v2/hook/oc_bfd8a7cc1a606f190b53e3fd0167f5a0'
payload={'msg_type':'text','content':{'text':'''$MSG'''}}
req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
urllib.request.urlopen(req,timeout=10)
print('FEISHU_SENT')
" 2>/dev/null

    # 清除状态
    rm -f "$COOL_FILE"
}

# ========== 2. 执行新的交易 ==========
RESULT=$(python3 okx_multi_engine.py 2>&1)

DIR=$(echo "$RESULT" | grep "信号:" | grep -v "多币种\|最优" | grep -oE "LONG|SHORT|NEUTRAL" | head -1)
CONF=$(echo "$RESULT" | grep "信号:" | grep -v "多币种\|最优" | grep -oE "[0-9]+%" | head -1 | tr -d '%')
NOW_S=$(date +%s)

if [ "$DIR" != "LONG" ] && [ "$DIR" != "SHORT" ]; then
    exit 0
fi

if [ "${CONF:-0}" -lt "$MIN_CONF" ]; then
    exit 0
fi

# 冷却
if [ -f "$LOG_FILE" ]; then
    LAST_T=$(python3 -c "import json; print(json.load(open('$LOG_FILE')).get('last_time',0))" 2>/dev/null)
    LAST_S=$(python3 -c "import json; print(json.load(open('$LOG_FILE')).get('last_side',''))" 2>/dev/null)
    if [ -n "$LAST_T" ] && [ -n "$LAST_S" ]; then
        ELAPSED=$(($NOW_S - LAST_T))
        if [ "$LAST_S" = "$DIR" ] && [ $ELAPSED -lt $COOLDOWN ]; then
            exit 0
        fi
    fi
fi

TRADE=$(python3 okx_multi_engine.py --trade 2>&1)

if echo "$TRADE" | grep -q "✅"; then
    # 提取关键信息
    COIN=$(echo "$TRADE" | grep "执行" | grep -oE "BTC|ETH|SOL|DOGE|XRP|BNB" | head -1)
    LEV=$(echo "$TRADE" | grep "执行" | grep -oE "[0-9]+x" | head -1)
    PRICE=$(echo "$TRADE" | grep "执行" | grep -oE "\$[0-9,]+\.[0-9]+" | head -1)
    SL=$(echo "$TRADE" | grep "止损:" | grep -oE "\$[0-9,]+\.[0-9]+" | head -1)
    TP=$(echo "$TRADE" | grep "止盈:" | grep -oE "\$[0-9,]+\.[0-9]+" | head -1)

    SIDE_CN="做多"
    [ "$DIR" = "SHORT" ] && SIDE_CN="做空"

    MSG="🚨 自动开仓

📌 币种: ${COIN}
📌 方向: ${SIDE_CN}
📌 杠杆: ${LEV}
📌 开仓价: ${PRICE}
📌 止损: ${SL}
📌 止盈: ${TP}

⏰ $(date '+%Y-%m-%d %H:%M:%S')"

    python3 -c "
import urllib.request, json
url='https://open.feishu.cn/open-apis/bot/v2/hook/oc_bfd8a7cc1a606f190b53e3fd0167f5a0'
payload={'msg_type':'text','content':{'text':'''$MSG'''}}
req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
urllib.request.urlopen(req,timeout=10)
print('FEISHU_SENT')
" 2>/dev/null

    # 保存持仓状态
    python3 -c "
import json
open('$COOL_FILE','w').write(json.dumps({
    'side':'$DIR','coin':'${COIN}-USDT-SWAP',
    'sl':'$SL','tp':'$TP','entry':'$PRICE'
}))
" 2>/dev/null

    python3 -c "import json; open('$LOG_FILE','w').write(json.dumps({'last_time':$NOW_S,'last_side':'$DIR','confidence':'$CONF'}))" 2>/dev/null
fi

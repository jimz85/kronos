# Kronos API 密钥管理规范

## 1. 密钥类型

| 密钥类型 | 用途 | 存储位置 | 轮换周期 |
|---------|------|---------|---------|
| OKX API Key | 交易授权 | `~/.hermes/.env` | 90天 |
| OKX Secret | 签名密钥 | `~/.hermes/.env` | 90天 |
| OKX Passphrase | API密码 | `~/.hermes/.env` | 90天 |
| Feishu App ID | 飞书应用 | `~/.hermes/.env` | 180天 |
| Feishu App Secret | 飞书密钥 | `~/.hermes/.env` | 90天 |
| GitHub Token | 代码推送 | `~/.config/gh/hosts.yml` | 30天 |

## 2. 密钥安全原则

### 2.1 绝对禁止

```
❌ 硬编码密钥到代码中
❌ 提交密钥到GitHub仓库
❌ 通过邮件/聊天工具传输密钥
❌ 在日志中打印密钥
❌ 使用弱密码或默认密钥
```

### 2.2 正确做法

```
✅ 使用环境变量管理密钥
✅ 密钥文件权限设置为 600
✅ 定期轮换密钥
✅ 记录密钥使用日志
✅ 分离测试和生产密钥
```

## 3. OKX 密钥管理

### 3.1 创建新API密钥

1. 登录 [OKX](https://www.okx.com/)
2. 进入 **账户与安全** → **API管理**
3. 点击 **创建API密钥**
4. 填写信息：
   - 备注名称：`kronos-production`
   - 密钥类型：**交易密钥**
   - 绑定IP：`0.0.0.0/0` (或指定IP白名单)
5. 勾选权限：
   - ✅ 读取账户信息
   - ✅ 读取交易历史
   - ✅ 发起订单
   - ✅ 取消订单
   - ❌ 提币 (不要勾选!)
6. 完成人机验证
7. **立即保存密钥** (Secret只显示一次)

### 3.2 密钥权限配置

```
┌─────────────────────────────────────────────────────────┐
│                    OKX API 密钥权限                         │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ✅ 账户余额读取     (读取市场数据、账户余额)              │
│  ✅ 杠杆交易         (开仓、平仓)                         │
│  ✅ 合约订单         (下单、撤单)                         │
│  ✅ 条件单           (止损、止盈)                        │
│                                                          │
│  ❌ 资金划转         (禁止)                              │
│  ❌ 提币/充值        (禁止)                              │
│  ❌ 钱包/资金划转     (禁止)                              │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 3.3 IP白名单配置

```bash
# 推荐: 限制固定IP
0.0.0.0/0              # 允许所有IP (仅用于开发)
123.456.789.0/24         # 限制特定网段
123.456.789.123          # 限制固定IP (生产推荐)

# 查看当前服务器IP
curl -s ifconfig.me
```

## 4. 密钥轮换流程

### 4.1 定期轮换 (每90天)

```bash
#!/bin/bash
# scripts/rotate_okx_keys.sh
# OKX密钥轮换脚本

set -e

echo "=== OKX 密钥轮换流程 ==="
echo "警告: 此操作将生成新密钥，旧密钥将在24小时后失效"
echo ""

# 1. 确认当前环境
read -p "当前环境 (test/production): " ENV
if [[ "$ENV" != "test" && "$ENV" != "production" ]]; then
    echo "❌ 无效的环境"
    exit 1
fi

# 2. 备份当前密钥
echo "[1/5] 备份当前密钥..."
cp ~/.hermes/.env ~/.hermes/.env.backup.$(date +%Y%m%d)
echo "✅ 已备份到 ~/.hermes/.env.backup.$(date +%Y%m%d)"

# 3. 生成新密钥 (手动步骤)
echo ""
echo "[2/5] 请按以下步骤生成新密钥:"
echo "1. 登录 OKX"
echo "2. 进入 账户与安全 → API管理"
echo "3. 点击 创建API密钥"
echo "4. 填写信息并提交"
echo "5. 复制新的 API Key、Secret、Passphrase"
echo ""

# 4. 输入新密钥
read -p "新 API Key: " NEW_API_KEY
read -p "新 Secret: " NEW_SECRET
read -p "新 Passphrase: " NEW_PASSPHRASE

# 5. 验证密钥格式
if [[ ${#NEW_API_KEY} -lt 20 ]]; then
    echo "❌ API Key 格式无效"
    exit 1
fi

# 6. 更新环境变量
echo "[3/5] 更新环境变量..."
sed -i.bak \
    -e "s/OKX_API_KEY=.*/OKX_API_KEY=$NEW_API_KEY/" \
    -e "s/OKX_SECRET=.*/OKX_SECRET=$NEW_SECRET/" \
    -e "s/OKX_PASSPHRASE=.*/OKX_PASSPHRASE=$NEW_PASSPHRASE/" \
    ~/.hermes/.env

# 7. 测试新密钥
echo "[4/5] 测试新密钥..."
cd ~/kronos
python3 -c "
import os
from dotenv import load_dotenv
load_dotenv()
import requests

key = os.getenv('OKX_API_KEY')
secret = os.getenv('OKX_SECRET')
passphrase = os.getenv('OKX_PASSPHRASE')

if not all([key, secret, passphrase]):
    print('❌ 密钥环境变量未设置')
    exit(1)

# 测试API连通性
import hmac, hashlib, base64, datetime
ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
msg = f'{ts}GET/api/v5/account/balance'
sign = base64.b64encode(hmac.new(
    secret.encode(), msg.encode(), hashlib.sha256
)).decode()

headers = {
    'OK-ACCESS-KEY': key,
    'OK-ACCESS-SIGN': sign,
    'OK-ACCESS-TIMESTAMP': ts,
    'OK-ACCESS-PASSPHRASE': passphrase,
    'Content-Type': 'application/json',
}
r = requests.get('https://www.okx.com/api/v5/account/balance', headers=headers)
result = r.json()

if result.get('code') == '0':
    print('✅ 新密钥测试成功')
else:
    print(f'❌ 密钥测试失败: {result}')
    exit(1)
"

# 8. 记录轮换
echo "[5/5] 记录轮换..."
echo "$(date +%Y-%m-%d) - OKX密钥轮换 - $ENV" >> ~/.kronos/key_rotation_log.txt

echo ""
echo "✅ 密钥轮换完成!"
echo "📝 旧密钥备份: ~/.hermes/.env.backup.$(date +%Y%m%d)"
echo "📝 请在OKX后台删除旧密钥 (24小时后自动失效)"
```

### 4.2 紧急轮换 (密钥泄露)

```bash
#!/bin/bash
# scripts/emergency_key_rotation.sh
# 紧急密钥轮换 - 用于密钥泄露场景

set -e

echo "🚨 紧急密钥轮换流程"
echo "警告: 此操作将立即使当前密钥失效!"
echo ""

# 1. 立即暂停所有交易
echo "[1/6] 暂停所有交易..."
pkill -f kronos_pilot.py
pkill -f kronos_auto_guard.py
echo "✅ 所有交易进程已停止"

# 2. 撤销所有活跃订单
echo "[2/6] 撤销所有活跃订单..."
cd ~/kronos
python3 -c "
import os, requests, hmac, hashlib, base64, json
from dotenv import load_dotenv
load_dotenv()

key = os.getenv('OKX_API_KEY')
secret = os.getenv('OKX_SECRET')
passphrase = os.getenv('OKX_PASSPHRASE')

ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
body = json.dumps({})
msg = f'{ts}GET/api/v5/trade/orders-pending{body}'
sign = base64.b64encode(hmac.new(
    secret.encode(), msg.encode(), hashlib.sha256
)).decode()

headers = {
    'OK-ACCESS-KEY': key,
    'OK-ACCESS-SIGN': sign,
    'OK-ACCESS-TIMESTAMP': ts,
    'OK-ACCESS-PASSPHRASE': passphrase,
    'Content-Type': 'application/json',
}

r = requests.get('https://www.okx.com/api/v5/trade/orders-pending', headers=headers)
orders = r.json().get('data', [])

for order in orders:
    # 取消每个订单
    cancel_msg = f'{ts}POST/api/v5/trade/cancel-order{body}'
    cancel_sign = base64.b64encode(hmac.new(
        secret.encode(), cancel_msg.encode(), hashlib.sha256
    )).decode()
    headers['OK-ACCESS-SIGN'] = cancel_sign
    requests.post(
        f'https://www.okx.com/api/v5/trade/cancel-order',
        headers=headers,
        data=json.dumps({'instId': order['instId'], 'ordId': order['ordId']})
    )
print(f'已撤销 {len(orders)} 个订单')
" 2>/dev/null || echo "部分订单撤销失败，请手动检查"

# 3. 备份当前配置
echo "[3/6] 备份当前配置..."
cp ~/.hermes/.env ~/.hermes/.env.emergency.$(date +%Y%m%d_%H%M%S)
echo "✅ 已备份"

# 4. 生成新密钥
echo "[4/6] 请立即登录OKX创建新密钥..."
echo "   https://www.okx.com/account/my-account/api-key"
read -p "新 API Key: " NEW_API_KEY
read -p "新 Secret: " NEW_SECRET
read -p "新 Passphrase: " NEW_PASSPHRASE

# 5. 更新配置
echo "[5/6] 更新配置..."
sed -i '' \
    -e "s/OKX_API_KEY=.*/OKX_API_KEY=$NEW_API_KEY/" \
    -e "s/OKX_SECRET=.*/OKX_SECRET=$NEW_SECRET/" \
    -e "s/OKX_PASSPHRASE=.*/OKX_PASSPHRASE=$NEW_PASSPHRASE/" \
    ~/.hermes/.env

# 6. 记录并告警
echo "[6/6] 发送安全告警..."
echo "🚨 $(date) - 紧急密钥轮换" >> ~/.kronos/security_log.txt
# 发送告警
curl -X POST "https://open.feishu.cn/open-apis/bot/v2/hook/xxx" \
    -H "Content-Type: application/json" \
    -d '{"msg_type":"text","content":{"text":"🚨 Kronos紧急密钥轮换完成，请检查账户安全"}}'

echo ""
echo "✅ 紧急轮换完成!"
echo "⚠️  请立即在OKX后台删除旧密钥"
echo "⚠️  检查账户是否有异常交易"
```

## 5. 密钥环境变量

### 5.1 环境变量模板

```bash
# ~/.hermes/.env

# ==================== OKX API ====================
# API密钥 (交易权限)
OKX_API_KEY=your_api_key_here
OKX_SECRET=your_secret_here
OKX_PASSPHRASE=your_passphrase_here

# 模拟盘/实盘切换
# 1 = 模拟盘 (测试)
# 0 = 实盘 (生产)
OKX_FLAG=1

# ==================== Feishu ====================
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=your_app_secret_here
FEISHU_CHAT_ID=oc_bfd8a7cc1a606f190b53e3fd0167f5a0

# ==================== 其他 ====================
# 日志级别
LOG_LEVEL=INFO

# 通知开关
ENABLE_FEISHU=true
```

### 5.2 权限检查

```bash
# 检查.env文件权限
ls -la ~/.hermes/.env

# 正确权限应该是: -rw-------
# 如果不是，执行:
chmod 600 ~/.hermes/.env
```

## 6. 密钥审计

### 6.1 审计日志

```bash
# ~/.kronos/key_rotation_log.txt 格式
2026-01-15 - OKX密钥轮换 - test
2026-04-15 - OKX密钥轮换 - production
2026-04-20 - Feishu密钥轮换 - production

# ~/.kronos/security_log.txt 格式
2026-04-01 10:30:00 - 紧急密钥轮换
2026-04-15 14:22:00 - 异常登录检测
```

### 6.2 定期审计清单

```markdown
## 月度密钥审计清单

### 1. 密钥有效期检查
- [ ] OKX API Key 有效期 > 30天
- [ ] Feishu App Secret 有效期 > 30天

### 2. 权限检查
- [ ] OKX API 只有必要权限
- [ ] 无提币权限
- [ ] IP白名单正确配置

### 3. 使用记录
- [ ] 检查密钥使用日志
- [ ] 确认无异常访问

### 4. 备份检查
- [ ] .env文件已备份
- [ ] 备份文件权限正确 (600)

### 5. 轮换记录
- [ ] 90天内已完成轮换
- [ ] 轮换记录已更新
```

## 7. 故障排查

### 7.1 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|---------|
| API签名错误 | 时间戳不同步 | 同步系统时间 |
| 密钥无效 | 密钥格式错误 | 检查.env文件编码 |
| IP限制 | 服务器IP不在白名单 | 添加IP到白名单 |
| 权限不足 | 密钥权限配置错误 | 在OKX后台重新配置 |

### 7.2 诊断命令

```bash
# 1. 检查环境变量
grep -E "^OKX_|^FEISHU_" ~/.hermes/.env

# 2. 测试API连通性
curl -s https://www.okx.com/api/v5/public/time

# 3. 验证密钥格式
python3 -c "
import os
key = os.getenv('OKX_API_KEY', '')
print(f'Key长度: {len(key)}')
print(f'Key格式: {key[:10]}...{key[-4:]}')
"

# 4. 测试OKX API
cd ~/kronos && python3 -c "
from kronos_pilot import _req
result = _req('GET', '/api/v5/public/time')
print('✅ API连通正常' if result.get('code') == '0' else '❌ API连通失败')
"
```

## 8. 相关文档

- [部署文档](deployment.md)
- [备份恢复文档](backup_recovery.md)
- [环境配置](environment.md)
- [安全审计](../scripts/security_check.sh)

---

*最后更新: 2026-04-26*
*安全等级: 高*
*维护者: Kronos 安全团队*

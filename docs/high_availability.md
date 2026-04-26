# Kronos 高可用架构方案

## 1. 当前单点风险

```
┌─────────────────────────────────────────────────────────────┐
│                    当前架构 (单点故障)                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Mac Mini (本地)                                           │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  kronos_pilot.py      (信号生成)                     │  │
│   │  kronos_auto_guard.py  (风险监控)                     │  │
│   │  kronos_heartbeat.py  (心跳复盘)                     │  │
│   │  real_monitor.py      (持仓监控)                     │  │
│   └─────────────────────────────────────────────────────┘  │
│                         │                                   │
│                         ▼                                   │
│   ┌─────────────────────────────────────────────────────┐  │
│   │  本地状态文件                                        │  │
│   │  • paper_trades.json                                │  │
│   │  • circuit.json                                    │  │
│   │  • treasury.json                                   │  │
│   └─────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘

风险：
❌ 机器宕机 → 交易完全中断
❌ 磁盘损坏 → 状态数据丢失
❌ 网络中断 → 无法接收信号
❌ 无故障转移 → 无冗余保护
```

## 2. 高可用架构目标

| 指标 | 当前 | 目标 |
|------|------|------|
| 可用性 | 99% | 99.9% |
| RTO (恢复时间目标) | 数小时 | < 5分钟 |
| RPO (数据丢失目标) | 数小时 | < 1分钟 |
| 故障转移 | 手动 | 自动 |

## 3. 推荐高可用方案

### 方案A：双机热备（推荐）

```
┌─────────────────────────────────────────────────────────────────┐
│                     双机热备架构                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────────┐         ┌─────────────────┐             │
│   │   主节点        │◄──────►│   备节点        │             │
│   │  (Mac Mini)     │  心跳   │  (备用服务器)   │             │
│   │                 │         │                 │             │
│   │  ● 运行中       │         │  ○ 热备         │             │
│   │  ● 交易执行     │         │  ○ 状态同步     │             │
│   │  ● 信号监听     │         │  ○ 无交易       │             │
│   └────────┬────────┘         └────────┬────────┘             │
│            │                              │                     │
│            │      状态同步                 │                     │
│            └──────────────┬───────────────┘                     │
│                           ▼                                     │
│            ┌──────────────────────────────┐                    │
│            │       Redis 状态同步          │                    │
│            │  • paper_trades (实时)        │                    │
│            │  • circuit_state (实时)        │                    │
│            │  • treasury_snapshot (每分钟)  │                    │
│            └──────────────────────────────┘                    │
│                           │                                     │
│                           ▼                                     │
│            ┌──────────────────────────────┐                    │
│            │       云存储备份             │                    │
│            │  • AWS S3 / 阿里云OSS        │                    │
│            │  • 状态快照 (每5分钟)        │                    │
│            └──────────────────────────────┘                    │
│                           │                                     │
│                           ▼                                     │
│                   ┌───────────────┐                            │
│                   │   OKX API    │                            │
│                   │  (实盘/模拟)  │                            │
│                   └───────────────┘                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

故障切换流程：
1. 主节点心跳消失 (超过30秒)
2. 备节点自动提升为主节点
3. 备节点从Redis恢复最新状态
4. 备节点继续执行交易
5. 人工介入修复原主节点
```

### 方案B：云原生容器化

```
┌─────────────────────────────────────────────────────────────────┐
│                   云原生容器化架构                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │                    Kubernetes 集群                       │  │
│   │                                                      │  │
│   │   ┌─────────────┐      ┌─────────────┐              │  │
│   │   │  kronos-1   │      │  kronos-2   │              │  │
│   │   │  (主Pod)    │◄───►│  (备Pod)    │              │  │
│   │   │             │  心跳  │             │              │  │
│   │   │  ✅ Active   │      │  ○ Standby  │              │  │
│   │   └─────────────┘      └─────────────┘              │  │
│   │                                                      │  │
│   └─────────────────────────────────────────────────────────┘  │
│                              │                                  │
│                              ▼                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │              PersistentVolume (共享存储)                   │  │
│   │   • 状态文件持久化                                        │  │
│   │   • 日志持久化                                            │  │
│   └─────────────────────────────────────────────────────────┘  │
│                              │                                  │
│                              ▼                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │              云存储 (AWS S3 / GCS)                       │  │
│   │   • 跨区域复制                                           │  │
│   │   • 版本控制                                             │  │
│   │   • 生命周期管理                                         │  │
│   └─────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## 4. 状态同步机制

### 4.1 实时同步 (Redis)

```python
# state_sync.py
import redis
import json
from typing import Any, Optional

class StateSync:
    """状态同步器 - 使用Redis实现实时同步"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = redis.from_url(redis_url)
        self.prefix = "kronos:state:"
    
    def sync_write(self, key: str, value: Any, ttl: int = 3600):
        """写入状态并同步到备节点"""
        full_key = f"{self.prefix}{key}"
        self.redis.setex(full_key, ttl, json.dumps(value))
    
    def sync_read(self, key: str) -> Optional[Any]:
        """从Redis读取状态"""
        full_key = f"{self.prefix}{key}"
        data = self.redis.get(full_key)
        return json.loads(data) if data else None
    
    def acquire_lock(self, lock_name: str, timeout: int = 30) -> bool:
        """获取分布式锁 - 确保同时只有一个节点执行交易"""
        lock_key = f"{self.prefix}lock:{lock_name}"
        return self.redis.set(lock_key, "1", nx=True, ex=timeout)
    
    def release_lock(self, lock_name: str):
        """释放分布式锁"""
        lock_key = f"{self.prefix}lock:{lock_name}"
        self.redis.delete(lock_key)
```

### 4.2 快照备份 (云存储)

```bash
#!/bin/bash
# scripts/cloud_backup.sh

# 每5分钟执行
aws s3 sync /Users/jimingzhang/kronos/data/ s3://kronos-backup/data/ \
    --exclude "*.tmp" \
    --exclude "*.log" \
    --storage-class STANDARD_IA

# 保留最近30个版本
aws s3api put-object-tagging \
    --bucket kronos-backup \
    --key data/ \
    --tagging 'Tier=Standard&Retention=30days'
```

## 5. 故障检测与切换

### 5.1 心跳机制

```python
# heartbeat_monitor.py
import time
import redis
from dataclasses import dataclass
from datetime import datetime

@dataclass
class HeartbeatStatus:
    node_id: str
    last_heartbeat: datetime
    is_active: bool
    state: str  # 'running', 'standby', 'failed'

class HeartbeatMonitor:
    """心跳监控 - 检测节点健康状态"""
    
    HEARTBEAT_INTERVAL = 10  # 秒
    HEARTBEAT_TIMEOUT = 30   # 秒 (超过30秒无心跳认为故障)
    
    def __init__(self, node_id: str, redis_url: str):
        self.node_id = node_id
        self.redis = redis.from_url(redis_url)
    
    def send_heartbeat(self):
        """发送心跳"""
        key = f"kronos:heartbeat:{self.node_id}"
        self.redis.setex(key, self.HEARTBEAT_TIMEOUT, time.time())
    
    def check_all_nodes(self) -> dict:
        """检查所有节点状态"""
        pattern = "kronos:heartbeat:*"
        nodes = {}
        
        for key in self.redis.scan_iter(pattern):
            node_id = key.decode().split(":")[-1]
            last_time = self.redis.get(key)
            if last_time:
                last_time = float(last_time)
                age = time.time() - last_time
                
                nodes[node_id] = {
                    "is_alive": age < self.HEARTBEAT_TIMEOUT,
                    "last_seen": last_time,
                    "age_seconds": age
                }
        
        return nodes
    
    def should_takeover(self) -> bool:
        """是否应该接管主节点"""
        nodes = self.check_all_nodes()
        active_nodes = [n for n, s in nodes.items() if s["is_alive"]]
        
        # 如果自己是唯一存活的节点，接管
        return self.node_id in active_nodes and len(active_nodes) == 1
```

### 5.2 自动故障切换

```python
# failover_controller.py
class FailoverController:
    """故障切换控制器"""
    
    def __init__(self, state_sync: StateSync, heartbeat: HeartbeatMonitor):
        self.state = state_sync
        self.heartbeat = heartbeat
    
    def monitor_and_failover(self):
        """监控并执行故障切换"""
        while True:
            time.sleep(10)  # 每10秒检查一次
            
            # 检查是否应该接管
            if self.heartbeat.should_takeover():
                self._execute_failover()
    
    def _execute_failover(self):
        """执行故障切换"""
        # 1. 获取分布式锁
        if not self.state.acquire_lock("failover", timeout=60):
            return  # 已有其他节点在切换
        
        try:
            # 2. 从Redis恢复最新状态
            paper_trades = self.state.sync_read("paper_trades")
            circuit_state = self.state.sync_read("circuit")
            treasury = self.state.sync_read("treasury")
            
            # 3. 恢复本地状态文件
            self._restore_state(paper_trades, circuit_state, treasury)
            
            # 4. 启动交易引擎
            self._start_trading_engine()
            
            # 5. 发送告警
            self._send_alert("FAILOVER", "备节点已接管主节点")
            
        finally:
            self.state.release_lock("failover")
    
    def _restore_state(self, paper_trades, circuit_state, treasury):
        """恢复状态文件"""
        atomic_write_json("paper_trades.json", paper_trades)
        atomic_write_json("circuit.json", circuit_state)
        atomic_write_json("treasury.json", treasury)
```

## 6. 告警与通知

### 6.1 告警规则

| 告警级别 | 触发条件 | 通知方式 | 响应时间 |
|---------|---------|---------|---------|
| 🔴 P0 | 双节点同时故障 | 飞书+短信+电话 | 立即 |
| 🟠 P1 | 主节点故障 | 飞书+邮件 | 5分钟内 |
| 🟡 P2 | 心跳延迟 > 20秒 | 飞书 | 30分钟内 |
| 🟢 P3 | 状态同步延迟 | 日志 | 工作时间 |

### 6.2 告警代码

```python
# alerts.py
class AlertManager:
    """告警管理器"""
    
    def __init__(self):
        self.feishu = FeishuNotifier()
        self.sms = SMSNotifier()  # Twilio
    
    def send_alert(self, level: str, title: str, message: str):
        """发送告警"""
        if level == "P0":
            # 紧急告警 - 多渠道
            self.feishu.send(f"🚨 {title}\n\n{message}")
            self.sms.send(f"KRONOS ALERT: {title}")
        elif level == "P1":
            # 重要告警
            self.feishu.send(f"🔴 {title}\n\n{message}")
        elif level == "P2":
            # 一般告警
            self.feishu.send(f"🟡 {title}\n\n{message}")
```

## 7. 测试与演练

### 7.1 故障切换测试

```bash
#!/bin/bash
# scripts/test_failover.sh

echo "=== Kronos 高可用故障切换测试 ==="

# 1. 确认主节点运行中
echo "[1/5] 检查主节点状态..."
python3 -c "from heartbeat_monitor import HeartbeatMonitor; ..."
if [ $? -eq 0 ]; then
    echo "✅ 主节点运行正常"
else
    echo "❌ 主节点未运行"
    exit 1
fi

# 2. 模拟主节点故障
echo "[2/5] 模拟主节点故障..."
pkill -f kronos_pilot.py
echo "✅ 已终止主节点进程"

# 3. 等待故障检测
echo "[3/5] 等待故障检测 (30秒)..."
sleep 30

# 4. 检查备节点是否接管
echo "[4/5] 检查备节点是否接管..."
if curl -s http://备节点IP:8080/health | grep "active"; then
    echo "✅ 备节点已接管"
else
    echo "❌ 备节点接管失败"
    exit 1
fi

# 5. 恢复主节点
echo "[5/5] 恢复主节点..."
pkill -f kronos_standby.py
echo "✅ 故障切换测试完成"
```

### 7.2 演练计划

| 季度 | 演练内容 | 时长 |
|------|---------|------|
| Q1 | 单节点故障切换 | 2小时 |
| Q2 | 网络中断恢复 | 2小时 |
| Q3 | 数据损坏恢复 | 3小时 |
| Q4 | 年度灾难恢复演练 | 8小时 |

## 8. 实施路线图

### Phase 1: 基础架构 (1-2周)
- [ ] 部署Redis实例
- [ ] 实现状态同步模块
- [ ] 添加心跳监控

### Phase 2: 故障切换 (2-3周)
- [ ] 实现故障检测
- [ ] 实现自动切换
- [ ] 添加告警通知

### Phase 3: 测试验证 (1周)
- [ ] 故障切换测试
- [ ] 性能基准测试
- [ ] 文档更新

### Phase 4: 生产部署 (1周)
- [ ] 部署备节点
- [ ] 配置监控告警
- [ ] 团队培训

## 9. 成本估算

| 方案 | 月成本 | 适用场景 |
|------|--------|---------|
| 双机热备 (VPS) | ~$50/月 | 中小资金 |
| 双机热备 (云服务器) | ~$200/月 | 大资金 |
| Kubernetes 托管 | ~$500/月 | 企业级 |

---

*最后更新: 2026-04-26*
*维护者: Kronos 团队*

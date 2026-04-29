# Kronos Monitoring Guide

本目录包含Prometheus监控指标、Grafana仪表盘和健康检查看门狗的实现。

## 组件

### 1. Prometheus Metrics Exporter (`prometheus_metrics.py`)
暴露Prometheus格式的监控指标。

**功能:**
- 交易指标 (总交易数、盈亏)
- 账户权益 (当前、起始、百分比变化)
- 持仓指标 (开仓数、各持仓盈亏)
- 熔断器状态 (是否触发、连续亏损次数)
- 系统运行时间
- API错误统计

**启动方式:**
```bash
# 单独启动 (默认端口9090)
python monitoring/prometheus_metrics.py

# 指定端口
python monitoring/prometheus_metrics.py --port 9090

# 测试模式 (不启动服务器)
python monitoring/prometheus_metrics.py --test
```

**HTTP端点:**
- `GET /metrics` - Prometheus指标 (供Prometheus抓取)
- `GET /health` - 健康检查
- `GET /metrics/positions` - 持仓JSON详情
- `GET /metrics/circuit` - 熔断器状态JSON
- `GET /metrics/trades` - 最近交易JSON
- `GET /reload` - 强制刷新指标

### 2. Health Watchdog (`health_watchdog.py`)
独立的健康检查服务，支持自愈操作和飞书告警。

**检查项目:**
- 进程存活检查
- OKX API可用性
- 熔断器状态
- 持仓超时检查 (>72小时)
- 权益异常检查 (回撤检测)
- 财务政策合规检查
- 最近交易活动检查

**启动方式:**
```bash
# 前台运行
python monitoring/health_watchdog.py

# 后台运行
python monitoring/health_watchdog.py --daemon

# 单次检查并退出
python monitoring/health_watchdog.py --once

# 自定义检查间隔 (秒)
python monitoring/health_watchdog.py --interval 60
```

**HTTP端点:**
- `GET /health` - 主健康端点
- `GET /health/live` - 存活探针
- `GET /health/ready` - 就绪探针
- `GET /health/details` - 详细健康信息

### 3. Grafana Dashboard
可视化仪表盘，展示系统状态。

**面板:**
- 系统概览 (权益、熔断器状态、持仓数、运行时间)
- 权益与盈亏曲线
- 持仓详情表
- 交易历史统计
- 守护运行统计
- 财务损失追踪

**导入方式:**
1. 启动Grafana: `http://localhost:3000` (admin/admin)
2. 自动导入: 仪表盘已配置自动provisioning
3. 手动导入: Grafana UI → Dashboards → Import → 上传 `grafana/provisioning/dashboards/kronos-overview.json`

### 4. Monitoring Stack (Docker)
完整的Prometheus + Grafana监控栈。

**启动:**
```bash
cd grafana
docker-compose -f docker-compose.monitoring.yml up -d
```

**服务地址:**
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3000 (admin/admin)

## 快速启动

```bash
# 启动全部监控服务
./monitoring/start_monitoring.sh start

# 查看状态
./monitoring/start_monitoring.sh status

# 停止全部
./monitoring/start_monitoring.sh stop
```

## 环境变量

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `PROMETHEUS_PORT` | 9090 | Prometheus导出器端口 |
| `HEALTH_PORT` | 9091 | 健康检查服务端口 |
| `HEALTH_CHECK_INTERVAL` | 60 | 健康检查间隔(秒) |
| `OKX_FLAG` | '1' | OKX模拟/真实交易模式 |

## Prometheus指标列表

| 指标名 | 类型 | 描述 |
|--------|------|------|
| `kronos_trades_total` | Counter | 总交易数 |
| `kronos_equity_current` | Gauge | 当前权益 |
| `kronos_equity_start` | Gauge | 起始权益 |
| `kronos_equity_pct_change` | Gauge | 权益百分比变化 |
| `kronos_positions_open` | Gauge | 开仓数 |
| `kronos_position_pnl` | Gauge | 各持仓盈亏 |
| `kronos_circuit_breaker_tripped` | Gauge | 熔断器是否触发 (0/1) |
| `kronos_circuit_consecutive_losses` | Gauge | 连续亏损次数 |
| `kronos_guard_runs_total` | Counter | 守护运行次数 |
| `kronos_guard_dangers_detected` | Counter | 检测到的危险数 |
| `kronos_treasury_hourly_loss` | Gauge | 小时损失 |
| `kronos_treasury_daily_loss` | Gauge | 日损失 |
| `kronos_system_uptime_seconds` | Gauge | 系统运行秒数 |

## 故障排查

**Prometheus无法抓取指标:**
```bash
# 检查Kronos导出器是否运行
curl http://localhost:9090/metrics

# 检查Prometheus targets
http://localhost:9090/targets
```

**Grafana无数据:**
```bash
# 检查数据源配置
http://localhost:3000/connections/datasources

# 检查Prometheus数据源URL是否为 http://prometheus:9090 (Docker内) 
# 或 http://localhost:9090 (宿主机)
```

**健康检查告警未发送:**
- 检查飞书机器人配置 (FEISHU_APP_ID, FEISHU_APP_SECRET)
- 确认机器人已加入群组

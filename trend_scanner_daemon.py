#!/usr/bin/env python3
"""
trend_scanner_daemon.py
每30秒扫描一次 + 执行交易
后台运行，stdout输出到日志文件
"""
import subprocess, sys, time, os
from pathlib import Path

LOG_FILE = Path.home() / '.hermes' / 'cron' / 'output' / 'scanner_daemon.log'
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

log("启动 trend_scanner_daemon (每30秒)")

while True:
    try:
        result = subprocess.run(
            ['python3', '/Users/jimingzhang/kronos/trend_scanner.py', '--live'],
            capture_output=True, text=True,
            timeout=55,
            env={**os.environ, 'PATH': f'/Users/jimingzhang/kronos/venv/bin:{os.environ.get("PATH","")}'}
        )
        if result.stdout:
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    log(line)
        if result.returncode != 0 and result.stderr:
            log(f"ERROR: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log("TIMEOUT: 扫描超过55秒，跳过")
    except Exception as e:
        log(f"EXCEPTION: {e}")
    
    time.sleep(30)  # 每30秒

#!/bin/bash
# 监控gemma4-heretic下载，完成后自动导入Ollama
# 用法: bash watch_download.sh

GGUF_FILE="/Users/jimingzhang/.ollama/models/gemma-4-E2B-heretic-IQ4_XS.gguf"
TARGET_SIZE=3309823232

echo "监控下载: $GGUF_FILE"
echo "目标大小: $((TARGET_SIZE/1024/1024)) MB"

while true; do
    if [ ! -f "$GGUF_FILE" ]; then
        sleep 60
        continue
    fi
    
    SIZE=$(stat -f%z "$GGUF_FILE" 2>/dev/null || stat -c%s "$GGUF_FILE" 2>/dev/null)
    PCT=$((SIZE * 100 / TARGET_SIZE))
    
    echo "$(date '+%H:%M:%S') ${SIZE}/$TARGET_SIZE = ${PCT}%"
    
    # 下载完成
    if [ "$SIZE" -ge "$TARGET_SIZE" ]; then
        echo "✅ 下载完成!"
        echo "运行导入..."
        bash /Users/jimingzhang/kronos/import_gemma4_heretic.sh
        break
    fi
    
    # 进程死了
    if ! pgrep -f "curl.*gemma-4-E2B-heretic" > /dev/null; then
        echo "⚠️ curl进程已停止，但文件未完成"
        echo "当前大小: $(du -h "$GGUF_FILE" | cut -f1)"
        break
    fi
    
    sleep 120  # 每2分钟检查一次
done

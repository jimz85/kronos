#!/bin/bash
# gemma4-2B-heretic Ollama导入脚本
# 当gemma-4-E2B-heretic-IQ4_XS.gguf下载完成后自动运行

set -e

GGUF_FILE="/Users/jimingzhang/.ollama/models/gemma-4-E2B-heretic-IQ4_XS.gguf"
TARGET_SIZE=3309823232  # 3.3GB

echo "=== Ollama 导入 gemma4-2B-heretic ==="

# 检查文件是否存在
if [ ! -f "$GGUF_FILE" ]; then
    echo "❌ 文件不存在: $GGUF_FILE"
    exit 1
fi

# 检查文件大小
ACTUAL_SIZE=$(stat -f%z "$GGUF_FILE" 2>/dev/null || stat -c%s "$GGUF_FILE" 2>/dev/null)
echo "文件大小: $(du -h "$GGUF_FILE" | cut -f1)"

if [ "$ACTUAL_SIZE" -lt "$((TARGET_SIZE / 2))" ]; then
    echo "⚠️ 文件可能不完整（${ACTUAL_SIZE} < ${TARGET_SIZE}）"
    echo "等待下载完成..."
    exit 1
fi

echo "✅ 文件完整，开始导入Ollama..."

# 创建Modelfile
cat > /tmp/gemma4-2b-heretic.Modelfile << 'EOF'
FROM /Users/jimingzhang/.ollama/models/gemma-4-E2B-heretic-IQ4_XS.gguf
TEMPLATE """{{ if .System }}{{ .System }}{{ end }}{{ range .Messages }}{{ if eq .Role "user" }}User: {{ .Content }}{{ else }}Model: {{ .Content }}{{ end }}{{ end }}"""
PARAMETER num_ctx 2048
PARAMETER temperature 0.7
EOF

# 导入Ollama
cd /Users/jimingzhang/.ollama/models
echo "运行: ollama create gemma4-2b-heretic -f /tmp/gemma4-2b-heretic.Modelfile"
ollama create gemma4-2b-heretic -f /tmp/gemma4-2b-heretic.Modelfile

echo "✅ 导入完成"
echo "验证: ollama list | grep gemma4-2b-heretic"
ollama list | grep gemma4-2b-heretic || echo "⚠️ 未找到，请检查"

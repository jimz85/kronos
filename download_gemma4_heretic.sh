#!/bin/bash
# Gemma4-2B-Heretic 下载脚本（断点续传）
# 目标: mradermacher/gemma-4-E2B-it-heretic-ara-GGUF / gemma-4-E2B-it-heretic-IQ4_XS.gguf
# 目标大小: ~1.6GB

set -e

MODEL_DIR="/Users/jimingzhang/.ollama/models"
TARGET_FILE="$MODEL_DIR/gemma-4-E2B-heretic-IQ4_XS.gguf"
TARGET_SIZE=1650000000  # ~1.65GB

# HuggingFace镜像加速
HF_BASE="https://hf-mirror.com"
REPO="mradermacher/gemma-4-E2B-it-heretic-ara-GGUF"
FILE="gemma-4-E2B-it-heretic-IQ4_XS.gguf"

echo "=== Gemma4-2B-Heretic 下载器 ==="
echo "目标文件: $TARGET_FILE"

# 检查当前文件大小
if [ -f "$TARGET_FILE" ]; then
    CURRENT_SIZE=$(stat -f%z "$TARGET_FILE" 2>/dev/null || stat -c%s "$TARGET_FILE" 2>/dev/null)
    echo "当前文件大小: $(du -h "$TARGET_FILE" | cut -f1)"
    echo "目标大小: $(numfmt --to=iec $TARGET_SIZE)"
    echo "下载进度: $(python3 -c "print(f'{$CURRENT_SIZE/$TARGET_SIZE*100:.0f}%')")"
else
    echo "新文件，将从头开始下载"
    CURRENT_SIZE=0
fi

# 用curl断点续传下载
echo ""
echo "开始下载（断点续传）..."
curl -L \
    --continue-at - \
    --output "$TARGET_FILE" \
    --progress-bar \
    "$HF_BASE/$REPO/resolve/main/$FILE"

# 验证文件大小
ACTUAL_SIZE=$(stat -f%z "$TARGET_FILE" 2>/dev/null || stat -c%s "$TARGET_FILE" 2>/dev/null)
echo ""
echo "=== 下载完成 ==="
echo "文件大小: $(du -h "$TARGET_FILE" | cut -f1)"

if [ "$ACTUAL_SIZE" -lt "$TARGET_SIZE" ]; then
    echo "⚠️ 文件可能不完整（$ACTUAL_SIZE < $TARGET_SIZE）"
    exit 1
else
    echo "✅ 文件完整"
fi

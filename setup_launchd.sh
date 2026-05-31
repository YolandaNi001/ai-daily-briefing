#!/bin/bash
# ============================================================
# AI产业+营销AI 每日新闻简报 — 定时任务安装脚本
# 使用 macOS launchd 每天早9:00自动执行
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.yolanda.daily-ai-briefing.plist"
PLIST_SRC="$SCRIPT_DIR/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
PYTHON_BIN="$(which python3)"

echo "=========================================="
echo " AI产业+营销AI 每日新闻简报 — 定时任务安装"
echo "=========================================="
echo ""

# Step 1: 检查 Python
echo "[1/4] 检查 Python 环境..."
if [ -z "$PYTHON_BIN" ]; then
    echo "  ✗ 未找到 python3，请先安装 Python"
    exit 1
fi
echo "  ✓ Python: $PYTHON_BIN"

# Step 2: 安装依赖
echo "[2/4] 安装 Python 依赖..."
cd "$SCRIPT_DIR"
pip3 install -r requirements.txt --quiet
echo "  ✓ 依赖安装完成"

# Step 3: 生成 plist 文件
echo "[3/4] 生成 launchd 配置文件..."
cat > "$PLIST_SRC" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.yolanda.daily-ai-briefing</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$SCRIPT_DIR/daily_ai_briefing.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/.briefing_cache/stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/.briefing_cache/stderr.log</string>

    <key>RunAtLoad</key>
    <false/>

    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
PLISTEOF
echo "  ✓ 配置文件已生成: $PLIST_SRC"

# Step 4: 安装到 LaunchAgents
echo "[4/4] 安装定时任务..."
mkdir -p "$HOME/Library/LaunchAgents"

# 卸载旧的（如果存在）
launchctl unload "$PLIST_DST" 2>/dev/null || true

cp "$PLIST_SRC" "$PLIST_DST"
launchctl load "$PLIST_DST"

echo "  ✓ 定时任务已安装"
echo ""
echo "=========================================="
echo " 安装完成！"
echo "=========================================="
echo ""
echo "📋 任务信息："
echo "   名称: com.yolanda.daily-ai-briefing"
echo "   执行: 每天 9:00 AM"
echo "   脚本: $SCRIPT_DIR/daily_ai_briefing.py"
echo "   日志: $SCRIPT_DIR/.briefing_cache/"
echo ""
echo "🔧 常用命令："
echo "   查看状态:  launchctl list | grep daily-ai-briefing"
echo "   手动执行:  python3 $SCRIPT_DIR/daily_ai_briefing.py --output"
echo "   测试采集:  python3 $SCRIPT_DIR/daily_ai_briefing.py --dry-run --output"
echo "   卸载任务:  launchctl unload $PLIST_DST"
echo "   查看日志:  tail -f $SCRIPT_DIR/.briefing_cache/stdout.log"
echo ""
echo "⚠  请确保已配置 .env 文件中的 DEEPSEEK_API_KEY 和 SERVERCHAN_SENDKEY！"
echo "   cp .env.example .env  # 然后编辑填入真实 Key"
echo ""
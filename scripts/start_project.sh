#!/usr/bin/env bash
# 新项目 Multi-Agent 流水线启动脚本（泛用版）
# 用法: ./start_project.sh <项目slug> <项目中文名> <工作目录>
# 示例: ./start_project.sh my-webapp "我的 Web 应用" ~/projects/my-webapp

set -e

if [ $# -lt 2 ]; then
    echo "用法: $0 <项目slug> <项目中文名> [工作目录]"
    echo "示例: $0 my-webapp \"我的 Web 应用\" ~/projects/my-webapp"
    exit 1
fi

SLUG="$1"
DISPLAY_NAME="$2"
WORKDIR="${3:-$HOME/projects/$SLUG}"

echo "=== 1. 建项目目录 ==="
mkdir -p "$WORKDIR"/{docs,src,tmp}
echo "✅ $WORKDIR"

echo "=== 2. 建看板 ==="
hermes kanban boards create "$SLUG" --display "$DISPLAY_NAME" 2>/dev/null || echo "（看板可能已存在，继续）"
hermes kanban boards use "$SLUG"

echo "=== 3. 建任务链 ==="
T1=$(hermes kanban create "需求梳理" --assignee pm --priority 10 \
    --workspace "dir:$WORKDIR" --json | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "  T1 需求梳理: $T1"

T2=$(hermes kanban create "技术方案设计" --assignee architect --parent "$T1" --priority 9 \
    --workspace "dir:$WORKDIR" --json | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "  T2 技术方案: $T2"

T3=$(hermes kanban create "代码实现" --assignee builder --parent "$T2" --priority 8 \
    --workspace "dir:$WORKDIR" --json | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "  T3 代码实现: $T3"

T4=$(hermes kanban create "代码审计" --assignee auditor --parent "$T3" --priority 7 \
    --workspace "dir:$WORKDIR" --json | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "  T4 代码审计: $T4"

echo "=== 4. 启动 Watchdog（返工桥接 + 完成通知） ==="
WATCHDOG_SCRIPT="$(cd "$(dirname "$0")" && pwd)/kanban_watchdog.py"
LOG_DIR="$HOME/.hermes/kanban-watchdog"
mkdir -p "$LOG_DIR"

# 检查是否已有同名 cron
if crontab -l 2>/dev/null | grep -q "kanban_watchdog.*--board.*$SLUG"; then
    echo "（$SLUG 的 watchdog 已存在，跳过）"
else
    # 添加 cron 条目（每分钟跑一次）
    (crontab -l 2>/dev/null | grep -v "kanban_watchdog.*--board.*$SLUG"; \
     echo "* * * * * /usr/bin/python3 '$WATCHDOG_SCRIPT' --board '$SLUG' >> '$LOG_DIR/$SLUG.log' 2>&1") \
    | crontab -
    echo "✅ Watchdog 已部署（每分钟检查，cron 模式）"
fi

echo ""
echo "=== 完成！任务链已建好 ==="
echo "  下一步：编排者（主对话）完成 T1（$T1）触发流水线："
echo "  hermes kanban complete $T1 --result \"PRD 已完成，见 docs/PRD.md\""
echo ""
echo "  流水线: T1(PM) → T2(Architect) → T3(Builder) → T4(Auditor)"
echo "  审计不通过自动返工：auditor → 修复方案(architect) → 🐶 watchdog桥接 → 修复执行(builder) → 复审(auditor)，≤3轮"
echo "  完成自动通知：看板全部完成后桌面弹窗通知"
echo ""
echo "  项目目录: $WORKDIR"
echo "  看板: $SLUG"
echo "  Watchdog 日志: $LOG_DIR/$SLUG.log"

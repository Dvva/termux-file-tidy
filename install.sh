#!/data/data/com.termux/files/usr/bin/bash
# 注册 / 卸载每日定时任务
#
# 用法：
#   bash install.sh            注册（每天一次）
#   bash install.sh --remove   取消注册
#   bash install.sh --list     查看待执行任务

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/tasker_file_tidy.py"
PERIOD_MS=86400000   # 24 小时

die() { echo "[错误] $*" >&2; exit 1; }

command -v termux-job-scheduler >/dev/null 2>&1 \
    || die "找不到 termux-job-scheduler，请先 pkg install termux-api"

[ -f "$SCRIPT" ] || die "找不到主脚本：$SCRIPT"

case "${1:-}" in
    --remove)
        termux-job-scheduler --cancel --job-id 1 2>/dev/null \
            || termux-job-scheduler --cancel-all 2>/dev/null \
            || echo "没有可取消的任务"
        echo "已取消定时任务"
        exit 0
        ;;
    --list)
        termux-job-scheduler --pending
        exit 0
        ;;
esac

# 同步过来的文件可能没有执行权限，先补上
chmod +x "$SCRIPT" || die "chmod 失败"

# 确认 python3 可用
command -v python3 >/dev/null 2>&1 || die "找不到 python3，请先 pkg install python"
python3 -c "import sys; sys.exit(0)" || die "python3 无法运行"

echo "注册每日任务：$SCRIPT"
termux-job-scheduler \
    --script "$SCRIPT" \
    --period-ms "$PERIOD_MS" \
    --persisted true \
    --battery-not-low false \
    || die "注册失败"

echo
echo "当前已注册的任务："
termux-job-scheduler --pending 2>/dev/null || true

echo
echo "完成。日志在 ~/.local/state/file_tidy/"

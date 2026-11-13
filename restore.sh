#!/data/data/com.termux/files/usr/bin/bash
# restore.sh - Termux 重装后一键恢复 termux-file-tidy
#
# 重装 Termux 后，只需要先手动做两件事（没有 git 和仓库就跑不了本脚本）：
#   pkg install -y git
#   git clone https://github.com/Dvva/termux-file-tidy.git
# 然后进仓库目录执行：
#   bash restore.sh
#
# 脚本依次做：
#   1. 恢复共享存储授权（termux-setup-storage，会弹系统窗口，需要点「允许」）
#   2. 装回依赖包：python / termux-api / ffmpeg
#   3. 重新注册终端命令 tify（~/bin 包装）
#   4. 重新注册每日定时任务（termux-job-scheduler）
#   5. 自检：配置语法、命令可用、任务在列
#
# 用法：
#   bash restore.sh             标准恢复
#   bash restore.sh --no-ffmpeg 不装 ffmpeg（三个压缩块会跳过）
#   bash restore.sh --full      连 clang/llvm 编译链一起装
#   bash restore.sh --skip-pkg  不装包，只重建命令和定时任务
#   bash restore.sh --no-job    不碰定时任务
#   bash restore.sh --force-job 已有任务也重新注册
#   bash restore.sh --help      显示这段说明

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PY="/data/data/com.termux/files/usr/bin/python3"
TIFY_BIN="$HOME/bin/tify"
PKGS="python termux-api ffmpeg"
FULL_PKGS="clang make pkg-config vim fzf xxd ncurses-utils"

DO_PKG=1
DO_JOB=1
FORCE_JOB=0
FULL=0

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   [ok] %s\n' "$*"; }
warn() { printf '   [!!] %s\n' "$*" >&2; }
die()  { printf '\n[错误] %s\n' "$*" >&2; exit 1; }

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
}

# ffmpeg 起不来，多半是 libc++ 比 libplacebo 旧（重装后只 pkg install 不 upgrade 就会这样）
ensure_ffmpeg() {
    local ff="/data/data/com.termux/files/usr/bin/ffmpeg"
    [ -x "$ff" ] || return 1
    "$ff" -version >/dev/null 2>&1 && return 0
    warn "ffmpeg 无法运行（多半是 libc++ 太旧），补装 libc++ 再试"
    pkg install -y libc++ >/dev/null 2>&1 || true
    dpkg --configure -a >/dev/null 2>&1 || true
    "$ff" -version >/dev/null 2>&1
}

for arg in "$@"; do
    case "$arg" in
        --help|-h) usage; exit 0 ;;
        --no-ffmpeg) PKGS="python termux-api" ;;
        --full) FULL=1 ;;
        --skip-pkg) DO_PKG=0 ;;
        --no-job) DO_JOB=0 ;;
        --force-job) FORCE_JOB=1 ;;
        *) die "不认识的参数：$arg（bash restore.sh --help）" ;;
    esac
done

[ -f "$DIR/tasker_file_tidy.py" ] || die "当前目录没有 tasker_file_tidy.py：$DIR"

# ── 1. 共享存储授权 ────────────────────────────────────────────
storage_ok() { [ -d /sdcard ] && ls /sdcard/ >/dev/null 2>&1; }

step "1/5 共享存储授权（/sdcard）"
if storage_ok; then
    ok "已经能读 /sdcard"
else
    warn "现在读不了 /sdcard，唤起授权窗口——手机上请点「允许」"
    if command -v termux-setup-storage >/dev/null 2>&1; then
        termux-setup-storage >/dev/null 2>&1 || true
    else
        warn "找不到 termux-setup-storage（termux-tools 没装？）"
    fi
    i=0
    while [ "$i" -lt 60 ] && ! storage_ok; do
        sleep 1
        i=$((i + 1))
    done
    if storage_ok; then
        ok "已授权"
    else
        warn "仍未授权：请在手机 Termux 里敲 termux-setup-storage 并点「允许」，再重跑本脚本"
    fi
fi

# ── 2. 依赖包 ──────────────────────────────────────────────────
step "2/5 依赖包"
if [ "$DO_PKG" -eq 1 ]; then
    command -v pkg >/dev/null 2>&1 || die "找不到 pkg 命令，这不像 Termux 环境"
    pkg update -y >/dev/null 2>&1 || warn "pkg update 失败，继续尝试直接安装"
    # shellcheck disable=SC2086
    if pkg install -y $PKGS; then
        ok "已安装：$PKGS"
    else
        # ffmpeg 的半配置状态常常是 libc++ 太旧导致 libplacebo 链接失败，
        # 这不该中断整场恢复（tify 和定时任务不依赖 ffmpeg），修一下再继续
        warn "pkg install 报了错误，尝试修 ffmpeg 的链接问题"
        ensure_ffmpeg || warn "ffmpeg 仍未修好，压缩块会跳过；可手动跑 pkg upgrade"
    fi
    if [ "$FULL" -eq 1 ]; then
        # shellcheck disable=SC2086
        pkg install -y $FULL_PKGS && ok "已安装编译链：$FULL_PKGS" || warn "编译链装失败（不影响整理功能）"
    fi
else
    warn "已跳过装包（--skip-pkg）"
fi

# ── 3. 注册命令 tify ───────────────────────────────────────────
step "3/5 注册终端命令 tify"
bash "$DIR/mkcmd" || die "mkcmd 执行失败"
[ -x "$TIFY_BIN" ] || die "~/bin/tify 没生成"

# ── 4. 每日定时任务 ────────────────────────────────────────────
step "4/5 每日定时任务"
# termux-api 包只是客户端，没有 Termux:API 应用时调用会一直挂住，先探测
if ! timeout 10 termux-battery-status >/dev/null 2>&1; then
    warn "Termux:API 应用不可用：装好 Termux:API（F-Droid）并打开一次再重跑"
    warn "否则 termux-job-scheduler 会卡住，定时任务装不了（分类功能本身不受影响）"
    DO_JOB=0
fi
if [ "$DO_JOB" -eq 0 ]; then
    warn "跳过定时任务注册"
elif ! command -v termux-job-scheduler >/dev/null 2>&1; then
    warn "找不到 termux-job-scheduler，跳过节任务（termux-api 包没装上？）"
else
    has_job=0
    if timeout 15 termux-job-scheduler --pending 2>/dev/null | grep -q 'termux-file-tidy'; then
        has_job=1
    fi
    if [ "$has_job" -eq 1 ] && [ "$FORCE_JOB" -eq 0 ]; then
        ok "已有注册，跳过（要重建加 --force-job）"
    else
        bash "$DIR/install.sh" || warn "install.sh 未成功，定时任务没注册上"
    fi
fi

# ── 5. 自检 ────────────────────────────────────────────────────
step "5/5 自检"
if [ -x "$PY" ] || command -v python3 >/dev/null 2>&1; then
    if python3 "$DIR/check_conf.py" -q; then
        ok "配置语法通过"
    else
        warn "配置检查有错误，看上面的输出"
    fi
else
    warn "python3 不可用，跳过配置检查"
fi

if [ -x "$TIFY_BIN" ]; then
    if "$TIFY_BIN" --help >/dev/null 2>&1; then
        ok "tify --help 正常"
    else
        warn "tify --help 报错"
    fi
fi

if [ -x /data/data/com.termux/files/usr/bin/ffmpeg ] \
    && /data/data/com.termux/files/usr/bin/ffmpeg -version >/dev/null 2>&1; then
    ok "ffmpeg 可用（compress-image / music / video 能跑）"
else
    warn "ffmpeg 不可用，三个压缩块会被跳过"
fi

if command -v termux-job-scheduler >/dev/null 2>&1; then
    echo "   当前任务："
    timeout 15 termux-job-scheduler --pending 2>/dev/null | sed 's/^/     /' || echo "     （读取超时：Termux:API 应用没装或没运行）"
fi

printf '\n恢复完成。新开一个终端（或 source ~/.bashrc）后执行：tify --help\n'

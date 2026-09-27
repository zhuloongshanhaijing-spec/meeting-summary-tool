#!/bin/sh
# install.sh — Meeting Summary Tool 一键装配
#
# 装配一次，之后在任意目录敲 `mst` 即可启动网页控制台：
#   cd "<本仓库>" && ./install.sh
#   mst                     # 打开 http://127.0.0.1:8788 并自动弹浏览器
#
# 做了什么：
#   1. 在 ~/.local/bin（或 ${MST_BIN_DIR}）生成启动器 `mst`——绝对路径
#      指向本仓库 start.py，exec 直通信号（Ctrl+C 干净退出），参数透传。
#   2. 若该目录不在 PATH，向对应 shell rc 追加一行带标记的 export。
# 卸载：./install.sh --uninstall
#
# 零依赖：POSIX sh + python3（运行期才需要）。仓库移动位置后重跑本脚本。
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
BIN_DIR=${MST_BIN_DIR:-"$HOME/.local/bin"}
LAUNCHER="$BIN_DIR/mst"
PATH_LINE='export PATH="'"$BIN_DIR"':$PATH"'
PATH_MARKER='# added by Meeting Summary Tool installer'

uninstall=0
[ "${1:-}" = "--uninstall" ] && uninstall=1

if [ "$uninstall" = 1 ]; then
    rm -f -- "$LAUNCHER"
    # 只移除本安装器添加的两行（marker + export）
    for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
        [ -f "$rc" ] || continue
        if grep -qF "$PATH_MARKER" "$rc" 2>/dev/null; then
            tmp="$rc.mst-tmp"
            grep -vF -e "$PATH_LINE" -e "$PATH_MARKER" "$rc" > "$tmp" || true
            # 仅当恰好删掉 2 行时才落盘，避免误删用户内容
            if [ "$(wc -l < "$rc")" -eq "$(($(wc -l < "$tmp") + 2))" ]; then
                mv "$tmp" "$rc"
                echo "已从 $(basename "$rc") 移除 PATH 行"
            else
                rm -f "$tmp"
                echo "注意：$(basename "$rc") 中的 PATH 行似乎被改过，未自动移除"
            fi
        fi
    done
    echo "已卸载：$LAUNCHER"
    exit 0
fi

# ---- 前置检查 -------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ 未找到 python3 —— 请先安装 Python 3" >&2
    exit 1
fi
# 默认入口 = 网页控制台；纯 CLI 编译入口：MST_ENTRY=run_meeting.py ./install.sh
ENTRY=${MST_ENTRY:-start.py}
if [ ! -f "$REPO/$ENTRY" ]; then
    echo "✗ 仓库不完整：缺 ${ENTRY}（${REPO}）" >&2
    exit 1
fi

# ---- 生成启动器 -----------------------------------------------------------
mkdir -p -- "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# mst — Meeting Summary Tool（由 install.sh 生成，入口：${ENTRY}）
# 用法：mst [--port N] [--no-browser]（透传给 ${ENTRY}）
exec /usr/bin/env python3 "$REPO/$ENTRY" "\$@"
EOF
chmod 755 "$LAUNCHER"

# ---- PATH 检查 ------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) path_ok=1 ;;
    *)              path_ok=0 ;;
esac

if [ "$path_ok" = 0 ]; then
    rc_name=".zshrc"; rc_file="$HOME/.zshrc"
    case "${SHELL:-}" in
        *bash) rc_name=".bashrc"; rc_file="$HOME/.bashrc" ;;
    esac
    # 去重：已有等价行就不再追加
    if [ -f "$rc_file" ] && grep -qF "$PATH_LINE" "$rc_file" 2>/dev/null; then
        path_ok=2
    else
        printf '\n%s\n%s\n' "$PATH_MARKER" "$PATH_LINE" >> "$rc_file"
    fi
fi

echo "✓ 启动器已生成：$LAUNCHER"
case "$path_ok" in
    1) echo "✓ $BIN_DIR 已在 PATH 中" ;;
    2) echo "✓ PATH 行已存在于 rc 文件，跳过" ;;
    0) echo "✓ 已把 $BIN_DIR 写入 ${rc_name}（新终端生效，或先 source 一次）" ;;
esac
echo
echo "现在可以一条命令启动：  mst   （入口：${ENTRY}）"
echo "（等价于 python3 \"$REPO/$ENTRY\"，支持 --port N / --no-browser）"

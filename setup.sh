#!/bin/sh
# setup.sh — Meeting Summary Tool 全自动装配（一条命令，从零到可用）
#
# 新 Mac 标准流程：
#   git clone <私有仓库> && cd meeting-summary-tool
#   ./setup.sh
#   mst            # 新终端里永久可用
#
# 做什么（每步幂等，重跑自动跳过已完成部分）：
#   0. 前置：macOS / python3 3.10–3.12 / git / Homebrew
#   1. brew 装 ffmpeg + cmake（缺才装）
#   2. Ollama：缺则 brew 安装；qwen3:8b 缺则 pull（服务未跑则先拉起）
#   3. whisper.cpp：克隆进 vendor/、cmake 编译、下模型 large-v3-turbo-q5_0
#   4. Qwen3-ASR venv：vendor/qwen-asr-venv + torch/transformers/accelerate
#      + 预下载 HF 模型 Qwen/Qwen3-ASR-1.7B（--no-qwen-model 可跳过）
#   5. tools-venv：vendor/tools-venv + numpy / opencv-python-headless /
#      pypinyin（录屏幻灯片帧处理 + OCR 修正建议；start.py 预检需要它）
#   6. 生成 config.json（已存在则保留不动——它是你的私有文件）
#   7. ./install.sh 装全局启动器 mst + PATH
#   8. 起一次服务实测 /api/status，全链路预检通过才报成功
#
# 三方依赖统一收在本仓库 vendor/ 下（固定区域，已 gitignore）。
# 跳过项：MST_SETUP_SKIP="ollama whisper qwen tools" ./setup.sh（空格分隔）
set -u

REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
VENDOR="$REPO/vendor"
W_BIN="$VENDOR/whisper.cpp/build/bin/whisper-cli"
W_MODEL_NAME="ggml-large-v3-turbo-q5_0.bin"
W_MODEL="$VENDOR/whisper.cpp/models/$W_MODEL_NAME"
QVENV="$VENDOR/qwen-asr-venv"
TVENV="$VENDOR/tools-venv"
SKIP=${MST_SETUP_SKIP:-}
want_qwen_model=1
[ "${1:-}" = "--no-qwen-model" ] && want_qwen_model=0

step() { printf '\n== %s\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }
warn() { printf '  ! %s\n' "$1"; }
die()  { printf '  ✗ %s\n' "$1" >&2; exit 1; }
has_skip() { case " $SKIP " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# ---- --check：只验证不安装（干跑），必需缺失退出码 1 -------------------------
if [ "${1:-}" = "--check" ]; then
    missing_req=""
    chk() { # chk <req|opt> <名称> <命令...>
        _kind=$1; _name=$2; shift 2
        if "$@" >/dev/null 2>&1; then
            ok "$_name"
        else
            if [ "$_kind" = req ]; then
                printf '  ✗ %s（必需，缺失）\n' "$_name"
                missing_req="$missing_req $_name"
            else
                warn "$_name（可选，未就绪）"
            fi
        fi
    }
    printf '== 一键装配自检（--check 只读，不安装任何东西）\n'
    [ "$(uname)" = "Darwin" ] || { printf '  ✗ 非 macOS\n'; exit 2; }
    chk req "git"            command -v git
    chk req "python3"        python3 -c 'import sys; assert sys.version_info[:2] >= (3,10) and sys.version_info[:2] <= (3,12)'
    chk req "ffmpeg"         command -v ffmpeg
    chk req "ollama"         command -v ollama
    if command -v ollama >/dev/null 2>&1; then
        chk req "ollama 服务"    curl -sf --max-time 3 http://127.0.0.1:11434/api/tags
        ollama list 2>/dev/null | grep -q "qwen3:8b" || { printf '  ✗ 模型 qwen3:8b（必需，缺失）\n'; missing_req="$missing_req qwen3:8b"; }
    fi
    # whisper 路径口径与网页面板一致：config.json > vendor 默认
    W_CK_BIN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]+"/config.json")).get("whisper_bin",""))' "$REPO" 2>/dev/null)
    W_CK_MODEL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]+"/config.json")).get("whisper_model",""))' "$REPO" 2>/dev/null)
    W_CK_BIN=${W_CK_BIN:-$W_BIN}; W_CK_MODEL=${W_CK_MODEL:-$W_MODEL}
    [ -x "$W_CK_BIN" ] && ok "whisper-cli（$W_CK_BIN）" || { printf '  ✗ whisper-cli（必需，缺失）\n'; missing_req="$missing_req whisper"; }
    [ -s "$W_CK_MODEL" ] && ok "whisper 模型（$W_CK_MODEL）" || { printf '  ✗ whisper 模型（必需，缺失）\n'; missing_req="$missing_req whisper-model"; }
    if [ -x "$QVENV/bin/python" ] && "$QVENV/bin/python" -c "import torch, transformers, accelerate, qwen_asr" >/dev/null 2>&1; then
        ok "Qwen3-ASR venv"
    else
        warn "Qwen3-ASR venv（可选，未就绪——中文增强转写缺它质量下降）"
    fi
    if [ -x "$TVENV/bin/python" ] && "$TVENV/bin/python" -c "import numpy, cv2, pypinyin" >/dev/null 2>&1; then
        ok "tools-venv"
    else
        warn "tools-venv（可选，未就绪——仅录屏事件需要）"
    fi
    [ -f "$REPO/config.json" ] && ok "config.json" || { printf '  ✗ config.json（必需，缺失——跑一次 ./setup.sh 自动生成）\n'; missing_req="$missing_req config.json"; }
    command -v mst >/dev/null 2>&1 && ok "启动器 mst" || warn "启动器 mst（未安装——./install.sh 可装）"
    if [ -n "$missing_req" ]; then
        printf '\n✗ 必需依赖缺失:%s\n  一键补齐：./setup.sh\n' "$missing_req"
        exit 1
    fi
    printf '\n✓ 必需依赖全部就绪。开始使用：mst\n'
    exit 0
fi

# ---- 0. 前置 --------------------------------------------------------------
step "0/8 前置检查"
[ "$(uname)" = "Darwin" ] || die "本装配器面向 macOS（当前: $(uname)）"
command -v git >/dev/null 2>&1 || die "缺 git（先装 Xcode Command Line Tools: xcode-select --install）"
command -v python3 >/dev/null 2>&1 || die "缺 python3（xcode-select --install 或 brew install python@3.12）"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PYMAJ=${PYV%.*}; PYMIN=${PYV#*.}
[ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -ge 10 ] || die "需要 python 3.10+（当前 ${PYV}）"
[ "$PYMIN" -le 12 ] || warn "python $PYV 未经项目验证（推荐 3.12）；继续但有风险"
if ! command -v brew >/dev/null 2>&1; then
    die "缺 Homebrew。先执行:
  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
然后重跑 ./setup.sh"
fi
ok "macOS / python $PYV / git / Homebrew 就绪"

# ---- 1. ffmpeg + cmake ----------------------------------------------------
step "1/8 ffmpeg + cmake（音频预处理 / 编译工具）"
if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg 已有: $(command -v ffmpeg)"
else brew install ffmpeg || die "brew install ffmpeg 失败"; ok "ffmpeg 已安装"; fi
if command -v cmake >/dev/null 2>&1; then ok "cmake 已有"
else brew install cmake || die "brew install cmake 失败"; ok "cmake 已安装"; fi

# ---- 2. Ollama + qwen3:8b ---------------------------------------------------
step "2/8 Ollama + qwen3:8b（本地 LLM 阶段）"
if has_skip ollama; then warn "按 MST_SETUP_SKIP 跳过 ollama"
elif command -v ollama >/dev/null 2>&1; then ok "ollama 已有"
else brew install ollama || die "brew install ollama 失败"; ok "ollama 已安装"; fi
if ! has_skip ollama; then
    if ! curl -sf --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        warn "Ollama 服务未在跑，拉起（open -a Ollama）…"
        open -a Ollama 2>/dev/null || ollama serve >/dev/null 2>&1 &
        i=0; while [ $i -lt 30 ]; do
            curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
            sleep 2; i=$((i + 1))
        done
        curl -sf --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 \
            || warn "Ollama 未就绪（start.py 首次启动会再等 60s，先继续）"
    else ok "Ollama 服务在跑"; fi
    if ollama list 2>/dev/null | grep -q "qwen3:8b"; then ok "模型 qwen3:8b 已存在"
    else printf '  … pull qwen3:8b（约 5GB，进度如下）\n'
         ollama pull qwen3:8b || warn "pull 失败——可稍后手动 ollama pull qwen3:8b"; fi
fi

# ---- 3. whisper.cpp --------------------------------------------------------
step "3/8 whisper.cpp + 模型 ${W_MODEL_NAME}（ASR 基座）"
if has_skip whisper; then warn "按 MST_SETUP_SKIP 跳过 whisper"
else
    mkdir -p -- "$VENDOR"
    if [ -x "$W_BIN" ]; then ok "whisper-cli 已编译: $W_BIN"
    else
        if [ ! -d "$VENDOR/whisper.cpp" ]; then
            git clone --depth 1 https://github.com/ggml-org/whisper.cpp \
                "$VENDOR/whisper.cpp" || die "克隆 whisper.cpp 失败（网络？）"
        fi
        (cd "$VENDOR/whisper.cpp" && cmake -B build >/dev/null \
            && cmake --build build -j --config Release >/dev/null) \
            || die "whisper.cpp 编译失败（查看上方 cmake 输出）"
        [ -x "$W_BIN" ] || die "编译完成但未找到 $W_BIN"
        ok "whisper.cpp 编译完成"
    fi
    if [ -f "$W_MODEL" ]; then ok "模型已存在: $W_MODEL"
    else printf '  … 下载模型 %s（约 575MB）\n' "$W_MODEL_NAME"
         (cd "$VENDOR/whisper.cpp" && bash models/download-ggml-model.sh \
             large-v3-turbo-q5_0) || die "模型下载失败（网络？重跑 setup.sh 续传）"
         ok "模型就绪"; fi
fi

# ---- 4. Qwen3-ASR venv -------------------------------------------------------
step "4/8 Qwen3-ASR venv（中文/混说 ASR，推荐）"
if has_skip qwen; then warn "按 MST_SETUP_SKIP 跳过 qwen venv"
elif [ -x "$QVENV/bin/python" ] && "$QVENV/bin/python" -c "import torch, transformers, accelerate, qwen_asr" >/dev/null 2>&1; then
    ok "venv 已就绪: $QVENV"
else
    python3 -m venv "$QVENV" || die "创建 venv 失败"
    "$QVENV/bin/pip" install --upgrade pip >/dev/null || die "pip 升级失败"
    printf '  … 安装 torch / transformers / accelerate / qwen-asr（体积较大，请耐心）\n'
    "$QVENV/bin/pip" install torch transformers accelerate qwen-asr \
        || die "pip 安装失败（网络/磁盘？）重跑 ./setup.sh 续装"
    ok "venv 依赖安装完成"
fi
if [ "$want_qwen_model" -eq 1 ] && ! has_skip qwen; then
    printf '  … 预下载 HF 模型 Qwen/Qwen3-ASR-1.7B（首次运行才不用等）\n'
    "$QVENV/bin/python" - <<'PYEOF' || warn "HF 模型预下载失败——首次中文编译时会再自动下载"
from huggingface_hub import snapshot_download
snapshot_download("Qwen/Qwen3-ASR-1.7B")
print("  ✓ HF 模型缓存就绪")
PYEOF
fi

# ---- 5. tools-venv ------------------------------------------------------------
step "5/8 tools-venv（录屏幻灯片帧处理 + OCR 修正建议：numpy / opencv / pypinyin）"
if has_skip tools; then warn "按 MST_SETUP_SKIP 跳过 tools venv（start.py 预检会要求它，编译前需补装）"
elif [ -x "$TVENV/bin/python" ] && "$TVENV/bin/python" -c "import numpy, cv2, pypinyin" >/dev/null 2>&1; then
    ok "venv 已就绪: $TVENV"
else
    mkdir -p -- "$VENDOR"
    python3 -m venv "$TVENV" || die "创建 tools-venv 失败"
    "$TVENV/bin/pip" install --upgrade pip >/dev/null || die "pip 升级失败"
    printf '  … 安装 numpy / opencv-python-headless / pypinyin\n'
    "$TVENV/bin/pip" install numpy opencv-python-headless pypinyin \
        || die "pip 安装失败（网络/磁盘？）重跑 ./setup.sh 续装"
    "$TVENV/bin/python" -c "import numpy, cv2, pypinyin" >/dev/null 2>&1 \
        || die "tools-venv 依赖自检失败（import numpy, cv2, pypinyin）"
    ok "tools-venv 依赖安装完成"
fi

# ---- 6. config.json ----------------------------------------------------------
step "6/8 config.json（只在缺失时生成，绝不覆盖你的现有配置）"
if [ -f "$REPO/config.json" ]; then
    ok "config.json 已存在，保留不动（如需重置：rm config.json 后重跑）"
else
    python3 - "$REPO" "$W_BIN" "$W_MODEL" "$QVENV/bin/python" <<'PYEOF' \
        || die "config.json 生成失败"
import json, sys
repo, wbin, wmodel, qpy = sys.argv[1:5]
cfg = {"whisper_bin": wbin, "whisper_model": wmodel, "qwen_python": qpy,
       "ollama_url": "http://127.0.0.1:11434", "ollama_model": "qwen3:8b"}
with open(f"{repo}/config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("  ✓ config.json 已生成（全部指向 vendor/，已 gitignore）")
PYEOF
fi

# ---- 7. 全局启动器 -------------------------------------------------------------
step "7/8 全局启动器 mst"
sh "$REPO/install.sh" || die "install.sh 失败"

# ---- 8. 实测起服 ----------------------------------------------------------------
step "8/8 起服实测（预检全过 + /api/status 200 才算装配成功）"
PORT=18877
LOG=$(mktemp /tmp/mst-setup-verify.XXXXXX)
python3 "$REPO/start.py" --no-browser --port "$PORT" > "$LOG" 2>&1 &
VPID=$!
verified=0
i=0; while [ $i -lt 20 ]; do
    if curl -sf --max-time 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
        verified=1; break
    fi
    kill -0 $VPID 2>/dev/null || break
    sleep 1; i=$((i + 1))
done
kill $VPID 2>/dev/null; wait $VPID 2>/dev/null
rm -f "$LOG"
[ "$verified" -eq 1 ] || die "实测未通过——请把上面输出贴给维护者排查"
ok "预检通过，/api/status 200"

printf '\n%s\n' "=============================================================="
echo "装配完成 🎉  新开一个终端（或 source ~/.zshrc），然后："
echo "    mst        # 打开 http://127.0.0.1:8788 并自动弹浏览器"
echo "想先验证全链路：bash scripts/demo.sh（合成事件，真实模型，几分钟）"

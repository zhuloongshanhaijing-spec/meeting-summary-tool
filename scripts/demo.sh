#!/usr/bin/env bash
# 30-second demo: synthesize a bilingual talk → compile it → query it via MCP.
# Prereqs: config.json filled in (see INSTALL.md), Ollama running, macOS say+ffmpeg.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] 生成合成 demo 事件（零隐私内容）"
python3 scripts/make_demo_event.py

echo "[2/4] 编译（ASR → 证据 → 主题单元 → 审计 → 打包，全本地）"
python3 run_meeting.py

echo "[3/4] 通过 MCP 检索（与 VS Code/Roo-Code/Claude Desktop 同协议）"
python3 scripts/ask.py "机器人社招新是什么时候" --event demo-event

echo "[4/4] 英文提问（跨语言路径与已知限制见 README）"
python3 scripts/ask.py "robotics club recruitment" --event demo-event

echo "完成：每条回答都带 U/A/R 证据 ID 与时间戳，可回查 02_逐句会议记录.md"

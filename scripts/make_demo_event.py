#!/usr/bin/env python3
"""Generate a synthetic demo event (zero privacy content).

Uses macOS `say` offline TTS to create one bilingual (~40s) wav plus a
matching notes.md, laid out under input/demo-event/. The demo proves the
full pipeline chain on machine-generated speech — README makes clear the
quality bar for real events is much higher.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENT = ROOT / "input" / "demo-event"

# Bilingual script: two zh paragraphs + one en paragraph, numbers and a
# named entity included so evidence IDs have something concrete to anchor.
ZH_TEXT = (
    "大家好，欢迎参加本周的项目例会。今天要讲三件事。"
    "第一，图书馆的开放时间从下周一开始延长到晚上九点。"
    "第二，机器人社团将在十月十五号举办招新活动，地点是二楼活动室。"
    "第三，预算方面，本学期剩余经费是四千二百元。"
    "下面请负责外联的同学介绍合作情况。"
)
EN_TEXT = (
    "Hello everyone. The partner school confirmed the exchange visit "
    "for November. They will send twelve students and two teachers, "
    "and our robotics club will host a joint workshop on campus."
)

NOTES_MD = """# 会议笔记（demo）

- 图书馆延长开放 → 下周一起，到 21:00
- 机器人社招新：10 月 15 日，二楼活动室
- 经费：本学期剩 4200 元
- 外联：十一月交换生来访（12 学生 + 2 老师），机器人社承办 workshop
- 疑问（录音可能没提）：招新要不要报名费？
"""


def main() -> int:
    if shutil.which("say") is None or shutil.which("ffmpeg") is None:
        print("此 demo 生成器依赖 macOS 的 say 与 ffmpeg（Linux 用户可自备任意短音频放入 input/demo-event/）")
        return 1
    EVENT.mkdir(parents=True, exist_ok=True)
    zh_aiff, en_aiff = EVENT / "demo_zh.aiff", EVENT / "demo_en.aiff"
    wav = EVENT / "demo-talk.wav"
    subprocess.run(["say", "-v", "Tingting", "-o", str(zh_aiff), ZH_TEXT], check=True)
    subprocess.run(["say", "-v", "Samantha", "-o", str(en_aiff), EN_TEXT], check=True)
    # concat -> 16k mono wav (pipeline's expected input shape)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(zh_aiff), "-i", str(en_aiff),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,aresample=16000,aformat=channel_layouts=mono[out]",
        "-map", "[out]", str(wav),
    ], check=True)
    zh_aiff.unlink(); en_aiff.unlink()
    (EVENT / "notes.md").write_text(NOTES_MD, encoding="utf-8")
    dur = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout.strip()
    print(f"demo 事件就绪: {wav.name} ({float(dur):.0f}s) + notes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())

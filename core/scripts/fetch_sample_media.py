#!/usr/bin/env python3
"""Acquire test material for the meeting pipeline.

Three sources, in order of network independence:

1. --synthesize / --preset <name>   Local TTS via macOS `say` (no network).
   Produces realistic bilingual lecture audio, including staff-logistics
   phrases so the relevance filter can be exercised on purpose.
2. --url <url>                       Direct download (ffprobe-validated).
   Note: this sandbox only reaches raw.githubusercontent.com over IPv4;
   curl is invoked with -4 for that reason.
3. --preset jfk                      Known-good public-domain human speech.

Everything lands in input/<event>/<name>.m4a so each preset becomes its own
event for run_meeting.py. Existing files with the same byte size are kept
(idempotent re-runs).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
INPUT_DIR = WORKSPACE / "input"

EN_SCRIPT = """Good afternoon, everyone. Welcome to the third session of our urban mobility lecture series. Today we will discuss congestion pricing and its effects on public transit usage.
Before we start, let me check the microphone. Testing, one two, one two. Can everyone hear me in the back row? Good. We will begin in about five minutes, please take your seats.
This lecture has three parts. First, the history of congestion pricing. Second, the London and Singapore case studies. Third, the proposed pilot for our own city.
Singapore introduced area licensing in nineteen seventy five. London followed with its congestion charge in two thousand three, charging five pounds per day initially, later raised to eight and then fifteen pounds.
Independent evaluations found traffic in central London dropped by roughly fifteen to twenty percent in the first two years, while bus ridership increased by about thirty percent.
Critics argue that congestion pricing is regressive. Supporters respond that revenue is reinvested into buses and subway lines, which disproportionately benefits lower income commuters.
In our own city, the pilot corridor is four point two kilometers long, with eleven entry points. The proposed fee is three dollars per crossing during peak hours, free before seven a.m. and after seven p.m.
Let me check the time. We are running slightly behind, so I will skip the slide on emission modeling. Please silence your phones for the rest of the session.
To summarize: pricing changes behavior, revenue matters, and public acceptance depends on visible transit investment. Next week we will cover parking policy. Thank you for attending today."""

ZH_SCRIPT = """各位老师、各位同学，下午好。欢迎参加今天的城市交通规划研讨会。今天我们讨论的主题是地铁网络规划与客流预测。
先试一下麦克风。喂，测试，一二三。后排的同学能听清吗？好，我们五分钟后正式开始，请大家先就座。
本次报告分为三个部分。第一，地铁线网的规划原则。第二，北京和上海的实际案例。第三，我们城市新线路的客流预测方法。
北京地铁二号线于一九七一年开通，是中国第一条地铁。到二零二四年，北京轨道交通运营里程已经超过八百公里，日均客运量超过一千万人次。
上海地铁于一九九三年开通首段线路，如今运营里程位居世界第一，超过八百公里，工作日客流经常突破一千二百万人次。
研究表明，地铁线路的客流预测误差通常在百分之十五到百分之二十五之间，主要受人口分布、接驳公交和票价政策的影响。
我们的新线路全长十八点六公里，设站十四座，预计初期日客运量为二十八万人次，远期达到四十五万人次。
提醒一下，会后请把问卷交给门口的工作人员，他们的工牌上有联系邮箱。稍后需要拷贝幻灯片的同学请举手登记。
总结一下：线网规划要以客流预测为基础，换乘站的设置直接影响运营效率，票价与公交的一体化是提升客流的关键。谢谢大家。"""

MIXED_SCRIPT = """Good morning everyone, welcome to the research group meeting. 今天的组会由我来汇报近期的实验进展。
First, a quick sound check. 测试麦克风，能听到吗？OK, we'll get started in five minutes, 大家先把座位坐好。
The first topic is data collection. 我们在过去两个月里一共采集了三千二百条语音样本，其中大约百分之四十包含中英文混说的情况，code-switching is actually very common in our recordings.
Second, the model. 目前的识别准确率是百分之九十一，比上个月提高了三个百分点，but the error rate on mixed-language segments is still twice as high as on monolingual ones.
第三点是关于标注规范。We need to decide whether a Chinese sentence with English brand names should be labeled as mixed or as Chinese. 我的建议是按主要语言标注，同时在备注字段记录英文词汇的比例。
Finally, the schedule. 下一周的deadline是周四，请大家提前把结果发到群里。Thanks everyone, that's all for today, 谢谢大家。"""

PRESET_TEXTS: dict[str, dict] = {
    "en-lecture": {"text": EN_SCRIPT, "voice": "Samantha", "lang": "en"},
    "zh-lecture": {"text": ZH_SCRIPT, "voice": "Eddy (Chinese (China mainland))", "lang": "zh"},
    "mixed-talk": {"text": MIXED_SCRIPT, "voice": "Eddy (Chinese (China mainland))", "lang": "mixed"},
}

PRESET_URLS: dict[str, str] = {
    # 11 s real human speech, public domain (US government work).
    "jfk": "https://raw.githubusercontent.com/ggerganov/whisper.cpp/master/samples/jfk.wav",
}


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout).get("format", {})
    except json.JSONDecodeError:
        return {}


def synthesize(spec: dict, target: Path) -> None:
    aiff = target.with_suffix(".aiff")
    say = shutil.which("say")
    if not say:
        raise SystemExit("macOS `say` not available; use --url instead")
    subprocess.run([say, "-v", spec["voice"], "-o", str(aiff), spec["text"]], check=True, timeout=600)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(aiff), "-vn", "-ac", "1", "-ar", "16000", str(target)],
        check=True, capture_output=True, timeout=300,
    )
    aiff.unlink(missing_ok=True)


def download(url: str, target: Path) -> None:
    # IPv4 forced: this sandbox cannot route IPv6 egress.
    subprocess.run(
        ["curl", "-4", "-sSL", "--max-time", "300", "--fail", url, "-o", str(target)],
        check=True, timeout=320,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=[*PRESET_TEXTS, *PRESET_URLS],
                        help="named sample to acquire")
    parser.add_argument("--url", help="arbitrary audio URL to download")
    parser.add_argument("--text-file", type=Path, help="synthesize this text file instead of a preset")
    parser.add_argument("--voice", help="voice name for synthesis (see `say -v ?`)")
    parser.add_argument("--event", default=None, help="event subfolder under input/ (default: preset name)")
    parser.add_argument("--name", default=None, help="output file stem (default: preset name)")
    parser.add_argument("--list-presets", action="store_true")
    args = parser.parse_args()

    if args.list_presets:
        for name, spec in {**PRESET_TEXTS, **{k: {"url": v} for k, v in PRESET_URLS.items()}}.items():
            kind = "tts" if name in PRESET_TEXTS else "download"
            print(f"{name:12s} [{kind}] {json.dumps(spec, ensure_ascii=False)[:100]}")
        return 0

    if args.text_file:
        if not args.text_file.is_file():
            raise SystemExit(f"no such file: {args.text_file}")
        spec = {"text": args.text_file.read_text(encoding="utf-8"),
                "voice": args.voice or "Samantha", "lang": "custom"}
        event = args.event or "custom-speech"
        stem = args.name or f"custom-{time.strftime('%H%M%S')}"
        acquire = lambda t: synthesize(spec, t)  # noqa: E731
    elif args.preset:
        event = args.event or args.preset
        stem = args.name or args.preset
        if args.preset in PRESET_TEXTS:
            spec = PRESET_TEXTS[args.preset]
            if args.voice:
                spec = {**spec, "voice": args.voice}
            acquire = lambda t: synthesize(spec, t)  # noqa: E731
        else:
            url = PRESET_URLS[args.preset]
            acquire = lambda t: download(url, t)  # noqa: E731
    elif args.url:
        event = args.event or "downloads"
        stem = args.name or "download"
        acquire = lambda t: download(args.url, t)  # noqa: E731
    else:
        parser.error("choose --preset, --url, or --text-file (or --list-presets)")

    event_dir = INPUT_DIR / event
    event_dir.mkdir(parents=True, exist_ok=True)
    target = event_dir / f"{stem}.{'wav' if args.preset == 'jfk' else 'm4a'}"
    if target.is_file() and target.stat().st_size > 1024:
        print(f"EXISTS {target} ({target.stat().st_size} bytes); keeping existing file")
    else:
        acquire(target)

    fmt = probe(target)
    duration = float(fmt.get("duration", 0))
    if not fmt or duration < 1:
        target.unlink(missing_ok=True)
        raise SystemExit(f"acquired file is not valid audio: {target}")
    print(json.dumps({
        "file": str(target.relative_to(WORKSPACE)),
        "bytes": target.stat().st_size,
        "duration_seconds": round(duration, 1),
        "format": fmt.get("format_name"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

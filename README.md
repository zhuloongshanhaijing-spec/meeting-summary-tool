# Meeting Summary Tool

**A local-first knowledge compiler for meetings and lectures.** It turns
recordings + your markdown notes into an evidence-linked knowledge package,
delivered to any AI chat client through MCP — so the AI answers *with
citations you can verify*, and you never have to read a transcript again.

会后知识编译器：把录音 + 个人笔记编译成证据链接的知识包，经 MCP 交给任意
AI 聊天客户端——AI 的每条回答都带可回查的证据编号与时间戳，而你从此不必读
逐字稿。

```
audio (m4a/wav) ─┐                                   ┌─> AI client (VS Code /
                 │   compile (fully local)           │     Roo-Code / Claude
notes (md) ──────┼─> run_meeting.py ──> meeting.db ──┴─>  Desktop, via MCP)
                 │      9 stages                     │
                 └─> 04_会议报告.md, 02_逐句记录.md   └─> ask.py (terminal)
```

## Why / 为什么做

Every claim in the knowledge package carries `U/A/R` identifiers:

- `U000046` — a topical **unit** (one distilled claim + certainty level)
- `A000057` — the **evidence**: the verbatim sentence it was derived from
- `R000283` — the **record**: source file + start/end timestamps

Your notes act as a **second evidence source** (supports / partial /
conflict / unknown against what was actually said) — if the recording never
mentions something in your notes, that is surfaced as `unknown`, not
silently dropped and not invented. Downstream AI clients save tokens by
searching the compiled package instead of re-reading transcripts.

知识包里的每条论断都带 U/A/R 编号：U=主题单元，A=它派生自的那句原话，R=源
文件与时间戳。笔记作为第二证据源与录音互相佐证（支持/部分/矛盾/未知），
录音没提到 ≠ 不存在，而是明确标为「未知」。

## Quick start (macOS) / 快速开始

```bash
# 1) install deps + fill in config.json   (see INSTALL.md)
cp config.example.json config.json   # then edit paths

# 2) compile the synthetic demo event (~4 min, fully local)
bash scripts/demo.sh

# 3) ask a question, get evidence-linked answers
python3 scripts/ask.py "机器人社招新是什么时候"
```

The demo event is machine-generated speech (zero privacy content) — it
proves the chain end-to-end: ASR → evidence → topic units → fidelity audit
→ package → MCP retrieval. Real meetings get much richer output.

(All scripts are invoked through `bash` / `python3`, so the package ships
without executable bits on purpose — `chmod` is not needed.)

## How it differs / 与现有项目的区别

| | this tool | meetily | Vexa |
|---|---|---|---|
| focus | post-meeting **compile + evidence-linked retrieval** | local meeting recorder | real-time meeting MCP |
| citations | claim ↔ sentence ↔ timestamp (U/A/R) | summary, no per-claim evidence links | flat transcript |
| notes as evidence | yes (second source, conflict detection) | no | no |
| delivery | MCP server (any client), 4 tools | UI-first | MCP, transcript-first |
| zh/en code-switching | first-class (per-track ASR arbitration) | en-first | en-first |

Honest scope: it is **not** a recorder, not realtime, not a GUI app, and it
does not do diarization (yet). The moat is thin — the value is the
evidence-link discipline.

## Privacy / 隐私

Everything runs on your machine: whisper.cpp / Qwen3-ASR / Ollama locally,
no cloud calls in the pipeline. The MCP server reads only `outputs/`.
Events whose directory ends in `_private` are never listed or searched.
Personal paths live in `config.json`, which is gitignored.

## Documentation / 文档

- `INSTALL.md` — dependencies, config, degradation modes (English-only mode etc.)
- `MCP_SETUP.md` — connecting VS Code / Roo-Code / Claude Desktop
- `ARCHITECTURE.md` — the 9-stage pipeline, ID system, audit guard
- `tests/` — 27 behavioral tests (`python3 -m unittest discover tests`)

## Known limitations / 已知限制

- macOS-first (demo generation uses `say`; pipeline itself is portable-ish,
  Linux needs `ffmpeg` + your own ASR setup — untested)
- A full install on a completely fresh machine has not yet been tested end to
  end. The published demo and clean-room checks used machines with the local
  ASR/LLM engines already installed.
- Chinese quality depends on Qwen3-ASR; without it you get English-only mode
- Single-event scaling tested to ~2h audio / ~900 sentence records
- The synthetic demo proves the chain, not transcription quality

## License

Source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE).
You may use, modify, and share it for noncommercial purposes; commercial use
requires separate permission from the licensor. This is **not** an
OSI-approved open-source license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
for externally installed dependencies.

许可要点（中文）：本项目为源代码可用（source-available），**不是** OSI 认证的开源许可
证——个人、研究、教育以及 PolyForm 条款所定义的非商业组织用途，可使用、修改与分享；
商业使用须另行获得许可人许可，详见 LICENSE 全文。

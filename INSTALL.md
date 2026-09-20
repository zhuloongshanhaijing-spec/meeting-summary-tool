# Install / 安装

macOS-first guide. Python **3.10+**, and everything below stays on your
machine. 中文要点见文末。

## 1. Base dependencies

```bash
brew install ffmpeg            # ffmpeg + ffprobe: audio prep, durations
```

Optional: if you already use the `arnndn` FFmpeg denoiser model, place
`cb.rnnn` in `~/.local/share/arnndn/`. The pipeline detects it automatically;
when it is absent, denoising is skipped and the pipeline continues. This model
is not bundled or downloaded by this repository.

Clone and check the test suite (no models needed yet):

```bash
python3 -m unittest discover tests   # expect: OK (27 tests)
```

## 2. whisper.cpp (required — ASR base engine)

```bash
git clone https://github.com/ggml-org/whisper.cpp
cd whisper.cpp && cmake -B build && cmake --build build -j --config Release
# download one model, e.g.:
bash ./models/download-ggml-model.sh large-v3-turbo-q5_0
```

Note the absolute paths of `build/bin/whisper-cli` and the `.bin` model —
you will paste both into `config.json`.

## 3. Ollama (required — pipeline LLM stages)

Relevance review, topic reconciliation, claim fidelity audit and note
classification all run on a local small model:

```bash
brew install ollama && ollama pull qwen3:8b
```

16GB machines: the pipeline loads/unloads this model per stage and never
runs two models at once — keep the default `keep_alive: 0` behavior.

## 4. Qwen3-ASR (optional — needed for Chinese/mixed audio)

English audio runs on whisper alone. Chinese or zh/en code-switched audio
needs the Qwen3-ASR engine (window mode) with whisper as cross-check:

```bash
python3 -m venv qwen-asr-venv
qwen-asr-venv/bin/pip install torch transformers accelerate
# offline model cache: export HF_HUB_OFFLINE=1 after pre-downloading
# Qwen/Qwen3-ASR-1.7B
```

**Degradation modes / 降级模式:**

| mode | what works | what you lose |
|---|---|---|
| whisper only | English events end-to-end | zh tracks (routed to whisper auto — hallucination-guarded) |
| whisper + Ollama | everything English | — |
| whisper + Ollama + Qwen3-ASR | full bilingual + code-switching | — (recommended) |

## 5. Configure

```bash
cp config.example.json config.json
# edit: whisper_bin, whisper_model, qwen_python (absolute paths)
```

`config.json` is gitignored — it is the only file that ever holds your
local paths. Every key can also come from an env var (`MST_WHISPER_BIN`,
`MST_WHISPER_MODEL`, `MST_QWEN_PYTHON`, `MST_OLLAMA_URL`, `MST_OLLAMA_MODEL`).
Missing keys produce one collective error listing everything.

## 6. Verify with the demo event

```bash
bash scripts/demo.sh
```

Expect: a synthesized bilingual talk (~40s) compiled in a few minutes,
quality gate `PASS`, then two MCP queries answered with `U/A/R` evidence
IDs. macOS only (`say`); on Linux, drop any short wav + notes.md into
`input/my-event/` and run `python3 run_meeting.py`.

## 中文要点

1. `brew install ffmpeg`；2. 编译 whisper.cpp 并记下二进制与模型绝对路径；
3. `ollama pull qwen3:8b`；4. 中文场景装 Qwen3-ASR venv；5. 复制
   `config.example.json` 为 `config.json` 填路径（环境变量亦可）；
6. `bash scripts/demo.sh` 跑合成 demo 验证全链路。所有阶段全本地，
   录音内容不出本机。

# Third-party notices

**External runtime dependencies — facts, not legal advice.** This repository
**does not distribute, mirror, or bundle** any third-party model weights,
FFmpeg, Ollama, whisper.cpp, Python virtual environments, or other third-party
binaries/caches. You obtain every runtime dependency yourself from its
**official source**; each component's own license then applies independently,
and you should read and accept those upstream terms yourself. Nothing here is
a product of this project, and nothing here means this project re-licenses or
redistributes any third-party component.

The web console (`webapp/`, `start.py`) and the launcher/bootstrap scripts
are **zero third-party dependencies**: Python 3.10+ standard library and
hand-written HTML/JS/CSS only — no frameworks, CDNs, fonts, or network
fetches at runtime.

This repository distributes no model weights, FFmpeg binaries, or other
third-party runtime artifacts. Users install the following dependencies
themselves and must review the upstream terms that apply to their chosen
versions and builds.

| Component | Role | Distributed here? | Upstream terms |
| --- | --- | --- | --- |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and a GGML Whisper model | base ASR | No | Review the upstream repository and the model source before download. |
| [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | optional Chinese / mixed-audio ASR | No | Review the model card and its license before download. |
| [Ollama](https://ollama.com/) with `qwen3:8b` | local pipeline LLM stages | No | Review Ollama's and the selected model's terms before use. |
| [FFmpeg](https://ffmpeg.org/) | audio preparation | No | Licensing depends on the build and enabled components; obtain it from your package manager or upstream. |
| `arnndn` `cb.rnnn` model | optional FFmpeg denoising | No | **Not distributed with this repository; no automatic download.** Used only if you already provide it locally; upstream source license is **UNCONFIRMED** — review it yourself before use. |

The source code in this repository is licensed separately under the PolyForm
Noncommercial License 1.0.0. Third-party notices do not grant any rights in
those external components.

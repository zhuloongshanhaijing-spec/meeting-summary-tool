# Third-party notices

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
| `arnndn` `cb.rnnn` model | optional FFmpeg denoising | No | Obtain separately and review the source and terms. |

The source code in this repository is licensed separately under the PolyForm
Noncommercial License 1.0.0. Third-party notices do not grant any rights in
those external components.

# RELEASE_MANIFEST — meeting-summary-tool v0.2.0 公开投放清单

> 版本：**v0.2.0（稳定版）** · 本轮未创建 tag/release，未推送任何远端。
> 许可边界：代码与文档 = **PolyForm Noncommercial 1.0.0**（LICENSE 逐字沿用，
> blob `b09ce33d…`）；对外定位 source-available；非商业使用无需申请；商业使用
> 须经 GitHub Issue 取得单独书面许可（COMMERCIAL_LICENSING.md）。
> 本包**不包含、不镜像、不内置下载**任何第三方模型权重、FFmpeg、Ollama、
> whisper.cpp、虚拟环境或第三方二进制/缓存（政策 §2–4，详见
> THIRD_PARTY_NOTICES.md 的 External runtime dependencies 声明）。

## 1. 逐文件清单（59 个内容文件 + 本清单 + FINAL_PUBLIC_SCAN.md）

### 根文档（14）

| 文件 | SHA-256（前 16） | 公开原因 |
|---|---|---|
| LICENSE | b09ce33dd2dff23c | 许可证全文（与公开仓库逐字一致） |
| README.md | 467e765c02eff4a0 | 项目说明（含外部运行时依赖节、网页 vs CLI、许可要点） |
| INSTALL.md | 7e4456034bec1fed | 安装指引（含外部依赖声明与官方来源、可选 arnndn 已标"不随包/许可未确认"） |
| CHANGELOG.md | 25d8b1ba85738226 | v0.2.0 稳定版发布说明 |
| PRIVACY_AND_DATA_FLOW.md | 9e2b9dba45340f61 | 隐私与数据流（`_private` 三面语义） |
| COMMERCIAL_LICENSING.md | c8a8afbe5a0e2b25 | 商业授权咨询指引 |
| CONTRIBUTING.md | b5a7fa57e79d4bb1 | 贡献与脱敏要求 |
| SECURITY.md | 99bfe016056329b7 | 安全报告渠道 |
| THIRD_PARTY_NOTICES.md | cd4c64d891391637 | 第三方事实声明（不随包分发/官方来源/许可独立适用） |
| ARCHITECTURE.md | 81465263ccaf0873 | 架构说明 |
| MCP_SETUP.md | 4603305d15b1e517 | MCP 接入指引 |
| config.example.json | a48b2fc14b589c08 | 占位配置模板（无真实值） |
| .gitignore | cb75aacca351bf10 | 排除 runs/outputs/input/config.json/vendor 等 |
| docs/PROGRESS_CONTRACT.md | 8b72939744e285f4 | 进度契约规格（用户文档） |

### 程序（22）

| 文件 | SHA-256（前 16） | 说明 |
|---|---|---|
| run_meeting.py | 5e5fd46b8ce6ea66 | 引擎+进度契约+input 自建 |
| config.py | bb8ccc3db6405344 | 配置解析 |
| start.py | 6e89d5f89f2ae295 | 一键启动（预检+起服，仅绑定 127.0.0.1） |
| webapp/server.py | 61c5fbaf1adf39e5 | 纯 stdlib 控制台服务 |
| webapp/static/{index.html, app.js, results.js, style.css} | d49cb440 / 07e0a0bc / d2d8543f / ed82f62c | 原生前端（无 CDN/框架） |
| core/scripts/×12 | （全量哈希见 §3 代码块） | 流水线+MCP 服务器 |
| note-layer/×6 | 同上 | 笔记佐证层 |
| setup.sh / install.sh | bd30294f / 956ec8a8 | 一键装配/启动器（拉取均为官方来源，声明见 INSTALL/NOTICES） |
| scripts/{ask.py, demo.sh, make_demo_event.py, smoke_webapp.py} | 7416c717 / 6c239d22 / 39cf0cf1 / b85c1a46 | 检索 CLI、合成 demo、真机 smoke |

### 测试（10）与 Issue 模板（3）

tests/×10（81 例，全量哈希见 §3）；`.github/ISSUE_TEMPLATE/`：bug_report.yml、
config.yml、commercial_licensing_inquiry.yml（无任何个人邮箱/姓名/账号）。

### 本清单与扫描报告

`RELEASE_MANIFEST.md`（本文件）与 `FINAL_PUBLIC_SCAN.md` 属投放包自证文档；
其自身哈希于负责人执行发布打包时生成核验。

## 2. 排除文件清单（未进入本目录，共 9 + 工作区侧 1）

| 排除项 | 原因 |
|---|---|
| OPEN_SOURCE_CANDIDATE_AUDIT / RELEASE_EVIDENCE_PACKET / DOC_CORRECTION_RECEIPT / FINAL_PACKAGING_RECEIPT / LICENSE_COMPATIBILITY_FACTS / RELEASE_READINESS / MANUAL_DECISIONS / FRESH_CLONE_VALIDATION / THIRD_PARTY_AND_DATA_FACTS | 治理/内部材料（负责人既定政策），含扫描规则、内部证据或决策过程 |
| 工作区根 PUBLIC_RELEASE_BASELINE.md 及其他私有材料 | 同上（且不在候选树内） |
| 任何模型权重/第三方二进制/vendor/runs/outputs/input/__pycache__/config.json/.env* | 政策 §2；目录内实测不存在（见 FINAL_PUBLIC_SCAN.md） |

## 3. 全量 SHA-256（59 文件，生成于 2026-09-27）

```
78eef85ad2dff23cb476ea623509167607299b7dd53c0121aee77c3d1e756161  .github/ISSUE_TEMPLATE/bug_report.yml
56470a86988639bcfa51cb3308bc3a07c2eba4d2af1dfc88382e26ebacfe9624  .github/ISSUE_TEMPLATE/commercial_licensing_inquiry.yml
89614eb3a23177abcf8f8f47a396ceb8e3bca7eec294341511ba99025e7b61ff  .github/ISSUE_TEMPLATE/config.yml
cb75aacca351bf107d79d5ae87a4bd94e98af1693ee132950649929623aafc25  .gitignore
81465263ccaf087395cd70eb9ba0c0083d898467f273c1fedfcf070fcebd79b6  ARCHITECTURE.md
25d8b1ba85738226ee3a92677c082d4c1b84d8a20d92c204ee5fe3328b6b3be5  CHANGELOG.md
c8a8afbe5a0e2b251d6c2768e493ac88217e6f5ae4bc73be30cc5e7d9f32f8fe  COMMERCIAL_LICENSING.md
b5a7fa57e79d4bb167fe9419d70e8629f1414554291b384dc4422f858fa18486  CONTRIBUTING.md
7e4456034bec1feda4de4df5e4b87e5b199fcd7002d9a891ebbbc844b907aa50  INSTALL.md
b09ce33dd2baf8a82beff68a3df3d02af5a950cd34ddc6f197db2190ed544bb9  LICENSE
4603305d15b1e51754d4655273e4909848e7eb7f2b7daee26c2f4e6db681e6ce  MCP_SETUP.md
9e2b9dba45340f6162084a7fbed5702adc3f1a6d5bd899cd37744e49ef113e9a  PRIVACY_AND_DATA_FLOW.md
467e765c02eff4a023c60534b7dd1240350a364aea4d0cbf092b62b16e8db223  README.md
99bfe016056329b71fc38c9bb8d05c1b6df343603fac11df50ae403d8d8235cd  SECURITY.md
cd4c64d8913916377945a5db5c58adcc66934afcc40ee6493914489fecdc627b  THIRD_PARTY_NOTICES.md
a48b2fc14b589c083b125bb1238f6a7eb76ab29864c6be794fda0da24520c655  config.example.json
bb8ccc3db640534419aad0e63b3d745a72bfdf70918f270ba84a47e54c691824  config.py
96c9e3801177dd9249396914b76c26e9581940867876d7843fd168003a6f2962  core/scripts/audit_claims.py
d9b64b3ea83277e3dc1ecb2cba68df7d65c5547159baa05daad0cbe30681f4e0  core/scripts/build_package_v3.py
8207ee787392c17951aa8664f0286b278451941eaf8abe71ef2b3ad7687d4287  core/scripts/mcp_meeting_server.py
fe5bd01121140e3c57a0453f60c7092aaed15a157be109c8a628645d26233e38  core/scripts/meeting_pipeline.py
a010a331f1e8be1b629426cf97718e1d5b3f86db4c417346393f0c966e3fcac2  core/scripts/prepare_audio.py
0e7295b5b2bc0bd0a66d72da6dd35d5c11ec24d4e495fe6ec05f588d97caab77  core/scripts/quality_gate.py
333decf63f36eb451a3de8dce76c78d34d8ee5c0d6d9cab840759c4e7c18551d  core/scripts/query_meeting.py
11db0c21d5ba9c3e4cc23a2d55a961f691c5e8637b99d10979f05ca837e743aa  core/scripts/relevance_filter.py
33a6a4a28332353f4333af5a08f7aa4d15b709ce4c848cddba13d5342817fd2a  core/scripts/run_ollama_reconcile.py
f79ce079c360f2ac043b18c42b57cef057192c86f06b7f36d4331f11d0434824  core/scripts/run_qwen3_asr.py
9eb4ae2601748cf129bb80d49fc9509427767f2e3f56c9953ece7d2cb4038c76  core/scripts/segment_asr_windows.py
7d8b0d1236b3c3770c40ed1079855eabe553c2ae9be9371c951743ddddd63f50  core/scripts/validate_package_v3.py
8b72939744e285f494ea0ab66d652fb14e9d549804b9d55272fb8caf76d07e21  docs/PROGRESS_CONTRACT.md
956ec8a88ef47fe13df9ddec243e176a81024337a5acc9d9dcbedd96ac542901  install.sh
445be25c8c41767f281d234364a54a035c32425b427cd5b016b768271e142f10  note-layer/EXECUTION_CHARTER.md
12c670fe1820453d59a7248fee92e64ae1ae455f5db921281b7c5b06d8e7bf18  note-layer/scripts/build_note_evidence.py
5968b8e0140ce4bff65d8d090c2c66dae2b530ac299cefd3dd7469796babec9b  note-layer/scripts/classify_note_links.py
126add7c41c7cfd0cd33f8862bde8efa4a36869483fdaa791650388b65228b86  note-layer/scripts/render_note_test.py
ba448bdb0029d1933e1b2932d32a9d5ed06455fa37531f611e5e0d51fac81396  note-layer/scripts/retrieve_note_links.py
7d396cad906376c9abc6c45e3b8da9d11c8dbc5970aacfcfe514c748b50b47bd  note-layer/scripts/sanitize_note_relations.py
5e5fd46b8ce6ea66474008a514927832f74d088454c6589b45f13b41cd8076ca  run_meeting.py
7416c717ffc1961a51bfff4bd1129187b19079e525503b1cf5f53ef1d78c720e  scripts/ask.py
6c239d226cbea26f6b34e789afd4c9ed558e4bae485f3048cbcf617c739ab51e  scripts/demo.sh
39cf0cf13ff83835aff4b2ccc601cd9c61f58b6c8e216417c050cd280dcafb93  scripts/make_demo_event.py
b85c1a46bf98ef9b22bad0b44ef4ae941a1371bfddafa897d02561d7dbaa657b  scripts/smoke_webapp.py
bd30294f19b4b03c4cb643354067c0b0a728b9463ccf00eba1b0dc06325668ea  setup.sh
6e89d5f89f2ae2950ec9cfe08063e116789d6089d20d4d5149ca9e68ecb30c8e  start.py
5cc352f9cd011889853ac62a4508c2961c94fd371571dbf4dbd0c5b6ba9059fe  tests/test_engine_arbitration.py
ba544ad8b09413053664ac2c2f799663b4a134539fd3abee7c34e97cd8b81832  tests/test_gate_per_track.py
f3786dacc13ddab801e9a7752533df8d59858be9448275ef1a72959c75ddd3db  tests/test_hallucination_score.py
45957f9740551f01bf70e1338cbb6f049f9a989681bd66482566aa1a3bd48982  tests/test_mcp_protocol.py
9c73ca9fabe674b567bc8fd565d2f35cb3bf1d5d8756c4145161253c1600a4df  tests/test_mcp_search.py
9f8ddba1d2dcfb5a1e4f5a4e73f3f7b2d86f20f8d387b8867fc96382084f302d  tests/test_private_exclusion.py
c1a2d68e7048367d455a5a70021c6bdb86c12ee6805d19762e193b23605880e3  tests/test_progress_hook.py
b07e12c8f6ced25ec7f9df431ad00792b54224d2a45bec77f8f923f74b4ce160  tests/test_start.py
14c63d35fbc4e658dd1282fce5efaa9c15d176d78bbe82ca33a41b99cde799ba  tests/test_webapp_outputs.py
a3e2b25a33def80b4fb9f34368491aad03d619b7303ee3513294bdb84182cd7a  tests/test_webapp_server.py
61c5fbaf1adf39e50bd450110d6cb2224aa3e40ec919a01abc0166bb10531c9b  webapp/server.py
07e0a0bcce1f284f640e86eb00678fed48746316c8889c5afe166d61abad1ba0  webapp/static/app.js
d49cb440a53728b9baf5b5631642ea2961fa9b29981acdbff576f1b7db72a93a  webapp/static/index.html
d2d8543f729cdf62b3f52c23a20f4207356fd302605cd485501bf0b7344f3e9e  webapp/static/results.js
ed82f62cb952ef4ddecc0202442f88275fb1be2084ba5b9467889a72ab551ff0  webapp/static/style.css
```

（说明：README/INSTALL/THIRD_PARTY_NOTICES/CHANGELOG 为本目录内的政策一致化
版本，故哈希与原候选目录不同；引擎/服务/测试等其余文件与原候选逐字节一致。）

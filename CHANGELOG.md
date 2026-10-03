# Changelog

本文件记录面向用户的显著变更。

## Unreleased

- 网页启动不再因缺少本地依赖而阻断；它显示依赖用途、必要性、下载量与官方来源，
  用户选择并授权后才在本机运行安装，支持复检和重试。
- 加入录屏处理：提取视频音轨、检测并确认幻灯片区域、抽取代表帧、使用 macOS
  Vision OCR，并把幻灯片内容作为带不确定性标记的补充证据。
- 多个零散输入先各自解析；页面仅展示有本地证据支持的候选分组、阅读顺序和缺口，
  不确定时不合并、不补写。
- 增加依赖恢复、视频输入、候选顺序和前端契约的本地测试。

## v0.2.0（2026-09-27）

在公开 v0.1.0-pre 的 CLI 编译器基础上，加入本地网页操作界面。

### 新增

- **本地网页控制台**（`webapp/` + `start.py`，纯 Python 标准库 + 原生前端，
  零第三方依赖）：拖拽上传录音/笔记、12 站阶段轨道实时进度、结果包在线阅读
  （Markdown 渲染）、整包 zip 下载、自定义输出目录（系统原生文件夹选择）。
  服务仅绑定 `127.0.0.1:8788`（可用 `--port` / `MST_WEB_PORT` 调整）。
- **进度契约**（`docs/PROGRESS_CONTRACT.md`）：`run_meeting.py` 新增
  `STAGE_ORDER` / `emit_progress()` / `resolve_output_dir()`；进度以
  `runs/<事件>/.progress.json` 快照 + `runs/progress.jsonl` 时间线旁路落盘，
  不改变 CLI 既有输出语义。
- **一键装配**（`setup.sh` + `install.sh`）：六步依赖装配自动化（幂等可续跑），
  装配后全局命令 `mst` 启动网页控制台；支持 `MST_SETUP_SKIP`、
  `--no-qwen-model`、`MST_ENTRY` 逃生口。
- **测试扩容**：新增进度契约（8）、网页服务端（19）、输出路由（10）、
  启动器（15）共 54 例（套件合计 81 例，全部无需真实模型即可运行）。
- **端到端冒烟**（`scripts/smoke_webapp.py`）：合成事件经网页全链路
  （上传→12 阶段→质量门禁→zip→停止语义）的真实验收脚本。
- 隐私文档 `PRIVACY_AND_DATA_FLOW.md`：明确 `_private` 事件在
  MCP / CLI / 本地网页三个可见性面的区别语义。

### 修复

- 全新克隆后首次运行：`input/` 目录缺失不再抛裸 `FileNotFoundError`
  回溯——由代码自动创建并给出投放指引（此前为原始 `os.listdir` ENOENT 崩溃）。
- 测试套件可独立运行：`tests/test_progress_hook.py` 自带哑配置环境，
  不再依赖测试模块导入顺序。

### 许可与文档

- 发布文案一致化：全仓统一 source-available 定位；明确**非商业使用无需申请**、
  **商业使用需另行书面许可**（GitHub Issue 商业咨询入口 +
  `COMMERCIAL_LICENSING.md` + Issue 模板 Commercial licensing inquiry）。
  LICENSE 文本未改动。

### 兼容性

- CLI 行为（stdout 日志、报告包内容、退出码、`--name/--skip-asr/--skip-notes`）
  与 v0.1.0-pre 一致；进度文件为旁路产物，不存在时引擎照旧。
- 许可证不变：PolyForm Noncommercial 1.0.0（逐字沿用）。

## [v0.1.0-pre] — 2026-09-20

首个 source-available 预发布：9 阶段本地编译流水线、U/A/R 证据链、
MCP 服务器（4 工具）、合成 demo、27 例行为测试。
（详见 GitHub Release v0.1.0-pre 说明。）

# Changelog

本文件记录面向用户的显著变更。

## v0.3.0（2026-10-06）

在 v0.2.0 的本地网页控制台基础上，重点提升**碎片装配的稳定性**并重构**界面结构**。

### 新增

- **五视图无滚动界面**：控制台改为「投放 / 进度 / 装配 / 结果 / 环境」五个可跳转视图，
  页面本身不再长滚动、内容在视图内独立滚动；导航项各附一句话说明；支持哈希直链
  （如 `#assembly` 直达装配视图）与 skip-to-main 无障碍跳转。
- **队列守旧提示**：开始编译前若队列中存在此前遗留的待处理事件，会先弹出确认并
  点名列出，避免误处理历史队列。
- **环境视图**：依赖自检与安装独立成视图；缺失依赖的门禁横幅可一键直达。

### 修复

- **碎片装配顺序无关化（关键稳定性修复）**：判读顺序改由**内容指纹**决定，与上传
  先后、文件命名无关；修复了「会内时间序反转（先传结尾片段）」与「自然序批量上传」
  导致的异常拆组。新增回归测试锁定该性质。
- **装配缺省态提示修正**：已编译碎片存在但尚无计划时，正确显示「生成装配计划」
  入口，不再误报「尚无碎片事件」。
- **视图切换地址栏同步**：刷新/后退不再回到错误视图。

### 兼容性

- 默认端口仍为 `127.0.0.1:8788`（`--port` / `MST_WEB_PORT` 可调）。
- 进度契约扩展为 **14 站**（新增 `video_ingest`、`slide_align`），见
  `docs/PROGRESS_CONTRACT.md`；纯 CLI 输出语义不变。
- 许可证不变：PolyForm Noncommercial 1.0.0（逐字沿用）。

### 测试

- 套件 **343 例**，从仓库根执行
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`；
  无需真实模型即可运行，缺少可选**开发**依赖时相关用例自动 skip（非失败）。

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

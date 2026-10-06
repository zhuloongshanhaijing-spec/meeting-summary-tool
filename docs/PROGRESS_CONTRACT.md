# Progress contract（进度契约规格）

> 状态：**稳定契约**。网页控制台（`webapp/`）与引擎（`run_meeting.py`）之间的
> 唯一进度接口。改动本表 = 破坏性变更，必须同步更新
> `tests/test_progress_hook.py` 与 `webapp/server.py`。

## 1. 阶段顺序（STAGE_ORDER）

`run_meeting.STAGE_ORDER` 是唯一的阶段真源（14 站，键为稳定契约）：

| # | key | 中文标签 |
|---|---|---|
| 1 | `inventory` | 文件清单 |
| 2 | `video_ingest` | 视频分解（音轨 / 幻灯片） |
| 3 | `audio_prepare` | 音频预处理（降噪） |
| 4 | `lang_probe` | 语言探测 |
| 5 | `asr` | 语音识别 |
| 6 | `literal` | 组装逐句记录 |
| 7 | `evidence` | 生成证据 |
| 8 | `relevance` | 无关话语过滤 |
| 9 | `slide_align` | 幻灯片对齐 |
| 10 | `reconcile` | 主题提取与索引 |
| 11 | `audit` | claim 保真审计 |
| 12 | `notes` | 笔记佐证 |
| 13 | `package` | 构建报告包 |
| 14 | `validate` | 验证与质量门禁 |

**子阶段**：`asr` 阶段期间可发出 `asr.whisper` / `asr.segment` / `asr.qwen`，
映射规则：取第一个 `.` 前的基名 → 父阶段索引（即都显示为第 5 站）。

## 2. 事件产生机制（emit_progress）

引擎通过 `run_meeting.emit_progress(event, kind, stage, message, status, counters)` 发事件：

- **两个落点**（均为尽力而为，进度系统故障绝不阻断流水线）：
  - 追加一行 JSON 到 `runs/progress.jsonl`（时间线，`GET /api/logs` 消费）
  - 原子覆写 `runs/<event>/.progress.json`（当前状态快照）
- **kind 取值**：`event_start` / `stage` / `event_done` / `event_failed`
  （服务端停止时另发 `event_stopped`）
- **status 取值**：`running` / `done` / `failed` / `stopped`
- 快照含 `stage_index`（1–14，按上表）/ `stage_total`=14 / `stage_started`
  （该阶段开始时刻，供前端计算已耗时）/ `updated`

## 3. 机器可读的最终结果

- **最终状态**：`runs/<event>/.progress.json` 的 `status` 字段
  （`done` / `failed` / `stopped`）+ 对应 kind 的 jsonl 行。
- **最终产物**：`outputs/<event>/`（或 `.mst-output.json` 覆盖目录）下的
  报告包（`01_主题索引.md`、`02_逐句会议记录.md`、`04_会议报告.md`、
  `05_不确定与冲突.md`、`06_笔记佐证与冲突.md`、`00_使用说明.md`、
  `meeting.db`、`quality_gate_report.json`）。
- **逐轨 ASR 子计数**：语音识别阶段的服务端计数由磁盘工件
  （`runs/<event>/asr_windows/flat/*.wav` 与 `asr_primary/`）推导，
  引擎不额外发计数文件。

## 4. 输出目录覆盖（.mst-output.json）

网页上传时可在事件输入目录旁写 `.mst-output.json`：
`{"output_dir": "/abs/or/~/path"}`；引擎经 `resolve_output_dir()` 读取，
非法 JSON / 缺字段 → 静默回退默认 `outputs/<name>`。点文件对
`find_input_events()` 不可见，随事件成功清理一并删除。

## 5. 兼容性承诺

- 本契约**只增不改**：新增字段不破坏消费者；改键名/顺序属破坏性变更。
- 纯 CLI 用法不读也不写上述文件以外的任何新语义：CLI 输出（stdout 日志、
  报告包内容、退出码）与 v0.1.0-pre 保持一致；进度文件是旁路产物，
  不存在时一切照旧。

## 6. 自动化测试

`tests/test_progress_hook.py`（8 例）钉住：快照 schema、子阶段→父索引映射、
阶段切换时间戳、`event_done/event_failed` 终态、输出目录覆盖与非法回退、
进度故障不影响引擎。`webapp` 侧消费由 `tests/test_webapp_server.py` 覆盖。

## 7. 网页界面消费（v0.3.0 起）

编辑器（`webapp/static/app.js`）以**五个可跳转视图**消费本契约，页面本身不整页滚动：

| 视图 | 哈希 | 消费的契约数据 |
|---|---|---|
| 投放 | `#drop` | 队列、依赖门；开始编译前若队列有遗留事件先弹确认 |
| 进度 | `#track` | `stage_index`/`stage_total`/`stage_started` 渲染阶段轨道与已耗时；`runs/progress.jsonl` 时间线 |
| 装配 | `#assembly` | `.assembly-plan.json` 状态（`absent`/`draft`/`confirmed`）与候选分组 |
| 结果 | `#results` | `outputs/<事件>/` 报告包 |
| 环境 | `#env` | 依赖自检与安装 |

**运行状态语义**：`.progress.json` 的 `status` ∈ `running`/`done`/`failed`/`stopped`
是唯一的终态真源；网页不维护权威状态，重启时按磁盘对账（残留 pid 死亡 → 标"已停止"）。
碎片装配的判读顺序对用户不可见，但保证同一批碎片无论上传先后都得到相同分组与顺序。

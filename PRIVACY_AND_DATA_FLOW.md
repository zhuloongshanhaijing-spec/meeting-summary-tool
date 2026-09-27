# Privacy & data flow（隐私与数据流）

## 1. 总原则

全部处理在本机完成：ASR（whisper.cpp / Qwen3-ASR）与 LLM 阶段（Ollama）均为
本地进程，流水线**不发起任何云端调用**。仓库不分发模型权重与二进制
（见 THIRD_PARTY_NOTICES.md）。

## 2. `_private` 事件的三个可见性面（重要区别）

以 `_private` 结尾的事件目录（如 `raw_debug_private`）是文档化的隐私退出机制，
但**只作用于 MCP 检索面**：

| 面 | `_private` 事件可见？ | 依据 |
|---|---|---|
| **MCP 检索**（`core/scripts/mcp_meeting_server.py`，供 AI 客户端） | **不可见**：不列出、不检索 | `mcp_meeting_server.py` 的 `_dbs()` 显式跳过；`tests/test_private_exclusion.py` 固化 |
| **CLI 输出**（`run_meeting.py`） | **正常处理**：照常编译、报告包写入 `outputs/<名称>_private/` | 引擎层无 `_private` 概念——排除仅是检索层行为 |
| **本地网页控制台**（`webapp/`，仅绑定 127.0.0.1） | **可见**：结果包列表与队列会显示 `_private` 事件 | `webapp/server.py` 的输出扫描不做 `_private` 过滤 |

**一句话**：`_private` 的承诺是"不经 MCP 暴露给 AI 客户端"，不是"在本机界面上
隐藏"。网页控制台是**仅本地界面**（服务只绑 `127.0.0.1`，无鉴权——请勿反向
代理到网络），在此界面看到 `_private` 事件不构成对上述承诺的违反。

## 3. 数据流与持久化

```
浏览器上传 ──> input/<事件>/（音频原件 + 合并后的 notes.md + 可选 .mst-output.json）
     │                │
     │                └─ 失败/停止：保留在 input/ 供重试；成功：随事件目录清理
     ▼
run_meeting.py（子进程，串行）
     ├─ runs/<事件>/   中间工件（whisper JSON、逐句记录、证据、审计…）+ .progress.json
     ├─ runs/progress.jsonl（追加式时间线）
     └─ outputs/<事件>/（或 .mst-output.json 覆盖目录）最终报告包 + meeting.db
```

- **网页服务本身无权威状态**：不维护数据库，一切从磁盘（`input/`、`runs/`、
  `outputs/`）推导；重启服务自动对账（残留 pid 死亡 → 事件标"已停止"）。
- **导出**：网页「下载 zip」打包 `outputs/<事件>/` 全部内容——`_private` 事件
  同样可导出（本地操作，同第 2 节语义）。
- **日志**：引擎 stdout 仅写 `runs/`（`GET /api/logs` 取尾部分页）；
  网页访问不留存请求日志（`log_message` 已静默）。浏览器与网页之间为明文
  HTTP——本机回环，勿跨网络使用。
- **上传队列防竞争**：向"正在处理中的事件"再次上传会被 409 拒绝，
  防止成功清理误删追加上传的文件。

## 4. 公开材料策略

演示与文档素材一律使用**合成事件**（`scripts/make_demo_event.py`：macOS `say`
机器合成语音，内容为虚构例会），零隐私内容。真实会议录音、转写、纪要、
截图、日志一律不得进入仓库（CONTRIBUTING / SECURITY 同此要求）。

## 5. 配置与个人路径

真实本机路径只存在于 `config.json`（gitignore）或 `MST_*` 环境变量；
`config.example.json` 仅含占位符。网页上传的「输出目录」以
`.mst-output.json` 随事件保存（含用户本机绝对路径，位于 gitignore 的
`input/` 下，随成功清理删除）。

## 6. 使用范围（许可证边界）

本仓库为 source-available（PolyForm Noncommercial 1.0.0，非 OSI 开源）。
非商业使用按 LICENSE 直接进行，**无需申请**；商业使用不由本仓库许可证授权，
须先经 GitHub Issue 取得**单独书面许可**（见 COMMERCIAL_LICENSING.md），
提交咨询不等于获得授权。

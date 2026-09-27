# FINAL_PUBLIC_SCAN — v0.2.0 公开投放终扫描与验证记录

> 执行日期：2026-09-27（UTC）· 目录：`meeting-summary-tool-v0.2.0-public/`
> 方法：全部命令在本目录内以相对路径执行；测试/验证用 `python3 -B`（零字节码
> 写入）。fresh-clone 验证在 /tmp 空目录副本上进行，本目录树未变。

## 1. 强制扫描（命令 / 输出 / 退出码）

### 1a. 密钥与凭据模式 —— 零命中

```
$ grep -rIlE '(sk-[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{20,}|BEGIN (RSA|EC|OPENSSH) PRIVATE)' .
（无输出）
exit=1
```

### 1b. 个人路径 / 本机用户名 —— 零命中

```
$ grep -rIlE '/Users/[[:alnum:]_-]+\|Documents/Codex\|DeepSeek Harness\|bogon\|[A-Za-z0-9_-]+-MacBook' .
（无输出）
exit=1
```

### 1c. 真实会议内容标记（排除合成 demo 语境）—— 零命中

```
$ grep -rIn '例会录音\|纪要归档\|内部会议\|董事会' --include='*.py' --include='*.md' .
（无输出）
exit=1
```

### 1d. 模型权重 / 二进制 / 音视频 / 数据库 —— 零命中

```
$ find . -type f \( -name '*.bin' -o -name '*.rnnn' -o -name '*.gguf' -o -name '*.safetensors' \
    -o -name '*.pt' -o -name '*.wav' -o -name '*.m4a' -o -name '*.mp3' -o -name '*.db' \
    -o -name '*.aiff' -o -name '*.dylib' -o -name '*.so' -o -name '*.exe' \) -size +0
（无输出）
exit=0（find 对空结果返回 0；输出为空 = 零命中）
```

### 1e. 违禁文件与目录（嵌套 .git / .env* / pem / id_rsa / config.json / __pycache__ / .DS_Store / vendor / runs / outputs / input）—— 零命中

```
$ find . -name '.git' -o -name '.env*' -o -name '*.pem' -o -name 'id_rsa*' -o -name 'config.json' \
    -o -name '__pycache__' -o -name '.DS_Store' -o -name vendor -o -name runs -o -name outputs -o -name input
（无输出）
exit=0（同上，空结果）
```

### 1f. 治理文档残留 —— 不存在

```
$ ls OPEN_SOURCE_CANDIDATE_AUDIT.md RELEASE_EVIDENCE_PACKET.md DOC_CORRECTION_RECEIPT.md \
     FINAL_PACKAGING_RECEIPT.md LICENSE_COMPATIBILITY_FACTS.md RELEASE_READINESS.md \
     MANUAL_DECISIONS.md FRESH_CLONE_VALIDATION.md THIRD_PARTY_AND_DATA_FACTS.md
ls: DOC_CORRECTION_RECEIPT.md: No such file or directory
ls: FINAL_PACKAGING_RECEIPT.md: No such file or directory
（全部 9 个文件均报 No such file or directory）
```

### 1g. 违禁发布表述（Unreleased / 尚未发布 / 预计发布 / READY_TO_PUBLISH）—— 零命中

```
$ grep -rIn 'Unreleased\|尚未发布\|预计发布\|READY_TO_PUBLISH' --include='*.md' .
（无输出）
exit=1
```

（补充核对：全目录 grep "候选" 的命中均为流水线技术词汇——"词法候选层/候选录音
段落"等，与发布状态表述无关；"开源"自述仅存在于否定句"不是 OSI 认证开源许可"。）

## 2. 测试（本目录原位）

```
$ PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover tests
Ran 81 tests in 2.241s
OK
exit=0
（运行后 find __pycache__ = 0，目录零残留）
```

## 3. fresh-clone 最小验证（/tmp 空目录副本）

```
$ rsync -a --exclude __pycache__ . /tmp/public-fresh/ && cd /tmp/public-fresh
$ python3 -B -m unittest discover tests        → OK（81 例），tests_exit=0
$ python3 run_meeting.py（无 config）          → 集体报错逐项列出 MST_* 环境变量（无回溯）
$ MST_WHISPER_BIN=/dummy/w MST_WHISPER_MODEL=/dummy/m MST_QWEN_PYTHON=/dummy/q \
  python3 run_meeting.py                       → FileNotFoundError: input/ 中没有任何可处理内容…
                                                 input_auto_created=yes（目录由代码创建）
$ <真实依赖环境变量> python3 start.py --no-browser --port 18902
$ curl http://127.0.0.1:18902/                 → 200
$ curl http://127.0.0.1:18902/api/status       → 200
$ 监听核验（lsof -sTCP:LISTEN）                 → 127.0.0.1:18902（仅回环）
```

## 4. 结论

**READY_FOR_OWNER_PUBLISH**

- 59 个内容文件 + 本扫描与 RELEASE_MANIFEST；全项扫描零命中；81/81 测试 OK
  （exit 0）；fresh-clone 四步全部通过（测试/无配置报错/input 自建/网页 200 且
  仅回环监听）。
- 本轮未创建 tag/release、未推送、未登录；发布动作（含对 v0.2.0 的最终确认）
  由负责人执行。

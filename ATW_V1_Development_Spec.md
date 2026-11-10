# Agent Task Watcher (ATW) V1.0 — 冻结技术规范、开发计划与验收标准

- 文档版本：1.0-baseline
- 冻结日期：2026-11-09
- 状态：需求冻结；允许修复实现缺陷，功能增减须另建变更记录
- 目标系统：Windows 10/11、主流 Linux（优先 Ubuntu/Debian）
- 目标用户：使用 Codex、DeepSeek Harness、OpenCode 等 Agent 远程部署与运行 CPU/GPU 仿真、训练、测试的开发人员
- 核心目标：AI Agent 移交长期运行的计算任务后退出会话，由本地 ATW 程序零大模型调用地监督任务，并以钉钉群机器人单向通知状态

## 0. 定义、范围和必须遵守的限制

### 0.1 本期能力

1. 单机常驻：每台计算服务器安装一个 ATW 实例，随系统启动并自动恢复监控。
2. 跨平台：Windows/Linux 共用 Python 核心；各自使用原生后台启动机制。
3. 只监控现有任务：ATW V1 **不会**创建、结束、重启或自动重试计算进程；AI/外部工具自行启动计算。
4. 文件热接入：AI 使用 SSH 向指定目录提交 JSON，ATW 自动识别并验证。
5. 监控目标：PID + 进程创建时间优先，备选进程名+命令行唯一匹配；追踪一个根进程与所观察到的子进程树。
6. 证据：运行本次任务的日志、结果文件、进程生存及 CPU/GPU 指标；多条件 AND/OR/NOT 与持续时间。
7. 并行监控：同一实例允许多个任务同时受监控。
8. 结果：SUCCESS、FAILED、UNKNOWN、INTERRUPTED；告警与进度是事件，不能替换主状态。
9. 注册回执：提交成功的准确定义是已验证目标进程、成功落库并开始监测；AI 必须校验回执。
10. 动态更新：同一任务可提交更高版本的监控规则/通知配置；已绑定进程身份不可更新。
11. 状态持久化：SQLite 存储任务、进程身份、版本、事件、采样摘要、消息队列和启动会话。
12. 钉钉单向 Markdown 消息：加签、限流、去重、重试、全局默认与任务覆盖。
13. CLI：查看当前任务、历史、详情、回执和健康状态；不提供计算任务控制命令。
14. Skill：调用时每次询问用户日志/结果路径（均可选择默认）；未指定时 AI 自行确定独立运行路径，并写入 JSON。
15. 发布：第一轮 Python 源码部署；达到 V1.0 验收后制作 Windows/Linux 独立发行包；GitHub 开源。
16. 预留通知通道接口：以后可新增飞书、Telegram、邮件等；V1 实际实现钉钉。

### 0.2 V1 非目标

- 不提供集中式多机控制台、浏览器 Web UI、开放的服务端 HTTP API。
- 不接收钉钉聊天命令；钉钉仅向群发送消息。
- 不主动接管进程生命周期或调配 GPU，任务启动属于 AI/外部程序。
- 不支持跨用户进程保证、多个独立根进程绑定、AMD/Intel GPU 遥测。
- 不通过 LLM 解析日志、判断状态或定期汇报。
- 不通过任意 JSON 执行 Shell/Python 命令；JSON 为声明式只读监控规则。
- 不保证仅通过 PID 轮询能无遗漏跟踪全部逃逸/脱离的子进程。
- 不声称 HTTP Webhook 可以提供严格 exactly-once 送达。

### 0.3 不得误报的原则

1. 进程消失 ≠ 成功；证据不足时返回 UNKNOWN。
2. 非托管 Linux 进程的退出码可能不可得；退出码未知必须显示为 null/unknown，严禁猜测为 0。
3. 设备级 GPU 利用率不能直接用作单任务 GPU 利用率；有进程级指标才可按进程级规则评估。
4. 采样指标 unavailable ≠ 0；使用 TRUE/FALSE/UNKNOWN 三态规则引擎。
5. 任务注册成功 ≠ 计算成功 ≠ 钉钉送达成功；三者状态分别存储。
6. 重启后禁止对已经进入终态的旧任务重新监控或重新发送“首次接入”通知。

## 1. 技术栈

- Python >= 3.11；`pyproject.toml`；开发阶段使用 `uv` 或 `pip` 安装。
- `psutil`：进程、创建时间、CPU/内存、子进程发现。
- `nvidia-ml-py`/NVML：NVIDIA GPU 设备与可获得的进程级资源指标；无 GPU/驱动不可用时优雅退化。
- `watchdog`：目录变更通知；另外配置周期性扫描作为兜底。
- `pydantic` + 导出 JSON Schema：解析严格校验、类型和版本检查。
- Python `sqlite3`：持久化，启用 WAL 和 busy_timeout，事务提交；不需要外部数据库。
- `httpx`：钉钉 HTTP 客户端；签名使用 Python 标准库 HMAC/SHA256、base64 与 urlencode。
- `typer`：CLI；`pytest`：测试；可选 `ruff`：静态检查。
- Linux systemd；Windows Task Scheduler；后期 PyInstaller 独立打包。
- 默认本地轮询周期 10 秒；inbox 兜底扫描 5 秒；两者必须可配置。高频率任务仍应避免过量采样。

## 2. 高层架构和数据流

```text
A 端 AI Agent / Skill
  ├─ 开发、Git/SSH 部署、独立启动计算进程
  ├─ 按用户指定或 AI 默认确定日志和结果路径
  ├─ 查询 root PID + create_time，生成 register JSON
  ├─ 通过 SSH 原子投递 tasks/inbox/*.json
  └─ 通过 SSH 调用 `atw receipt <request_id> --wait 30 --json`
                          │
                          ▼
B 端 ATW 常驻服务 ─ File intake（watchdog + 扫描 + Pydantic）
  ├─ Process monitor (psutil / 进程树身份缓存)
  ├─ Resource monitor (CPU / RAM / NVIDIA GPU)
  ├─ Evidence monitor（本次运行日志/结果，轮转和读取位置）
  ├─ Rule engine（TRUE/FALSE/UNKNOWN，持续时间）
  ├─ Task state machine / Recovery manager
  ├─ SQLite: Tasks / Revisions / Observations / Events / Outbox
  └─ DingTalk notifier（中文 Markdown / HMAC / 限流 / 重试）
                          │
                          ▼
                       钉钉群
```

不依赖服务端开放端口。ATW 只访问本机资源和钉钉 HTTPS API。通知适配器 API 应可替换。

## 3. 文件交接协议 AFHP/1

### 3.1 路径约定

Linux 可配置示例：`~/.local/share/atw/`；Windows 可配置示例：`%LOCALAPPDATA%\ATW\`。路径必须从配置解析，严禁在核心模块写死。

```text
ATW_HOME/
├── config/config.yaml                 # 非敏感全局配置
├── tasks/inbox/                      # 请求待处理
├── tasks/accepted/                   # 已受理请求文件
├── tasks/rejected/                   # 拒绝请求文件
├── tasks/archive/                    # 终态任务历史快照
├── receipts/                         # 回执，按 request_id 命名
├── data/atw.db                       # 唯一的任务状态权威源
└── logs/atw.log                      # ATW 自身日志
```

仅 inbox 作为新请求入口。accepted/archive 不因服务重启再次登记。目录扫描顺序不代表任务版本顺序；数据库版本与唯一键决定最终结果。

### 3.2 原子提交与消费

1. AI 通过 SSH 将文件完整写入 `inbox/<request_id>.json.tmp`，关闭句柄。
2. 在同一文件系统内原子重命名为 `inbox/<request_id>.json`；Windows 覆盖既有目标应禁止，同名提交必须由幂等规则处理。
3. ATW 仅处理 `*.json`，并限制文件大小（默认 <=256 KiB）。
4. 校验完整 JSON、协议版本、请求 ID、revision、expires_at、路径访问范围和监控规则类型。
5. 校验进程唯一性、进程创建时间、同用户可访问性；先将任务与接入事件事务性落库。
6. 原子生成 `receipts/<request_id>.json`（先写临时文件再 rename）；原始请求移入 accepted 或 rejected。
7. 遇到临时 I/O 失败可重试；JSON 格式或策略错误直接拒绝并写回执。
8. 服务启动时扫描遗留 inbox 请求并按 request_id 幂等重放；保证重复提交不创建第二份任务。

### 3.3 标识、版本与不可变字段

- `request_id`：每次提交唯一、不可复用的字符串（建议 UUID 或 ULID）。同一 request_id 重复提交返回原结果；不同内容使用旧 request_id 时拒绝 `REQUEST_CONFLICT`。
- `task_id`：每次实际计算运行唯一；重跑要新 task_id；已终态任务不能重开。
- `revision`：注册必须等于 1；更新应等于当前 revision+1。
- `operation`：`register` 或 `update`。
- `expires_at`：仅限制尚未被受理的请求；已受理任务不受注册请求过期影响。
- `target`（包括 root PID/创建时间/匹配规则）不可更新；`rules`、`notifications`、任务展示用描述等为可更新字段。
- `expected_revision`：用于更新时的乐观并发控制。
- 更新格式统一采用 `changes` 顶层对象，且被包含的子对象**整体替换**；禁止含糊的深层 merge。

### 3.4 注册 JSON 完整示例

> 下面的 PID、路径和时间为示意；运行时由 Skill 自动生成。时间为含时区的 ISO 8601 字符串，创建时间取自 psutil 并保留足够精度。

```json
{
  "protocol": "atw.task.v1",
  "operation": "register",
  "request_id": "req-20261109-cfd-001",
  "task_id": "cfd-20261109-001",
  "revision": 1,
  "expires_at": "2026-11-09T23:00:00Z",
  "task": {
    "name": "油气管道甲烷扩散 CFD 仿真",
    "project": "pipeline-simulation",
    "run_started_at": "2026-11-09T20:00:00Z"
  },
  "target": {
    "kind": "pid",
    "pid": 12345,
    "create_time_epoch": 1794254400.125,
    "tree": {"enabled": true}
  },
  "evidence": {
    "run_directory": "/home/simrunner/project/runs/cfd-20261109-001",
    "log": {
      "path": "/home/simrunner/project/runs/cfd-20261109-001/run.log",
      "scope": "current_run",
      "encoding": "utf-8"
    },
    "result_files": [
      "/home/simrunner/project/runs/cfd-20261109-001/results/result.csv"
    ]
  },
  "rules": {
    "success": {
      "all": [
        {"kind": "process_tree_exited"},
        {"kind": "log_contains", "value": "Simulation completed"},
        {"kind": "file_exists_fresh", "path": "/home/simrunner/project/runs/cfd-20261109-001/results/result.csv"}
      ]
    },
    "failure": {
      "any": [
        {"kind": "log_contains", "value": "CUDA out of memory"},
        {"kind": "log_contains", "value": "Traceback (most recent call last)"}
      ],
      "finalize": "on_process_exit"
    },
    "warnings": [
      {"id": "cpu-idle", "kind": "cpu_below", "percent": 2.0, "duration_seconds": 900}
    ],
    "max_runtime_seconds": 14400
  },
  "notifications": {
    "on_accepted": true,
    "on_success": true,
    "on_failure": true,
    "on_unknown": true,
    "on_interrupted": true,
    "on_warning": true,
    "progress_interval_minutes": 30,
    "warning_repeat_minutes": 30
  }
}
```

**实现时需使 JSON Schema 与上例字段完全一致，不能出现示例可提交但 schema 拒绝的情况。** `create_time_epoch` 的精度按 psutil 可获取精度校验；任务实际开始时间、进程创建时间须与目标场景合理一致，登记时比较并记录。

### 3.5 使用进程名及命令行唯一匹配

`target` 可替换为：

```json
{
  "kind": "match",
  "name": "python",
  "cmdline_contains": ["simulate.py", "--run-id", "run001"],
  "match_mode": "unique",
  "tree": {"enabled": true}
}
```

`name`+`cmdline_contains` 执行组合匹配，命中 0 个 → `PROCESS_NOT_FOUND`；命中 >1 个 → `AMBIGUOUS_PROCESS`；恰好 1 个 → 锁定实际 PID+create_time，持久化身份，再开始监控。`match` 不得在运行期间漂移到别的同名进程。

### 3.6 更新请求示例

```json
{
  "protocol": "atw.task.v1",
  "operation": "update",
  "request_id": "req-20261109-cfd-002",
  "task_id": "cfd-20261109-001",
  "revision": 2,
  "expected_revision": 1,
  "expires_at": "2026-11-09T23:30:00Z",
  "changes": {
    "notifications": {
      "on_accepted": true,
      "on_success": true,
      "on_failure": true,
      "on_unknown": true,
      "on_interrupted": true,
      "on_warning": true,
      "progress_interval_minutes": 60,
      "warning_repeat_minutes": 30
    }
  }
}
```

被更新的 `notifications` 子对象整体替换，未提供的 `rules` 维持不变；规则中的持续时间累积在语义变更时重置。

### 3.7 回执协议

`receipts/<request_id>.json` 格式：

```json
{
  "protocol": "atw.receipt.v1",
  "request_id": "req-20261109-cfd-001",
  "task_id": "cfd-20261109-001",
  "revision": 1,
  "status": "accepted",
  "monitoring": true,
  "bound_process": {
    "pid": 12345,
    "create_time_epoch": 1794254400.125
  },
  "accepted_at": "2026-11-09T20:01:00Z",
  "message": "已接管该计算任务的监督"
}
```

失败回执 `status=rejected`、`monitoring=false`、`error={code,message}`；常见代码：`INVALID_JSON`、`UNSUPPORTED_PROTOCOL`、`SCHEMA_VALIDATION_FAILED`、`REQUEST_EXPIRED`、`PROCESS_NOT_FOUND`、`PROCESS_IDENTITY_MISMATCH`、`AMBIGUOUS_PROCESS`、`ACCESS_DENIED`、`TASK_ALREADY_EXISTS`、`TASK_FINALIZED`、`REVISION_CONFLICT`、`REQUEST_CONFLICT`、`UNSAFE_PATH`。

回执 `accepted + monitoring=true` = 已验证目标身份、持久化任务且开始采样；不保证目标计算成功、不代表钉钉通知已成功。

CLI `atw receipt <request-id> --wait 30 --json`：
- 成功返回 JSON 与退出码 0；
- rejected 返回 JSON 与非零退出码；
- 等待超时返回 `RECEIPT_TIMEOUT` 与非零退出码。

## 4. 进程、证据、规则与状态

### 4.1 进程绑定

- 同用户进程可读；PID+create_time 强身份匹配。
- 记录根进程的 `pid`, `create_time_epoch`, `exe`, `cmdline摘要`, `username`, `observed_at`。
- `kind=pid` 时应提供 `create_time_epoch`；由 Skill 事前查询并填入。验收时不得将只匹配 PID 视为身份确认。
- 每次采样递归查找子进程，为每个已发现子进程建立 `pid+create_time_epoch` 身份记录；持续跟踪此前已发现的子进程，即使父进程已退出。
- 明确标注轮询无法确保发现采样间隔内创建并脱离树的进程，V1 不扩张系统权限以承诺完全覆盖。
- 进程匹配失败时返回拒绝回执；运行中检测 PID 复用，应视原进程结束，绝对不能追踪新进程。

### 4.2 资源指标

- CPU：按进程树聚合采样；明确 psutil 进程 CPU 百分比可大于 100%（多核）。
- 内存：按进程汇总 RSS，采样中说明共享内存可能导致合计重复计量。
- GPU：优先按已绑定 PID 聚合进程级指标；GPU 标识使用 UUID，索引为辅助展示。
- GPU/驱动/指标不支持时返回 `unavailable`，规则评估 UNKNOWN；整卡利用率仅展示为设备背景，不代替单任务指标。
- `NVML_ERROR_NOT_SUPPORTED`、权限不足等须明确记录，不能误报 GPU 空闲。
- 采样周期默认 10 秒；首个 CPU 测量窗口可不完整，标为预热/UNKNOWN。

### 4.3 日志读取与结果归属

- 首选 Skill 为每次运行生成独立 `run_directory`；用户明确指定路径时尊重其选择，但验证文件归属和可读性。
- `scope=current_run`：只评价本次运行证据，允许先于登记开始的本次运行日志被读取（支持热接入已有任务）。
- 存储日志文件身份、文件大小、读取偏移、最近变更时间；支持日志追加、截断、轮转、UTF-8 多字节分片。
- 对复用路径、旧结果文件、日志时间戳模糊等情况，进行保守判定或产生可说明的 UNKNOWN；不能因旧文件存在误报 SUCCESS。
- `file_exists_fresh` 默认要求本次运行专属路径且存在本次运行证据（例如启动后产生/更新，以及必要时的任务标识或输出清单）；单独使用 mtime 不能绝对证明文件归属。
- 文件路径经过规范化和允许根目录检查；避免符号链接跳出许可路径。
- 第一版支持 `log_contains` 字面量匹配；日志型进度可额外提供受限的模板/正则提取，不允许执行任意脚本。
- 若既没有日志也没有成功文件条件、又无法获得可信退出码，默认不能判为 SUCCESS。

### 4.4 规则引擎

- 使用 `TRUE/FALSE/UNKNOWN` 三态布尔规则；AND/OR/NOT 及 `for_seconds`。
- 支持 `process_exited`, `process_tree_exited`, `log_contains`, `log_not_updated`, `file_exists_fresh`, `file_size_above`, `cpu_below`, `gpu_util_below`, `gpu_memory_above`, `elapsed_above`。
- `warnings` 产生事件而非终态；默认低资源活动只有在连续符合阈值时间后告警，且不应自动终止。
- 成功规则由任务 JSON 自定义，可以是仅“运行完成+有效结果文件”，也可以是多条件联合；全局模板只提供建议、不覆盖任务显式规则。
- 失败规则 `finalize` 支持 `immediate` 与 `on_process_exit`；`immediate` 进入 FAILED 并停止监测，但不杀死计算进程。
- `max_runtime_seconds` 默认告警并继续监督；如超时规则被任务设为终态，可标记 FAILED/UNKNOWN，但仍不控制进程。
- 规则冲突：优先记录 `RULE_CONFLICT`；若强制终态需明确失败优先，避免误报成功。
- 三态不完全满足成功条件时不得自动返回 SUCCESS。
- 对日志/结果路径不可访问要发送异常事件，并按声明的证据规则决定终态，严禁悄然忽略。

### 4.5 主状态机

```text
[register JSON]
    ├─ rejected -> 不生成任务，生成错误回执
    └─ accepted -> MONITORING
                   ├─ 持续 WARNING / PROGRESS 事件（保持 MONITORING）
                   ├─ 符合成功规则 -> SUCCESS
                   ├─ 符合失败规则 -> FAILED
                   ├─ 进程结束但证据不足 -> UNKNOWN
                   └─ 确认 B 端整机重启导致中断 -> INTERRUPTED

终态：SUCCESS / FAILED / UNKNOWN / INTERRUPTED
所有终态不可重新变更为 MONITORING；归档属性独立保存。
```

终态写入数据库与通知事件入 outbox 必须位于同一事务。程序恢复时不会再发送同一个 task_id 对应的“首次接入”事件。

## 5. 服务恢复、健康与不重复通知

1. 建立单实例互斥锁，防止同机多服务同时写入任务与消息。
2. SQLite 表（至少）：`tasks`, `task_revisions`, `processes`, `observations`, `events`, `notification_outbox`, `service_sessions`, `requests`。
3. 服务启动时获取本机启动标识：Linux boot_id；Windows 系统启动信息与本机持久化标记组合判断，明确记录可信度。
4. ATW 服务单独重启：对 MONITORING 任务重新校验 PID+create_time；原进程存在则恢复监控，恢复日志偏移、通知队列和告警去重信息；不重发接入通知。
5. 监控进程已结束但证据明确时，按证据判定；否则 UNKNOWN；绝不自动挂到相同 PID 的新进程。
6. 确认 B 端整机重启：此前非终态任务标记 INTERRUPTED，并投递一次中断通知；不能恢复已经在旧开机实例中结束的计算实例。
7. 已终态任务的 JSON 持久化归档，扫描 inbox/accepted/archive 时不得将其重新激活。
8. 服务恢复事件只在检测到真实的监控中断并恢复时通知一次；首次安装启动不误报为“崩溃恢复”。
9. 可选每日健康摘要：服务运行时长、现监控任务数、过去一天成功/失败/未知/中断统计、通知队列状态、GPU 采样能力；按配置时区发送。
10. 服务完全宕机期间无法自行向钉钉告警：V1 仅保证恢复时通知。外部看门狗/独立心跳服务是未来扩展。
11. 通知 outbox 独立处理：去重键由 `task_id + event_type + event_instance` 构成；网络失败指数退避，服务器成功回复时记录已送达。

## 6. 钉钉通知

### 6.1 协议

- 自定义机器人 Webhook，`msgtype=markdown`，字段 `markdown.title` 与 `markdown.text`。
- 本机配置 Webhook URL 与加签密钥；运行时 `timestamp + "\n" + secret` 计算 HMAC-SHA256，再 base64、urlencode。
- 使用 HTTPS，HTTP 成功且 API JSON `errcode == 0` 才确认 `delivered`。
- 消息以中文为主，保留状态码、PID、CPU/GPU、路径等必要英文。
- 标题统一含 `[ATW]`，支持关键词安全策略；正文限制日志片段与敏感信息泄露，默认不发送原始完整日志。
- 实现可插拔 `Notifier` 抽象，V1 只做 DingTalk。

### 6.2 通知事件

`MONITOR_ACCEPTED`, `PROGRESS`, `WARNING`, `SUCCESS`, `FAILED`, `UNKNOWN`, `INTERRUPTED`, `SERVICE_RECOVERED`, `DAILY_HEALTH`。

默认：接入、所有终态和首次警告立即排队；周期进度 30 分钟；重复告警 30 分钟；健康摘要可选。任务配置可以覆盖全局通知频率。

### 6.3 限流与异常

- 文档规定自定义机器人最多 20 条/分钟；ATW 默认为同机器人最多 15 条/分钟，采用可配置滑动窗口/令牌桶。
- 紧急消息优先，必要时把多条进度消息合并摘要；重要事件持久化保存，避免被限流直接丢弃。
- 网络失败/超时可重试，超时后服务端是否成功可能未知；目标是“至少一次尝试、持久化、去重”，不得宣传严格 exactly-once。
- API 返回 `errcode != 0` 视发送失败；错误日志屏蔽完整 URL、access_token、secret 和 sign。
- 钉钉自定义机器人有下线风险，保持 notifier 插件封装，后续可替换通道。

### 6.4 消息示例

```markdown
### [ATW] 仿真任务成功完成

- **任务名称：** 油气管道甲烷扩散 CFD 仿真
- **计算节点：** B-Linux / RTX 3090
- **任务编号：** `cfd-20261109-001`
- **最终状态：** SUCCESS
- **运行时长：** 1小时38分钟
- **进程退出码：** 未知（外部进程）
- **判断证据：** 进程树退出；完成日志命中；本次结果文件存在
- **结果路径：** `/home/simrunner/project/runs/cfd-20261109-001/results`

备注：本消息由 ATW 自动发送，运行进程由 AI/用户管理。
```

## 7. Skill：atw-task-handoff

### 7.1 调用时强制的一次路径询问

每次调用 Skill，向用户询问：

> 本次任务的日志路径和结果输出路径分别是什么？两项都可留空或回复“默认”，未指定项由 AI 按项目结构自行确定，并写入监控 JSON。

**用户响应行为：**
- 用户提供两个路径：按用户选择使用，检查权限/归属；必要时说明不适用的原因并提出最小改动。
- 仅提供一个路径：另一项由 AI 生成。
- 留空或选择默认：AI 根据项目目录、运行 ID 自动设置独立路径（建议 `runs/<task-id>/run.log`、`runs/<task-id>/results/`）。
- 不重复询问已提供的答案；同一次 Skill 调用最多询问一次路径相关问题。
- 已经在运行的程序有既定路径时，AI 不得凭空假设能更改该程序的日志/结果位置；应读取真实既有位置并向用户说明限制；不得创建空文件冒充运行结果。

### 7.2 交接步骤

1. 获得用户路径偏好并解析当前 B 端操作系统、工程目录、命令、结果定义、预期运行时长。
2. 明确计算由 AI/外部程序启动，ATW 仅监测；确保程序独立于 SSH 会话长期运行。
3. 获得 root PID + 精确 create_time；若无法明确 PID，使用名称+完整关键命令行严格唯一匹配。
4. 根据真实日志/结果确定 success/failure/warnings/progress 规则；不能为了拿到成功通知而放宽规则。
5. 生成符合 Schema 的 JSON，放在用户指定的运行目录以便审计，再通过 SSH 原子投递到 ATW inbox。
6. `atw receipt <request_id> --wait 30 --json` 查询回执；验证 accepted、monitoring true、绑定 PID+create_time 与预期一致。
7. 再次核对进程存活、正确脱离 SSH；报告 `task_id`、监控状态、日志、结果位置、预计通知策略。
8. 结束本次 AI goal，不进行持续轮询，不消耗额外 LLM token。
9. 如果 rejected/timeout，立即向用户报告；保留计算进程；不得宣称监控接管成功。

### 7.3 文件投递示例

Linux B：

```bash
scp task.json B:~/.local/share/atw/tasks/inbox/<request_id>.json.tmp
ssh B 'mv ~/.local/share/atw/tasks/inbox/<request_id>.json.tmp ~/.local/share/atw/tasks/inbox/<request_id>.json'
ssh B 'atw receipt <request_id> --wait 30 --json'
```

上面 `<request_id>` 为示意占位符，真实实现要进行正确的 SSH/路径转义；Windows B 使用 PowerShell 的对应操作。独立路径在注册和运行之间的一致性由 Skill 负责验证。

## 8. CLI 契约

```text
atw status [--json]
atw list [--state MONITORING] [--json]
atw show <task-id> [--json]
atw history [--limit N] [--json]
atw validate <file.json> [--json]
atw receipt <request-id> [--wait SECONDS] [--json]
atw doctor [--json]
atw notify test
atw notifications [--json]
atw service run                    # 前台运行，供 systemd / Scheduler 调用
```

`--json` 输出必须是有效 JSON，不混入 ANSI 控制符或普通日志。CLI 查询只读，不提供 `kill/stop/retry/restart` 计算任务功能。`notify test` 仅测试发消息，不触及计算进程。

## 9. 配置与安全

全局 `config.yaml` 包含：`instance_id`, `paths`, `scan_interval_seconds`, `sample_interval_seconds`, `timezone`, `notification`, `allowed_read_roots`, `health_summary`, `retention_days`。敏感参数读取环境变量，例如 `ATW_DINGTALK_WEBHOOK` 与 `ATW_DINGTALK_SECRET`（从系统机密存储引入为未来增强）；不将密钥写入任务 JSON 或 Git。

安全基线：
- 默认仅同一系统用户监控；不可访问的进程/文件明确报错。
- 禁止任意 Shell/代码执行；拒绝不支持规则类型、深度过大规则、超大 JSON、非法文件路径。
- 符号链接、路径穿越和扩展根目录访问需要校验；日志预览截断与脱敏。
- 数据库、回执与日志目录权限限定本用户；Windows 使用用户 ACL；Linux 建议 0700 目录与 0600 文件。
- `atw doctor` 在脱敏前提下显示系统能力、依赖与配置诊断结果。
- 包装独立程序时确保 `psutil`/NVML 动态依赖可用或允许降级。

## 10. 服务与打包

Linux：提供 systemd service 文件安装脚本，明确 `User=`、`WorkingDirectory=`、`ExecStart=... atw service run`、`Restart=always`、重启退避与日志策略；若使用 user service，须说明启用 linger 才能在未登录的情况下开机运行。不得默认安装成 root 服务。

Windows：提供任务计划程序安装/卸载 PowerShell 脚本，以“计算机启动”为触发器，使用指定同一用户账户，配置无人登录运行、失败重试、正确解释器绝对路径、工作目录与权限。验收必须实测重启后的无人值守状态，不能仅验证登录自启。

源码版：`pip install -e .` 或 `uv sync`；支持 `atw doctor`/`atw service run`。

发行版：Windows exe 和 Linux 独立二进制；通过 CI 建构，不在单一系统交叉生成不兼容文件；含配置示例、服务安装/卸载说明和版本升级文档。

## 11. 推荐项目结构

```text
agent-task-watcher/
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── docs/
│   ├── protocol.md
│   ├── architecture.md
│   ├── deployment-linux.md
│   ├── deployment-windows.md
│   ├── security.md
│   └── acceptance.md
├── src/atw/
│   ├── main.py
│   ├── config.py
│   ├── models/
│   ├── intake/
│   ├── monitoring/
│   ├── rules/
│   ├── tasks/
│   ├── notifications/
│   ├── storage/
│   ├── health/
│   └── cli/
├── schemas/task-v1.schema.json
├── examples/
│   ├── task-pid.json
│   ├── task-match.json
│   └── task-update.json
├── skills/atw-task-handoff/SKILL.md
├── deploy/linux/
├── deploy/windows/
├── tests/unit/
├── tests/integration/
└── tests/e2e/
```

## 12. 详细开发阶段与可执行任务

### P0 — 工程与协议骨架

- 建立 package/CLI、目录与配置加载；严格 Pydantic 注册/更新/回执模型与 Schema。
- JSON 文件创建、原子发布、inbox 扫描、accepted/rejected 分流。
- request_id 幂等、task_id 唯一、revision 原子更新与过期校验。
- SQLite 初始化、迁移策略、service single-instance lock。
- `atw validate`, `atw doctor`, `atw receipt` 可用。

**阶段验收**：无计算进程时，非法注册可得到精确拒绝回执；重复请求不产生重复任务；Schema 与所有示例一致。

### P1 — 进程监督闭环

- `psutil` 进程身份匹配、进程树发现、CPU/内存采样、终态检测。
- 日志增量/历史本次运行读取；结果文件新鲜性和任务归属验证。
- 规则引擎、三态运算、进度/警告/终态事件。
- 基于 mock HTTP 的钉钉中文 Markdown 模板、签名及发送队列。
- 使用 3 种小型本地任务验证 SUCCESS/FAILED/UNKNOWN。

**阶段验收**：真实独立后台进程在 AI/CLI 退出后仍运行，ATW 监测到正确终态；无可靠成功证据的消失进程为 UNKNOWN。

### P2 — 热更新、可靠恢复和通知

- 更新接口和规则版本；SQLite 事务状态机与 outbox；日常 CLI 查询。
- 服务重启恢复；整机启动标识；终态不重开；通知去重与限流。
- CPU 低负载持续告警、超时、周期汇报与日志轮转测试。
- `SERVICE_RECOVERED` 与可配置 `DAILY_HEALTH`。

**阶段验收**：至少同时监测 5 个任务；ATW 强制退出再重启后恢复正确；终态不重发首次接入；重启前未发消息能恢复尝试投递。

### P3 — NVIDIA GPU 与 Skill

- NVML 设备发现、进程级 GPU 监测、UUID 标识与指标 unavailable 降级。
- `atw-task-handoff/SKILL.md`：每次询问可选日志和结果路径，缺省由 AI 确定。
- 自动生成任务 JSON，SSH 原子投递和查询回执；目标运行过程与 SSH 断开无关。
- AI/Agent 完成转交后立即结束，不建立长轮询 AI goal。

**阶段验收**：有 GPU 的 Linux 上真实 GPU 任务可被识别；无 GPU 的 Windows/Linux 正常监测 CPU 任务；Skill 的路径选择和回执严格验收通过。

### P4 — 跨平台发行、文档和 GitHub 开源

- Linux systemd + Windows Task Scheduler 安装/卸载脚本，使用同一用户权限。
- Windows/Linux 自动测试与打包；Windows 无登录运行测试；Linux 开机自动恢复测试。
- README、架构、协议、使用示例、FAQ、故障排查、隐私安全与限制文档。
- PyInstaller 打包，GitHub Actions 构建；发布 GitHub Release。
- 开源许可证由项目维护者明确选择（默认提案 MIT，发布前确认）；不得自动提交包含密钥的文件。

**阶段验收**：在 Windows 与 Linux 分别实现源码部署、自启动和完整任务接入通知；发行包能独立运行，文档与 CLI 一致。

## 13. 验收矩阵（必须形成可复现报告）

| ID | 分类 | 场景 | 必须结果 |
|---|---|---|---|
| AT-001 | 入口 | 原子投递完整 JSON | 5 秒兜底扫描 + 处理时间内发现，请求回执成功 |
| AT-002 | 入口 | 半写入 `.tmp` | 不被识别，不登记 |
| AT-003 | 入口 | 错误 JSON/schema | rejected 且有错误码，不崩溃 |
| AT-004 | 幂等 | 同 request_id 重复投递 | 单次任务登记、原回执可复用 |
| AT-005 | 版本 | 同 task_id 合法 revision+1 | 只更新允许字段，历史版本可查 |
| AT-006 | 版本 | revision 旧/冲突 | 拒绝，旧配置保留 |
| AT-007 | 过期 | 过期尚未接入请求 | REQUEST_EXPIRED，已接入任务不受影响 |
| AT-008 | 进程 | PID+create_time 完整匹配 | 成功绑定正确运行实例 |
| AT-009 | 进程 | PID 被系统复用 | 不错误绑定新进程 |
| AT-010 | 进程 | 名称+命令匹配多个进程 | AMBIGUOUS_PROCESS，不猜测 |
| AT-011 | 进程 | 父进程退出，已发现子进程仍运行 | 继续监控子进程 |
| AT-012 | 进程 | 命令行权限不足 | 拒绝/告警，不能静默绑定 |
| AT-013 | 规则 | 同时满足完成日志、结果文件、进程树退出 | SUCCESS，只发一次成功通知 |
| AT-014 | 规则 | 任务失败日志命中 | 按 finalize 模式发异常/FAILED |
| AT-015 | 规则 | 进程消失、无可信成功证据 | UNKNOWN，绝不标 SUCCESS |
| AT-016 | 规则 | GPU 指标 unavailable | UNKNOWN 条件，不视作利用率 0 |
| AT-017 | 规则 | CPU 低于阈值但未满持续时间 | 不触发告警 |
| AT-018 | 规则 | CPU 低于阈值满持续时间 | 触发一次告警，任务继续监控 |
| AT-019 | 文件 | 上次运行遗留结果文件 | 不触发本次运行成功 |
| AT-020 | 文件 | 本次任务已运行后热接入且已写日志 | 能解析有效的本次运行历史内容 |
| AT-021 | 文件 | 日志截断/轮转 | 读取位置恢复，避免重复误报 |
| AT-022 | 状态 | ATW 服务崩溃再启动，任务仍在 | 恢复监控，不重复首次通知 |
| AT-023 | 状态 | 主机重启，旧任务中断 | INTERRUPTED，旧任务不重新激活 |
| AT-024 | 状态 | 已终态任务在 inbox/accepted/archive 遗留 | 不再次进入 MONITORING |
| AT-025 | 通知 | 钉钉 HTTP 200 但 errcode 非 0 | 标记失败并按错误类型处理 |
| AT-026 | 通知 | 钉钉网络断连 | outbox 保留，网络恢复后重试 |
| AT-027 | 通知 | 多任务突发大量消息 | 每机器人保持 <=15 条/分钟配置限流 |
| AT-028 | 通知 | 同异常长时间重复 | 按频率去重/提醒，无消息风暴 |
| AT-029 | Skill | 用户提供日志和结果路径 | 使用用户路径并写入 JSON |
| AT-030 | Skill | 用户只提供一个路径 | 未提供项由 AI 生成，写入 JSON |
| AT-031 | Skill | 用户都选择默认 | 自动生成独立路径，完成真实交接 |
| AT-032 | Skill | 回执 rejected 或等待超时 | 不声明接管成功，不杀任务 |
| AT-033 | Skill | SSH 断开 | 计算任务仍运行，ATW 继续监督 |
| AT-034 | 平台 | Linux systemd 启动/失败重启 | 无登录条件下稳定运行并恢复监控 |
| AT-035 | 平台 | Windows Task Scheduler 开机启动 | 无登录条件下正常监控/通知 |
| AT-036 | 健康 | 服务异常恢复 | SERVICE_RECOVERED 单次通知 |
| AT-037 | 健康 | 每日摘要启用 | 本地时区按配置发 1 次，重启不重复 |
| AT-038 | 安全 | JSON 试图注入 Shell/越界路径 | 拒绝，不执行代码、不泄漏密钥 |
| AT-039 | 降级 | 无 NVIDIA GPU / NVML 不可用 | CPU 任务监控无障碍，诊断明确 |
| AT-040 | 并行 | 至少 5 个独立任务并行 | 状态、规则、通知彼此隔离 |

**时延验收建议**：默认配置下，稳定正常负载中 inbox 识别到生成回执不超过 30 秒；进程结束到数据库生成终态事件不超过 30 秒（不包含外部钉钉网络延迟）。通过 5 次重复测试记录最慢耗时。网络通知延迟单独度量。

**性能与规模验证**：在 Linux 和 Windows 各自用 5 个真实 CPU 测试进程和 20 个模拟/轻量进程负载验证，不允许 ATW CPU 忙等。资源预算与阈值由 P2 基准测试固定并写入验收报告，不提前给无法验证的硬性数字。

**测试实现**：pytest 单元测试覆盖三态规则、模型、幂等、时间/版本冲突、签名/限流；集成测试覆盖 SQLite、文件目录、进程生命周期和 httpx mock；Windows/Linux 各做服务与真实进程 E2E。测试证据以 `docs/acceptance.md` 呈现，包含命令、环境、通过/失败及截图/日志摘要（脱敏）。

## 14. Codex 执行约束及阶段交付形式

1. 先检查仓库现状，如为空按冻结结构创建；如已有代码，优先保留适合的部分，避免大幅改名。
2. 先实现 P0 再做 P1...P4；每阶段输出新增文件、功能总结、验证结果与未解决问题。
3. 每阶段仅运行与修改范围对应的最小充分测试；不扩建额外测试平台，也不反复运行昂贵仿真。
4. 接口一旦实现须同步导出 JSON Schema、更新示例和文档；不得出现字段名漂移。
5. 不将真实钉钉 Webhook/加签密钥提交到仓库；真实发送只在用户提供安全配置时做一次性测试。
6. 如实际硬件/操作系统不可用，使用 mock + 将对应 E2E 项目标注 `NOT_VERIFIED`，严禁声称已经验证。
7. 技术实现发现与本规范冲突时，先记录偏差与解决建议，不自行扩大 V1 需求。
8. 对可能损坏用户任务的操作（杀进程、覆盖数据、修改运行命令）必须避免；ATW 本身对计算进程只读。
9. 最终交付源代码、示例 JSON、Skill、Windows/Linux 部署脚本、验收报告与 Release 打包流程。

## 15. 唯一待项目维护者录入的配置值（不影响开发）

- 钉钉 Webhook 地址与加签密钥（运行环境注入；开发时 mock）。
- B 端具体目标 OS 版本和实际安装路径（通过系统自动识别或安装向导决定）。
- 每次运行的日志与结果路径（由 Skill 调用时询问，未指定由 AI 决定）。
- 项目开源仓库名称/账号与最终许可证（发布阶段确定）。

## 16. 核心验收结论

V1 可宣布完成，必须同时满足：

- 两个平台上成功通过目录热接入；AI 可获得可验证的 accepted/rejected 回执。
- 任意时刻均不通过 ATW 控制计算进程；真实长任务在 AI 退出后仍可持续监控。
- 进程身份验证、日志/结果证据、规则三态、不误判 SUCCESS、并行隔离通过。
- ATW 服务重启与主机重启行为正确；终态任务不重复监控；通知具备限流/去重/重试。
- 同用户 Linux/Windows 自启动稳定；无 GPU 可正常退化；有 NVIDIA GPU 能在支持指标范围内采样。
- Codex Skill 每次询问可选日志/结果路径，并在未指定时正确生成；交接失败不会虚报成功。
- AT-001 至 AT-040 均有 PASS 或明确经维护者批准的环境限制说明；核心项 AT-004、AT-008、AT-013、AT-015、AT-022、AT-023、AT-032、AT-033、AT-034、AT-035 不能豁免。

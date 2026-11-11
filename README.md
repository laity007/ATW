# Agent Task Watcher (ATW) V1.0

ATW 是一个**零大模型调用**的本地计算任务监督程序：AI Agent 通过 SSH 以 JSON
文件把长期运行的计算任务交接给服务器上的 ATW，AI 会话退出后，ATW 持续监督
进程、日志、结果文件和 GPU 指标，并通过钉钉群机器人单向通知最终状态。

> ATW 只**监督与通知**。它不会启动、停止、重启或重试任何计算进程。

## 能力边界（V1）

- 单机常驻，Windows 10/11 与主流 Linux（优先 Ubuntu/Debian）。
- 只监控已存在的任务：`PID + 进程创建时间` 强身份，或进程名 + 命令行唯一匹配。
- 追踪根进程和观察到的子进程树；父进程退出后继续追踪已发现的子进程。
- 本次运行的日志/结果证据、CPU/RSS、NVIDIA NVML 进程级显存；不可用指标为
  `UNKNOWN`，绝不当作 0。
- 三态规则引擎 `TRUE/FALSE/UNKNOWN`，支持 AND/OR/NOT、持续时间、警告、
  进度和终态。
- SQLite（WAL）持久化；服务重启恢复、主机重启判定 `INTERRUPTED`、终态不可重开。
- 钉钉自定义机器人 Markdown 通知：HMAC-SHA256 加签、15 条/分钟限流、
  去重、持久化 outbox 与指数退避重试。
- **不提供** Web UI、HTTP API、钉钉聊天命令、计算进程控制、AMD/Intel GPU 遥测。

## 验收状态（2026-11-10）

- Windows：`116 passed, 1 skipped`（WDDM 下 NVML 不提供进程级显存，按 UNKNOWN
  处理，不是 0）。
- Ubuntu 22.04：`117 passed`（含真实 RTX 3090 NVML 进程显存测试）。
- 真实 Linux 交接：显式双路径、仅日志路径、双默认路径、rejected 回执、
  SSH 断开后继续监控、systemd `kill -9` 后自动恢复，均已实测。
- 真实钉钉测试消息发送成功（`ok=true, channel=dingtalk`）。
- 因环境权限/共享主机限制，**Windows Task Scheduler 的 AtStartup 无登录启动
  与物理重启**未实测（`NOT_VERIFIED`）；Linux 实际重启未执行，但 systemd
  单元、linger、崩溃重启与 SSH 退出独立性已实测。详见
  [`docs/acceptance.md`](docs/acceptance.md)。

## 快速开始

### 1. 安装（源码版）

```bash
git clone https://github.com/laity007/ATW.git agent-task-watcher
cd agent-task-watcher
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .
atw doctor
```

Python 要求 **>= 3.11**。

### 2. 初始化配置

```bash
atw doctor --json
```

默认 `ATW_HOME`：

- Linux: `~/.local/share/atw`
- Windows: `%LOCALAPPDATA%\ATW`

把 `config.example.yaml` 复制到 `<ATW_HOME>/config/config.yaml`，按需修改
`allowed_read_roots`、`instance_id`、`timezone` 等。**不要把密钥写入该文件。**

钉钉密钥只通过环境变量注入：

```bash
export ATW_DINGTALK_WEBHOOK='https://oapi.dingtalk.com/robot/send?access_token=...'
export ATW_DINGTALK_SECRET='SEC...'
atw notify test
```

Windows PowerShell:

```powershell
$env:ATW_DINGTALK_WEBHOOK = 'https://oapi.dingtalk.com/robot/send?access_token=...'
$env:ATW_DINGTALK_SECRET = 'SEC...'
atw notify test
```

未配置密钥时 `notification.provider: auto` 自动使用 mock（消息写入
`logs/mock_notifications.jsonl`），便于开发和验收。

### 3. 启动服务（前台，供 systemd / Scheduler 调用）

```bash
atw service run
```

### 4. AI Agent 交接

阅读 [`skills/atw-task-handoff/SKILL.md`](skills/atw-task-handoff/SKILL.md)。
Skill 每次调用都会询问日志路径与结果输出路径（均可留空/默认），生成符合
Schema 的 JSON，通过 SSH 原子投递到 `tasks/inbox/`，并校验
`atw receipt <request_id> --wait 30 --json` 的 `accepted` 回执与
PID + 创建时间。失败时不会宣称接管成功。

## CLI

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
atw service run
```

所有 `--json` 输出为纯 JSON（不含 ANSI/日志）。CLI 查询只读，不提供
`kill/stop/retry/restart` 计算任务的能力。

## 协议与目录

```text
ATW_HOME/
├── config/config.yaml
├── tasks/inbox/           # AI 原子投递入口
├── tasks/accepted/
├── tasks/rejected/
├── tasks/archive/         # 终态任务快照
├── receipts/              # <request_id>.json
├── data/atw.db            # 唯一状态权威源
└── logs/atw.log
```

完整协议见 [`docs/protocol.md`](docs/protocol.md)，架构见
[`docs/architecture.md`](docs/architecture.md)，JSON Schema 见
[`schemas/task-v1.schema.json`](schemas/task-v1.schema.json)。

## 文档

- [实施计划](docs/implementation-plan.md)
- [协议 AFHP/1](docs/protocol.md)
- [架构](docs/architecture.md)
- [Linux 部署](docs/deployment-linux.md)
- [Windows 部署](docs/deployment-windows.md)
- [安全与隐私](docs/security.md)
- [验收记录](docs/acceptance.md)
- [发行构建](docs/release.md)
- [FAQ / 故障排查](docs/faq.md)

## 安全

- `key.txt` / `webhook.txt` / `.env` 已在 `.gitignore` 中，禁止提交。
- 日志、回执和通知正文不包含 webhook、token、secret、sign。
- 证据路径必须位于 `allowed_read_roots`，符号链接越界将被拒绝。
- ATW 对计算进程只读；不会执行 JSON 中的任何 shell/代码。

详见 [`docs/security.md`](docs/security.md)。

## 许可证

MIT，见 [LICENSE](LICENSE)。

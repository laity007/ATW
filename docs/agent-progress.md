# Agent 交接和进度

在计算节点上使用服务账户及相同的 `--home` 和 `--config`。先启动独立的计算进程，再执行交接。用 `atw status --json` 检查运行中服务的 `active_protocols`。

## 单项训练

以下命令假定本次任务已有真实日志、对应的结果路径及成功标记。参数在 Windows 和 Linux 相同，示例续行使用 Bash 语法。Windows 可用单行命令。

```bash
atw handoff --task-id yolo-s07 --request-id req-yolo-s07-01 --name 'UAV-YOLO S07' \
  --pid 12345 --run-dir /runs/s07 --log /runs/s07/train.log \
  --result /runs/s07/best.pt --success-log '<应用的实际成功标记>' \
  --progress-csv /runs/s07/results.csv --progress-column epoch --counter-base 1 \
  --total 200 --unit epoch --request-out /runs/s07/atw-request.json --wait 30 --json
```

- 先加 `--dry-run` 可查看实际请求、进度样本和启动时间来源。专用进程默认使用创建时间，长驻调度器场景需显式给出 `--run-started-at`。通过进程名和命令行定位时，CLI 先唯一匹配，再将结果固定为 PID 和创建时间。
- CSV 读取最后一条换行结尾的完整数据行，列名去除首尾空白。示例 [CSV](../examples/progress-epochs.csv) 的完成进度为 31%。将样例内容换成本次运行产生的 CSV。零基计数使用 `--counter-base 0`。
- 成功需要真实标记、对应结果文件和进程树退出。复杂的规则或已有 AFHP 文件使用 `atw submit FILE --wait 30 --json`。
- 返回码 0 表示已受理，1 表示拒绝或核验失败，2 表示等待超时或服务配置加载失败。网络中断时执行 `atw receipt req-yolo-s07-01 --wait 30 --json`。需要重投时使用 `atw submit` 提交 `--request-out` 保存的原请求。更改后的请求用新请求 ID。
- 无可靠进度来源时指定 `--progress-unknown '原因'`，仍可按 AFHP/1 监控。旧节点可继续使用 Skill 中的 AFHP/1 原子交接方式。

## 串行或并行队列

将 [队列清单](../examples/progress-plan.json) 放在本次运行目录，保持清单的 `task_id` 与交接一致。每项配置 `id`、`total`、`unit`、`weight`、`required`、独立的 `report` 文件，以及可选的 `csv` 对象。状态文件路径相对于运行目录。

```bash
atw progress report --plan /runs/batch/progress-plan.json --item S07 --attempt 1 \
  --state running --completed 62
atw progress report --plan /runs/batch/progress-plan.json --item S08 --attempt 1 \
  --state queued
```

使用运行目录 `/runs/batch` 和任务 ID `yolo-screening`，将交接命令中的单项 CSV 选项换成 `--progress-plan /runs/batch/progress-plan.json`，绑定队列主进程。若清单放在子目录，给 `progress report` 同时传入 `--run-dir`。项目验收后提交 `--state completed --acceptance accepted`。一项配置 CSV 时，运行状态可以省略 `--completed`。重试 CSV 使用独立文件，例如 `"csv":{"path":"S07/run-{attempt}/results.csv","column":"epoch","counter_base":1}`。

执行进度为各项完成比例按权重取平均。队列重试沿用子任务 ID 并递增 attempt。可选项允许报告 `skipped`，仍计入原计划分母。验收以项目报告的状态为准。缺计数时显示未知或最近有效快照。签收后用 `atw show TASK --json` 查看每项来源诊断。任务运行和通知频率由已有 Supervisor 配置控制。

## 协议与升级

- `atw.task.v1.1` 在注册请求中允许顶层 `progress`。配置类型为 `csv` 或 `queue`。原始 AFHP JSON 中的路径使用节点绝对路径，CLI 可把清单中的相对路径转换为绝对路径。源文件必须位于本次运行目录和读路径白名单内。新版沿用 `atw.receipt.v1`。
- 更新使用原 revision 检查。`changes.progress` 缺省时沿用，显式 `null` 移除，对象替换原配置。可用 `atw submit` 提交更新 JSON。
- Linux 和 Windows 升级沿用各自部署方式。先在独立 ATW_HOME 验收，备份原数据库并保留已知可用的安装包。验证旧任务、原回执和待发通知再启用新版交接。回退前停止新版投递并核对尚未消费的新版请求，原 AFHP/1 模式可继续使用。 [Linux wheel 升级](deployment-linux.md#5-verified-wheel-upgrade-and-rollback)

## 1.1.0 验收记录

| 环境 | 结果 |
|---|---|
| Windows | `python -m pytest -q` 通过 128 项，跳过 POSIX 信号和当前驱动的 NVML 进程显存两项。以 PyInstaller 构建的单文件 CLI 可执行 `--version` 和 `handoff --help`。 |
| C2 Ubuntu 临时 ATW_HOME | `python -m pytest -q` 通过 130 项。通过 `python -m atw.schema_export --check`。 |
| 旧版回退 | 两端均用基线 `89802e5` 源码切换测试服务。旧版成功打开新版数据库、显示任务并接收 AFHP/1 更新。再次运行 1.1.0 后，CSV 进度恢复并自动更新。 |
| 进度推送 | 两端均使用真实本机 Supervisor 和 mock 通知通道。验证了 CSV 62/200 到 63/200、加权队列、100% 时继续独立验收及移除进度配置。 |
| 构建 | 两端的 PyInstaller 单文件程序通过启动检查。纯 Python wheel 在 Ubuntu 的隔离目录安装并运行 `atw --version` 成功。 |

复跑两项端到端验收可执行 `python -m pytest -q tests/e2e/test_service_e2e.py -k 'handoff_csv or queue_handoff'`。将 `ATW_TEST_LEGACY_SRC` 指向基线提交中的 `src` 绝对路径即可同时演练回退。现场上线仍需在预定的钉钉测试群核对手机端样式和投递。

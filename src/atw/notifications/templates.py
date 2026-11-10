"""Chinese Markdown message templates for DingTalk."""

from __future__ import annotations

from typing import Any

from atw.notifications.base import NotificationMessage
from atw.storage.records import TaskRecord
from atw.utils.timeutil import duration_text

EVENT_TITLES = {
    "MONITOR_ACCEPTED": "已接管任务监督",
    "PROGRESS": "任务进度",
    "WARNING": "任务告警",
    "SUCCESS": "任务成功完成",
    "FAILED": "任务失败",
    "UNKNOWN": "任务结束，状态未知",
    "INTERRUPTED": "任务已中断",
    "SERVICE_RECOVERED": "服务已恢复监控",
    "DAILY_HEALTH": "每日健康摘要",
    "NOTIFY_TEST": "通知通道测试",
}


def _title(event_type: str, title: str | None = None) -> str:
    return f"[ATW] {title or EVENT_TITLES.get(event_type, event_type)}"


def _clip(value: str, limit: int = 300) -> str:
    text = value.strip().replace("\r", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _path_list(paths: list[str]) -> str:
    if not paths:
        return "未指定"
    return "\n".join(f"  - `{path}`" for path in paths)


def _gpu_summary(gpu: dict[str, Any] | None) -> str:
    if not gpu:
        return "未采样"
    if not gpu.get("available"):
        return f"不可用（{gpu.get('reason') or '无 NVIDIA GPU/NVML'}）"
    memory = gpu.get("memory_mb")
    mem_text = f"{memory} MiB" if memory is not None else "unknown"
    util = gpu.get("utilization_percent")
    if util is None:
        util_text = "进程级利用率不可用（UNKNOWN，不视为 0）"
    else:
        util_text = f"{util}%"
    devices = gpu.get("devices") or []
    device_text = "、".join(
        f"{d.get('name', 'GPU')}（显存 {d.get('memory_used_mb')}/{d.get('memory_total_mb')} MiB）"
        for d in devices[:2]
    )
    return f"任务显存 {mem_text}，任务利用率 {util_text}" + (
        f"；设备背景：{device_text}" if device_text else ""
    )


def _base_lines(record: TaskRecord, node_label: str) -> list[str]:
    lines = [
        f"- **任务名称：** {record.name}",
        f"- **计算节点：** {node_label}",
        f"- **任务编号：** `{record.task_id}`",
        f"- **监控版本：** revision {record.revision}",
    ]
    if record.project:
        lines.append(f"- **项目：** {record.project}")
    return lines


def _evidence_line(record: TaskRecord) -> list[str]:
    lines: list[str] = []
    if record.evidence:
        lines.append(f"- **运行目录：** `{record.evidence.run_directory}`")
        if record.evidence.log:
            lines.append(f"- **日志路径：** `{record.evidence.log.path}`")
        if record.evidence.result_files:
            lines.append("- **结果路径：**\n" + _path_list(record.evidence.result_files))
    return lines


def render_event(
    event_type: str,
    record: TaskRecord,
    payload: dict[str, Any],
    *,
    node_label: str,
) -> NotificationMessage:
    title = _title(event_type, payload.get("title"))
    lines: list[str] = [f"### {title}", ""]
    lines.extend(_base_lines(record, node_label))

    if event_type == "MONITOR_ACCEPTED":
        bound = payload.get("bound_process") or {}
        lines.extend(
            [
                "- **监控状态：** MONITORING",
                f"- **绑定进程：** PID `{bound.get('pid')}`，创建时间 "
                f"`{bound.get('create_time_epoch')}`",
                f"- **日志路径：** `{(record.evidence.log.path if record.evidence and record.evidence.log else '未指定')}`",
            ]
        )
        if record.evidence and record.evidence.result_files:
            lines.append("- **结果路径：**\n" + _path_list(record.evidence.result_files))
        lines.extend(
            [
                "",
                "ATW 仅监督与通知，不会启动、停止或重试该计算进程。",
            ]
        )
    elif event_type in {"SUCCESS", "FAILED", "UNKNOWN", "INTERRUPTED"}:
        state = payload.get("state") or event_type
        duration = payload.get("duration_seconds")
        lines.extend(
            [
                f"- **最终状态：** {state}",
                f"- **运行时长：** {duration_text(duration) if duration is not None else '未知'}",
                "- **进程退出码：** 未知（ATW 非父进程，外部进程退出码不可得）",
                f"- **判断证据：** {_clip(str(payload.get('evidence_summary') or '无'))}",
            ]
        )
        if payload.get("reason"):
            lines.append(f"- **说明：** {_clip(str(payload.get('reason')))}")
        if payload.get("cpu_percent") is not None:
            lines.append(f"- **CPU（树聚合）：** {payload.get('cpu_percent')}%")
        lines.append(f"- **GPU：** {_gpu_summary(payload.get('gpu'))}")
        lines.extend(_evidence_line(record))
        if event_type == "SUCCESS":
            lines.append("")
            lines.append("备注：本消息由 ATW 自动发送，运行进程由 AI/用户管理。")
    elif event_type == "WARNING":
        lines.extend(
            [
                f"- **告警编号：** `{payload.get('warning_id', 'warning')}`",
                f"- **告警内容：** {_clip(str(payload.get('message') or payload.get('detail') or ''))}",
                f"- **已运行：** {duration_text(payload.get('elapsed_seconds') or 0)}",
                "- **任务状态：** 继续监控（WARNING 是事件，不替代主状态）",
            ]
        )
    elif event_type == "PROGRESS":
        lines.extend(
            [
                "- **当前状态：** MONITORING",
                f"- **已运行：** {duration_text(payload.get('elapsed_seconds') or 0)}",
                f"- **CPU（树聚合）：** {payload.get('cpu_percent') if payload.get('cpu_percent') is not None else 'UNKNOWN'}%",
                f"- **GPU：** {_gpu_summary(payload.get('gpu'))}",
            ]
        )
    else:
        for key, value in payload.items():
            if key in {"title", "gpu"}:
                continue
            lines.append(f"- **{key}：** {_clip(str(value))}")

    return NotificationMessage(title=title, text="\n".join(lines))


def render_service_recovered(
    *, node_label: str, task_count: int, task_ids: list[str], recovered_at: str
) -> NotificationMessage:
    title = _title("SERVICE_RECOVERED")
    lines = [
        f"### {title}",
        "",
        f"- **计算节点：** {node_label}",
        f"- **恢复时间：** {recovered_at}",
        f"- **恢复监控任务数：** {task_count}",
    ]
    if task_ids:
        lines.append("- **任务编号：** " + "、".join(f"`{tid}`" for tid in task_ids[:10]))
    lines.extend(
        [
            "",
            "ATW 服务在执行期间重启，已重新校验进程身份并恢复监控；不会重复发送首次接入通知。",
        ]
    )
    return NotificationMessage(title=title, text="\n".join(lines))


def render_daily_health(
    *,
    node_label: str,
    date_text: str,
    monitoring: int,
    stats: dict[str, int],
    outbox: dict[str, int],
    gpu: dict[str, Any],
    uptime_seconds: float | None,
) -> NotificationMessage:
    title = _title("DAILY_HEALTH")
    gpu_text = "可用" if gpu.get("available") else f"不可用（{gpu.get('reason')}）"
    lines = [
        f"### {title}",
        "",
        f"- **计算节点：** {node_label}",
        f"- **统计日期：** {date_text}",
        f"- **服务运行时长：** {duration_text(uptime_seconds or 0)}",
        f"- **当前监控任务：** {monitoring}",
        f"- **过去 24 小时：** SUCCESS {stats.get('SUCCESS', 0)} / "
        f"FAILED {stats.get('FAILED', 0)} / UNKNOWN {stats.get('UNKNOWN', 0)} / "
        f"INTERRUPTED {stats.get('INTERRUPTED', 0)}",
        f"- **通知队列：** pending {outbox.get('pending', 0)} / "
        f"delivered {outbox.get('delivered', 0)} / failed {outbox.get('failed', 0)}",
        f"- **GPU 采样能力：** {gpu_text}",
    ]
    return NotificationMessage(title=title, text="\n".join(lines))


def render_test_message(*, node_label: str, provider: str) -> NotificationMessage:
    title = _title("NOTIFY_TEST")
    text = "\n".join(
        [
            f"### {title}",
            "",
            f"- **计算节点：** {node_label}",
            f"- **通知通道：** {provider}",
            "- **结果：** 通道可用",
            "",
            "这是一条 ATW 通知通道测试消息，不涉及任何计算进程。",
        ]
    )
    return NotificationMessage(title=title, text=text)

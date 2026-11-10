"""NVIDIA NVML integration with explicit ``unavailable`` degradation.

Per-process NVML exposes used GPU memory but not per-process utilization.
Therefore ``gpu_util_below`` can only be evaluated when a real per-process
utilization source exists; device utilization is shown as background only and
never substituted for a task metric.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass(slots=True)
class GpuDevice:
    index: int
    uuid: str
    name: str
    memory_total_mb: float | None
    memory_used_mb: float | None
    utilization_percent: float | None


@dataclass(slots=True)
class GpuProcess:
    pid: int
    gpu_uuid: str
    memory_mb: float | None
    utilization_percent: float | None = None


@dataclass(slots=True)
class GpuSnapshot:
    available: bool
    reason: str | None = None
    devices: list[GpuDevice] = field(default_factory=list)
    processes: list[GpuProcess] = field(default_factory=list)
    per_process_utilization_available: bool = False
    process_metrics_available: bool = True
    errors: list[str] = field(default_factory=list)

    def task_metrics(self, pids: set[int]) -> dict[str, object]:
        matched = [p for p in self.processes if p.pid in pids]
        memory = [p.memory_mb for p in matched if p.memory_mb is not None]
        memory_complete = all(p.memory_mb is not None for p in matched)
        util_available = self.per_process_utilization_available and any(
            p.utilization_percent is not None for p in matched
        )
        util_values = [p.utilization_percent for p in matched if p.utilization_percent is not None]
        if not self.available:
            memory_mb = None
        elif not matched:
            # Only a real "no GPU process" is 0; an unavailable process query
            # (permission/unsupported) must stay UNKNOWN.
            memory_mb = 0.0 if self.process_metrics_available else None
        elif not memory_complete:
            memory_mb = None
        else:
            memory_mb = round(sum(memory), 1)
        return {
            "available": self.available,
            "reason": self.reason,
            "per_process_utilization_available": self.per_process_utilization_available,
            "process_metrics_available": self.process_metrics_available,
            "memory_mb": memory_mb,
            "utilization_percent": max(util_values) if util_available and util_values else None,
            "devices": [
                {
                    "uuid": d.uuid,
                    "index": d.index,
                    "name": d.name,
                    "memory_used_mb": d.memory_used_mb,
                    "memory_total_mb": d.memory_total_mb,
                    "utilization_percent": d.utilization_percent,
                }
                for d in self.devices
            ],
            "processes": [
                {
                    "pid": p.pid,
                    "gpu_uuid": p.gpu_uuid,
                    "memory_mb": p.memory_mb,
                }
                for p in matched
            ],
            "errors": self.errors,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "reason": self.reason,
            "devices": [
                {
                    "index": d.index,
                    "uuid": d.uuid,
                    "name": d.name,
                    "memory_total_mb": d.memory_total_mb,
                    "memory_used_mb": d.memory_used_mb,
                    "utilization_percent": d.utilization_percent,
                }
                for d in self.devices
            ],
            "processes": [
                {
                    "pid": p.pid,
                    "gpu_uuid": p.gpu_uuid,
                    "memory_mb": p.memory_mb,
                    "utilization_percent": p.utilization_percent,
                }
                for p in self.processes
            ],
            "per_process_utilization_available": self.per_process_utilization_available,
            "process_metrics_available": self.process_metrics_available,
            "errors": self.errors,
        }


class GpuMonitor:
    """Lazy NVML wrapper. Never raises into the supervisor loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._error: str | None = None
        self._nvml = None

    def _init(self) -> bool:
        if self._initialized:
            return self._error is None
        self._initialized = True
        try:
            import pynvml  # type: ignore

            try:
                pynvml.nvmlInit()
            except pynvml.NVMLError as exc:
                self._error = f"NVML 初始化失败: {self._nvml_error_name(exc)}"
                return False
            self._nvml = pynvml
            return True
        except ImportError:
            self._error = "未安装 nvidia-ml-py（NVML 不可用）"
            return False
        except Exception as exc:  # pragma: no cover - defensive
            self._error = f"NVML 初始化异常: {type(exc).__name__}: {exc}"
            return False

    @staticmethod
    def _nvml_error_name(exc: Exception) -> str:
        name = getattr(exc, "nvmlErrorString", None)
        if callable(name):
            try:
                return str(name())
            except Exception:
                pass
        return type(exc).__name__

    def capability(self) -> dict[str, object]:
        with self._lock:
            ok = self._init()
            if not ok:
                return {"available": False, "reason": self._error, "devices": []}
            devices = []
            try:
                for index in range(self._nvml.nvmlDeviceGetCount()):
                    handle = self._nvml.nvmlDeviceGetHandleByIndex(index)
                    uuid = self._nvml.nvmlDeviceGetUUID(handle)
                    name = self._nvml.nvmlDeviceGetName(handle)
                    devices.append(
                        {
                            "index": index,
                            "uuid": uuid.decode() if isinstance(uuid, bytes) else str(uuid),
                            "name": name.decode() if isinstance(name, bytes) else str(name),
                        }
                    )
                return {
                    "available": True,
                    "reason": None,
                    "devices": devices,
                    "per_process_utilization_available": False,
                    "note": "NVML 提供进程级显存，但不提供进程级利用率；gpu_util_below 将为 UNKNOWN",
                }
            except Exception as exc:
                self._error = f"NVML 设备枚举失败: {self._nvml_error_name(exc)}"
                return {"available": False, "reason": self._error, "devices": []}

    def sample(self, pids: set[int]) -> GpuSnapshot:
        with self._lock:
            if not self._init():
                return GpuSnapshot(available=False, reason=self._error)
            pynvml = self._nvml
            errors: list[str] = []
            devices: list[GpuDevice] = []
            processes: list[GpuProcess] = []
            process_query_ok = False
            try:
                count = pynvml.nvmlDeviceGetCount()
            except Exception as exc:
                self._error = f"NVML 设备计数失败: {self._nvml_error_name(exc)}"
                return GpuSnapshot(available=False, reason=self._error)
            for index in range(count):
                try:
                    handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                    uuid_raw = pynvml.nvmlDeviceGetUUID(handle)
                    uuid = uuid_raw.decode() if isinstance(uuid_raw, bytes) else str(uuid_raw)
                    name_raw = pynvml.nvmlDeviceGetName(handle)
                    name = name_raw.decode() if isinstance(name_raw, bytes) else str(name_raw)
                    total = self._device_memory(handle)
                    used = self._device_used_memory(handle)
                    util = self._device_utilization(handle)
                    devices.append(
                        GpuDevice(
                            index=index,
                            uuid=uuid,
                            name=name,
                            memory_total_mb=total,
                            memory_used_mb=used,
                            utilization_percent=util,
                        )
                    )
                    found, ok = self._running_processes(handle, uuid, errors)
                    process_query_ok = process_query_ok or ok
                    processes.extend(found)
                except Exception as exc:
                    errors.append(f"GPU {index} 采样失败: {self._nvml_error_name(exc)}")
            # De-duplicate by (pid, uuid), keeping the larger memory figure.
            merged: dict[tuple[int, str], GpuProcess] = {}
            for proc in processes:
                key = (proc.pid, proc.gpu_uuid)
                current = merged.get(key)
                if current is None or (proc.memory_mb or 0) > (current.memory_mb or 0):
                    merged[key] = proc
            return GpuSnapshot(
                available=True,
                devices=devices,
                processes=list(merged.values()),
                per_process_utilization_available=False,
                process_metrics_available=process_query_ok and not errors,
                errors=errors,
            )

    def _device_memory(self, handle) -> float | None:
        try:
            mem = self._nvml.nvmlDeviceGetMemoryInfo(handle)
            return round(mem.total / (1024 * 1024), 1)
        except Exception:
            return None

    def _device_used_memory(self, handle) -> float | None:
        try:
            mem = self._nvml.nvmlDeviceGetMemoryInfo(handle)
            return round(mem.used / (1024 * 1024), 1)
        except Exception:
            return None

    def _device_utilization(self, handle) -> float | None:
        try:
            rates = self._nvml.nvmlDeviceGetUtilizationRates(handle)
            return float(rates.gpu)
        except Exception:
            return None

    def _running_processes(
        self, handle, uuid: str, errors: list[str]
    ) -> tuple[list[GpuProcess], bool]:
        out: list[GpuProcess] = []
        query_ok = False
        for fn_name in (
            "nvmlDeviceGetComputeRunningProcesses_v3",
            "nvmlDeviceGetComputeRunningProcesses",
            "nvmlDeviceGetGraphicsRunningProcesses_v3",
            "nvmlDeviceGetGraphicsRunningProcesses",
        ):
            fn = getattr(self._nvml, fn_name, None)
            if fn is None:
                continue
            try:
                entries = fn(handle) or []
                query_ok = True
                for proc in entries:
                    pid = int(getattr(proc, "pid", -1))
                    if pid <= 0:
                        continue
                    used = getattr(proc, "usedGpuMemory", None)
                    memory_mb = None
                    if used is not None and used != getattr(
                        self._nvml, "NVML_VALUE_NOT_AVAILABLE", None
                    ):
                        if used < (1 << 60):
                            memory_mb = round(used / (1024 * 1024), 1)
                    out.append(GpuProcess(pid=pid, gpu_uuid=uuid, memory_mb=memory_mb))
            except Exception as exc:
                name = self._nvml_error_name(exc)
                if "NotSupported" not in name:
                    errors.append(f"{fn_name} 不可用: {name}")
        return out, query_ok

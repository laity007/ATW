from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from atw.monitoring.gpu import GpuMonitor
from conftest import wait_until

pytestmark = pytest.mark.gpu
PYTHON = getattr(sys, "_base_executable", sys.executable)

GPU_ALLOC_SCRIPT = r"""
import ctypes, json, os, pathlib, sys, time
run = pathlib.Path(sys.argv[1])
lib_name = "nvcuda.dll" if sys.platform == "win32" else "libcuda.so.1"
lib = ctypes.CDLL(lib_name)
c_int = ctypes.c_int
c_uint64 = ctypes.c_uint64
c_size_t = ctypes.c_size_t
lib.cuInit.argtypes = [ctypes.c_uint]
lib.cuInit.restype = c_int
rc = lib.cuInit(0)
if rc != 0:
    (run / "gpu_error").write_text(f"cuInit={rc}")
    raise SystemExit(0)
dev = c_int()
rc = lib.cuDeviceGet(ctypes.byref(dev), 0)
if rc != 0:
    (run / "gpu_error").write_text(f"cuDeviceGet={rc}")
    raise SystemExit(0)
ctx = ctypes.c_void_p()
ctx_create = getattr(lib, "cuCtxCreate_v2", None) or lib.cuCtxCreate
ctx_create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, c_int]
ctx_create.restype = c_int
rc = ctx_create(ctypes.byref(ctx), 0, dev)
if rc != 0:
    (run / "gpu_error").write_text(f"cuCtxCreate={rc}")
    raise SystemExit(0)
ptr = c_uint64()
alloc = getattr(lib, "cuMemAlloc_v2", None) or lib.cuMemAlloc
alloc.argtypes = [ctypes.POINTER(c_uint64), c_size_t]
alloc.restype = c_int
size = 256 * 1024 * 1024
rc = alloc(ctypes.byref(ptr), size)
if rc != 0:
    (run / "gpu_error").write_text(f"cuMemAlloc={rc}")
    raise SystemExit(0)
(run / "gpu_ready").write_text(json.dumps({"pid": os.getpid(), "bytes": size}))
while not (run / "gpu_go").exists():
    time.sleep(0.05)
free = getattr(lib, "cuMemFree_v2", None) or lib.cuMemFree
free.argtypes = [c_uint64]
free(ptr)
destroy = getattr(lib, "cuCtxDestroy_v2", None) or lib.cuCtxDestroy
destroy.argtypes = [ctypes.c_void_p]
destroy(ctx)
"""


@pytest.fixture(scope="module")
def capability():
    cap = GpuMonitor().capability()
    if not cap.get("available"):
        pytest.skip(f"NVML unavailable: {cap.get('reason')}")
    return cap


def test_nvml_capability_has_uuid(capability) -> None:
    devices = capability.get("devices") or []
    assert devices
    assert devices[0]["uuid"].startswith("GPU-")


def test_real_process_gpu_memory_and_rule(
    atw_home, repo, supervisor, tmp_path, register_task, capability
):
    run = tmp_path / "runs" / "gpu-task"
    run.mkdir(parents=True)
    script = tmp_path / "gpu_alloc.py"
    script.write_text(GPU_ALLOC_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [PYTHON, str(script), str(run)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: (run / "gpu_ready").exists() or proc.poll() is not None, timeout=30)
        if (run / "gpu_error").exists() or proc.poll() is not None:
            pytest.skip(
                f"CUDA allocation unavailable: {(run / 'gpu_error').read_text() if (run / 'gpu_error').exists() else proc.returncode}"
            )
        wait_until(lambda: (run / "gpu_ready").exists(), timeout=10)
        info = json.loads((run / "gpu_ready").read_text())
        assert info["pid"] == proc.pid
        time.sleep(0.5)
        monitor = GpuMonitor()
        snap = monitor.sample({proc.pid})
        assert snap.available
        matched = [p for p in snap.processes if p.pid == proc.pid]
        assert matched, [(p.pid, p.memory_mb) for p in snap.processes]
        print("GPU process sample:", [(p.pid, p.memory_mb) for p in matched])
        metrics = snap.task_metrics({proc.pid})
        # On Windows WDDM, NVML may expose the process but not its memory
        # (None).  That is a legitimate "unavailable", never zero.
        if matched[0].memory_mb is not None:
            assert matched[0].memory_mb >= 100
            assert metrics["memory_mb"] >= 100
        else:
            assert metrics["memory_mb"] is None
            pytest.skip("NVML 进程级显存在此平台不可用（WDDM），仅验证进程识别")

        _, receipt = register_task(
            proc,
            run,
            extra_rules={
                "warnings": [
                    {
                        "id": "gpu-mem",
                        "kind": "gpu_memory_above",
                        "mb": 1,
                        "duration_seconds": 0,
                    }
                ]
            },
        )
        assert receipt.status == "accepted"
        supervisor.gpu = monitor
        supervisor.tick()
        events = [r for r in repo.recent_events("task-1") if r["event_type"] == "WARNING"]
        assert events, "GPU memory warning not emitted"
    finally:
        (run / "gpu_go").write_text("1", encoding="utf-8")
        if proc.poll() is None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from atw.errors import (
    AMBIGUOUS_PROCESS,
    PROCESS_IDENTITY_MISMATCH,
    PROCESS_NOT_FOUND,
    AtwError,
)
from atw.models.task import Target
from atw.monitoring.process import ProcessIdentity, ProcessMonitor

PYTHON = getattr(sys, "_base_executable", sys.executable)


def test_validate_pid_identity_current_process() -> None:
    pid = os.getpid()
    create = psutil.Process(pid).create_time()
    target = Target(kind="pid", pid=pid, create_time_epoch=create)
    info = ProcessMonitor().validate_pid_target(target)
    assert info.identity.pid == pid
    assert abs(info.identity.create_time_epoch - create) < 1e-6


def test_pid_not_found() -> None:
    target = Target(kind="pid", pid=999_999_99, create_time_epoch=1.0)
    with pytest.raises(AtwError) as exc:
        ProcessMonitor().validate_pid_target(target)
    assert exc.value.code == PROCESS_NOT_FOUND


def test_pid_identity_mismatch() -> None:
    pid = os.getpid()
    create = psutil.Process(pid).create_time()
    target = Target(kind="pid", pid=pid, create_time_epoch=create + 100)
    with pytest.raises(AtwError) as exc:
        ProcessMonitor().validate_pid_target(target)
    assert exc.value.code == PROCESS_IDENTITY_MISMATCH


def _spawn_sleep(marker: str) -> subprocess.Popen:
    code = f"import time; time.sleep(30)  # {marker}"
    proc = subprocess.Popen(
        [PYTHON, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give psutil time to expose the cmdline.
    time.sleep(0.2)
    return proc


def test_match_unique() -> None:
    marker = "atw-unique-marker-12345"
    proc = _spawn_sleep(marker)
    try:
        name = psutil.Process(proc.pid).name()
        target = Target(
            kind="match",
            name=name,
            cmdline_contains=[marker],
            match_mode="unique",
        )
        info = ProcessMonitor().match_target(target)
        assert info.identity.pid == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_match_ambiguous() -> None:
    marker = "atw-ambiguous-marker-67890"
    procs = [_spawn_sleep(marker), _spawn_sleep(marker)]
    try:
        name = psutil.Process(procs[0].pid).name()
        target = Target(
            kind="match",
            name=name,
            cmdline_contains=[marker],
            match_mode="unique",
        )
        with pytest.raises(AtwError) as exc:
            ProcessMonitor().match_target(target)
        assert exc.value.code == AMBIGUOUS_PROCESS
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_pid_reuse_is_treated_as_ended(monkeypatch) -> None:
    monitor = ProcessMonitor(create_time_tolerance=0.05)

    class FakeProc:
        pid = 4242

        def status(self):
            return "running"

        def create_time(self):
            return 100.0

    monkeypatch.setattr(monitor, "_get_process", lambda ident: FakeProc())
    target = Target(kind="pid", pid=4242, create_time_epoch=200.0)
    snap = monitor.snapshot(target, [ProcessIdentity(4242, 200.0)])
    assert not snap.root_alive
    assert not snap.tree_alive
    assert 4242 in snap.pid_reused


def test_parent_exits_child_survives(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen(
        [PYTHON, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    child_pid = int(parent.stdout.readline().decode().strip())
    try:
        root = ProcessIdentity(parent.pid, psutil.Process(parent.pid).create_time())
        monitor = ProcessMonitor(create_time_tolerance=0.05)
        target = Target(kind="pid", pid=parent.pid, create_time_epoch=root.create_time_epoch)
        first = monitor.snapshot(target, [root])
        identities = [m.identity for m in first.members]
        assert any(ident.pid == child_pid for ident in identities), [
            m.identity.pid for m in first.members
        ]
        parent.kill()
        parent.wait(timeout=5)
        time.sleep(0.2)
        second = monitor.snapshot(target, [root] + identities)
        assert not second.root_alive
        assert second.tree_alive
        assert any(m.identity.pid == child_pid for m in second.members)
    finally:
        try:
            psutil.Process(child_pid).terminate()
        except psutil.Error:
            pass
        if parent.poll() is None:
            parent.kill()
        parent.stdout.close() if parent.stdout else None

from __future__ import annotations

import os

import psutil
import pytest

from atw.errors import ACCESS_DENIED, AtwError
from atw.models.task import Target
from atw.monitoring.process import ProcessMonitor


def test_pid_access_denied_propagates(monkeypatch) -> None:
    pid = os.getpid()
    create = psutil.Process(pid).create_time()
    monitor = ProcessMonitor()

    def denied(proc, role):
        raise AtwError(ACCESS_DENIED, "无法读取命令行（权限不足）")

    monkeypatch.setattr(monitor, "_info", denied)
    with pytest.raises(AtwError) as exc:
        monitor.validate_pid_target(Target(kind="pid", pid=pid, create_time_epoch=create))
    assert exc.value.code == ACCESS_DENIED

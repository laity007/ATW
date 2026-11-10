"""psutil process identity binding, tree discovery and CPU/RSS sampling.

Identity is always ``(pid, create_time)``.  A live PID whose create time differs
is treated as the original task having ended; ATW never re-binds to the new
process (PID reuse).
"""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass, field

import psutil

from atw.errors import (
    ACCESS_DENIED,
    AMBIGUOUS_PROCESS,
    PROCESS_IDENTITY_MISMATCH,
    PROCESS_NOT_FOUND,
    AtwError,
)
from atw.models.task import Target


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    create_time_epoch: float

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.create_time_epoch:.6f}"


@dataclass(slots=True)
class ProcInfo:
    identity: ProcessIdentity
    ppid: int | None
    name: str
    exe: str | None
    cmdline: str
    username: str | None
    role: str = "child"


@dataclass(slots=True)
class ProcessSnapshot:
    root_alive: bool
    tree_alive: bool
    tree_exited: bool
    tree_complete: bool
    cpu_percent: float | None
    cpu_available: bool
    memory_rss: int | None
    members: list[ProcInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pid_reused: list[int] = field(default_factory=list)
    unknown_children: int = 0

    def details(self) -> dict[str, object]:
        return {
            "root_alive": self.root_alive,
            "tree_alive": self.tree_alive,
            "tree_complete": self.tree_complete,
            "cpu_available": self.cpu_available,
            "cpu_percent": self.cpu_percent,
            "memory_rss": self.memory_rss,
            "members": [
                {
                    "pid": m.identity.pid,
                    "create_time_epoch": m.identity.create_time_epoch,
                    "role": m.role,
                    "name": m.name,
                }
                for m in self.members
            ],
            "errors": self.errors,
            "pid_reused": self.pid_reused,
            "unknown_children": self.unknown_children,
        }


def _same_user(candidate: str | None, current: str | None) -> bool:
    if candidate is None or current is None:
        return True
    if os.name == "nt":
        return candidate.casefold() == current.casefold()
    return candidate == current


class ProcessMonitor:
    def __init__(self, *, create_time_tolerance: float = 0.05):
        self.tolerance = max(0.0, create_time_tolerance)
        self._procs: dict[ProcessIdentity, psutil.Process] = {}
        self._primed: set[ProcessIdentity] = set()
        self._current_user: str | None = None
        try:
            self._current_user = psutil.Process(os.getpid()).username()
        except (psutil.Error, OSError):
            try:
                self._current_user = getpass.getuser()
            except Exception:  # pragma: no cover - exotic environments
                self._current_user = None

    # ------------------------------------------------------------------ helpers
    def _info(self, proc: psutil.Process, role: str) -> ProcInfo:
        ident = ProcessIdentity(proc.pid, float(proc.create_time()))
        try:
            ppid = proc.ppid()
        except (psutil.Error, OSError):
            ppid = None
        try:
            name = proc.name()
        except (psutil.Error, OSError):
            name = ""
        try:
            exe = proc.exe()
        except (psutil.Error, OSError):
            exe = None
        try:
            cmdline = " ".join(proc.cmdline())
        except psutil.AccessDenied as exc:
            raise AtwError(ACCESS_DENIED, f"无法读取 PID {proc.pid} 的命令行（权限不足）") from exc
        except (psutil.Error, OSError):
            cmdline = ""
        try:
            username = proc.username()
        except psutil.AccessDenied as exc:
            raise AtwError(ACCESS_DENIED, f"无法读取 PID {proc.pid} 的用户名（权限不足）") from exc
        except (psutil.Error, OSError):
            username = None
        return ProcInfo(ident, ppid, name, exe, cmdline, username, role)

    def _guard_user(self, info: ProcInfo, *, what: str) -> None:
        if not _same_user(info.username, self._current_user):
            raise AtwError(
                ACCESS_DENIED,
                f"{what} 属于其他系统用户 ({info.username})，ATW 仅监控同用户进程",
            )

    # --------------------------------------------------------------- validation
    def validate_pid_target(self, target: Target) -> ProcInfo:
        assert target.pid is not None and target.create_time_epoch is not None
        try:
            proc = psutil.Process(target.pid)
        except psutil.NoSuchProcess as exc:
            raise AtwError(PROCESS_NOT_FOUND, f"未找到 PID {target.pid}") from exc
        except psutil.AccessDenied as exc:
            raise AtwError(ACCESS_DENIED, f"PID {target.pid} 无权访问") from exc
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                raise AtwError(PROCESS_NOT_FOUND, f"PID {target.pid} 已是僵尸进程")
            actual = float(proc.create_time())
        except psutil.NoSuchProcess as exc:
            raise AtwError(PROCESS_NOT_FOUND, f"PID {target.pid} 已退出") from exc
        except psutil.AccessDenied as exc:
            raise AtwError(ACCESS_DENIED, f"PID {target.pid} 无权读取创建时间") from exc
        if abs(actual - target.create_time_epoch) > self.tolerance:
            raise AtwError(
                PROCESS_IDENTITY_MISMATCH,
                f"PID {target.pid} 创建时间不匹配：期望 {target.create_time_epoch:.6f}，"
                f"实际 {actual:.6f}（PID 可能已被复用）",
            )
        info = self._info(proc, "root")
        self._guard_user(info, what=f"PID {target.pid}")
        return info

    def match_target(self, target: Target) -> ProcInfo:
        assert target.name and target.cmdline_contains
        matches: list[ProcInfo] = []
        inaccessible_matches: list[int] = []
        for proc in psutil.process_iter(["pid", "name", "username", "cmdline"]):
            try:
                info = proc.info
                pid = int(info["pid"])
                if pid == os.getpid():
                    continue
                name = info.get("name") or ""
                name_ok = (
                    name.casefold() == target.name.casefold()
                    if os.name == "nt"
                    else name == target.name
                )
                if not name_ok:
                    continue
                username = info.get("username")
                if not _same_user(username, self._current_user):
                    continue
                cmdline = info.get("cmdline")
                if cmdline is None:
                    try:
                        cmdline = proc.cmdline()
                    except psutil.AccessDenied:
                        inaccessible_matches.append(pid)
                        continue
                    except (psutil.Error, OSError):
                        inaccessible_matches.append(pid)
                        continue
                joined = " ".join(cmdline)
                if not all(needle in joined for needle in target.cmdline_contains):
                    continue
                try:
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        continue
                except (psutil.Error, OSError):
                    pass
                matches.append(self._info(proc, "root"))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
                continue
        if inaccessible_matches:
            raise AtwError(
                ACCESS_DENIED,
                "存在名称/命令行疑似匹配但无权读取命令行的进程: "
                + ", ".join(str(pid) for pid in inaccessible_matches[:5]),
            )
        if not matches:
            raise AtwError(
                PROCESS_NOT_FOUND,
                f"未找到名称={target.name!r} 且命令行包含 {target.cmdline_contains!r} 的进程",
            )
        if len(matches) > 1:
            raise AtwError(
                AMBIGUOUS_PROCESS,
                "匹配到多个进程（"
                + ", ".join(str(m.identity.pid) for m in matches[:8])
                + "），拒绝猜测",
            )
        info = matches[0]
        self._guard_user(info, what=f"PID {info.identity.pid}")
        return info

    # ----------------------------------------------------------------- sampling
    def _get_process(self, ident: ProcessIdentity) -> psutil.Process:
        proc = self._procs.get(ident)
        if proc is None:
            proc = psutil.Process(ident.pid)
            self._procs[ident] = proc
        return proc

    def _check_identity(
        self, ident: ProcessIdentity
    ) -> tuple[bool, psutil.Process | None, str | None]:
        try:
            proc = self._get_process(ident)
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False, None, None
            actual = float(proc.create_time())
        except psutil.NoSuchProcess:
            return False, None, None
        except psutil.AccessDenied:
            return False, None, f"PID {ident.pid} 无权访问"
        except OSError as exc:
            return False, None, f"PID {ident.pid} 读取失败: {exc}"
        if abs(actual - ident.create_time_epoch) > self.tolerance:
            return False, None, f"PID {ident.pid} 创建时间变化，判定原进程已退出（PID 复用）"
        return True, proc, None

    def _discover_children(self, proc: psutil.Process, errors: list[str]) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        try:
            children = proc.children(recursive=True)
        except psutil.NoSuchProcess:
            return out
        except psutil.AccessDenied:
            errors.append(f"PID {proc.pid} 子进程列表不可读（权限不足）")
            return out
        except OSError as exc:
            errors.append(f"PID {proc.pid} 子进程枚举失败: {exc}")
            return out
        for child in children:
            try:
                out.append(self._info(child, "child"))
            except AtwError as exc:
                errors.append(exc.message)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied:
                errors.append(f"PID {child.pid} 无权访问")
            except OSError:
                continue
        return out

    def snapshot(
        self,
        target: Target,
        known: list[ProcessIdentity],
        root_identity: ProcessIdentity | None = None,
    ) -> ProcessSnapshot:
        errors: list[str] = []
        pid_reused: list[int] = []
        root_ident = root_identity
        if root_ident is None and target.kind == "pid" and target.pid and target.create_time_epoch:
            root_ident = ProcessIdentity(target.pid, float(target.create_time_epoch))
        known_set: dict[ProcessIdentity, None] = {}
        if root_ident is not None:
            known_set[root_ident] = None
        for ident in known:
            known_set[ident] = None

        members: list[ProcInfo] = []
        alive_keys: set[ProcessIdentity] = set()
        # Verify every previously known identity first (children survive parent).
        for ident in list(known_set):
            alive, proc, error = self._check_identity(ident)
            if error:
                errors.append(error)
                if "PID 复用" in error:
                    pid_reused.append(ident.pid)
            if not alive or proc is None:
                continue
            try:
                info = self._info(proc, "root" if ident == root_ident else "child")
            except AtwError as exc:
                errors.append(exc.message)
                continue
            alive_keys.add(ident)
            members.append(info)
            # Only recurse from processes that are still alive.
            for child in self._discover_children(proc, errors):
                if child.identity not in known_set:
                    known_set[child.identity] = None
                if child.identity not in {m.identity for m in members}:
                    alive_keys.add(child.identity)
                    members.append(child)

        # Drop cached Process objects for dead identities to avoid unbounded growth.
        for ident in list(self._procs):
            if ident not in alive_keys:
                self._procs.pop(ident, None)

        cpu_sum = 0.0
        cpu_available = False
        cpu_warm = True
        rss = 0
        rss_available = False
        for info in members:
            ident = info.identity
            try:
                proc = self._procs.get(ident) or self._get_process(ident)
                value = proc.cpu_percent(interval=None)
                if ident in self._primed:
                    cpu_sum += value
                    cpu_available = True
                else:
                    self._primed.add(ident)
                    cpu_warm = False
                mem = proc.memory_info().rss
                rss += int(mem)
                rss_available = True
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        if not cpu_warm:
            # At least one member contributed no usable CPU window this cycle.
            cpu_available = False
        if not members:
            cpu_available = False

        root_alive = root_ident in alive_keys if root_ident is not None else False
        tree_alive = bool(alive_keys)
        blocking_errors = [e for e in errors if "PID 复用" not in e]
        return ProcessSnapshot(
            root_alive=root_alive,
            tree_alive=tree_alive,
            tree_exited=(not tree_alive),
            tree_complete=not blocking_errors,
            cpu_percent=round(cpu_sum, 3) if cpu_available else None,
            cpu_available=cpu_available,
            memory_rss=rss if rss_available else None,
            members=members,
            errors=errors,
            pid_reused=pid_reused,
            unknown_children=sum(1 for e in errors if "无权" in e or "权限" in e),
        )

    def prime(self, target: Target) -> None:
        """Prime CPU counters for a newly accepted target (best effort)."""
        if target.kind == "pid" and target.pid and target.create_time_epoch:
            ident = ProcessIdentity(target.pid, float(target.create_time_epoch))
            try:
                proc = self._get_process(ident)
                proc.cpu_percent(interval=None)
                self._primed.add(ident)
            except (psutil.Error, OSError):
                pass

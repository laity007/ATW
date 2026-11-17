#!/usr/bin/env python3
"""Upgrade or roll back a wheel-backed ATW systemd user service."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

PROJECT = "agent-task-watcher"
SAFE_DOWNGRADES = {(frozenset({1, 2}), frozenset({1}))}
PROBE = """
import importlib.metadata as md
import json
import sys
from pathlib import Path
import atw
import atw.cli.app
from atw.config import load_config
from atw.storage.db import MIGRATIONS
dist = md.distribution('agent-task-watcher')
package = Path(atw.__file__).resolve().parent
if package != Path(dist.locate_file('atw')).resolve():
    raise SystemExit('ATW is not loaded from the installed distribution')
home, config = sys.argv[1:]
cfg, paths = load_config(Path(home), Path(config) if config else None, create_dirs=False)
print(json.dumps(dict(version=atw.__version__, dist_version=dist.version,
    package=str(package), schema=[v for v, _ in MIGRATIONS],
    config=str(paths.config_file.resolve()), db=str(paths.db_file.resolve()))))
"""


class UpgradeError(RuntimeError):
    pass


def run(*command, cwd=None, env=None, timeout=90):
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode:
        # Unit environment, Python exceptions and pip's configuration may contain secrets.
        raise UpgradeError(f"{Path(command[0]).name} {command[1]} exited {result.returncode}")
    return result.stdout


def read_unit(name):
    value = run(
        "systemctl", "--user", "show", name, "--no-pager",
        "--property=ExecStart,WorkingDirectory,MainPID,ActiveState,NeedDaemonReload",
    )
    props = dict(line.split("=", 1) for line in value.splitlines() if "=" in line)
    match = re.fullmatch(
        r"\{ path=(/[^\s;]+) ; argv\[\]=(.*?) ; ignore_errors=no ; .*? \}",
        props.get("ExecStart", ""),
    )
    if not match or props.get("NeedDaemonReload") != "no":
        raise UpgradeError("Expected a loaded user unit with one ATW ExecStart")
    python, argv = match.group(1), match.group(2).split()
    if argv[:4] != [python, "-m", "atw", "--home"] or argv[-2:] != ["service", "run"]:
        raise UpgradeError("Expected ExecStart=<python> -m atw --home <path> [--config <path>] service run")
    config = ""
    if len(argv) == 9 and argv[5] == "--config":
        config = argv[6]
    elif len(argv) != 7:
        raise UpgradeError("ExecStart has unsupported arguments")
    home, cwd = argv[4], props.get("WorkingDirectory") or str(Path.home())
    if not Path(home).is_absolute() or (config and not Path(config).is_absolute()):
        raise UpgradeError("ExecStart must use absolute paths")
    return dict(name=name, python=python, argv=argv, home=home, cwd=cwd, config=config,
                pid=int(props.get("MainPID", "0")), active=props.get("ActiveState"))


def service_env(unit):
    proc = Path("/proc") / str(unit["pid"])
    args = os.fsdecode((proc / "cmdline").read_bytes()).rstrip("\0").split("\0")
    if args != unit["argv"]:
        raise UpgradeError("MainPID command differs from ExecStart")
    # Keep only the variables that affect interpreter/config resolution.
    environment = {
        key: value for item in os.fsdecode((proc / "environ").read_bytes()).split("\0")
        if "=" in item for key, value in (item.split("=", 1),)
        if key.startswith("PYTHON") or key == "ATW_CONFIG"
    }
    return {**clean_env(unit), **environment}


def clean_env(unit):
    return {key: val for key, val in os.environ.items()
            if not key.startswith(("PYTHON", "PIP_")) and key != "ATW_CONFIG"} | {
        "ATW_HOME": unit["home"],
        "PIP_CONFIG_FILE": os.devnull,
    }


def probe(unit, env, config=None, stage=None):
    code = PROBE
    args = []
    if stage:
        # Override the running package without mutating the service interpreter.
        code = "import sys\nsys.path.insert(0, sys.argv.pop(1))\n" + PROBE
        args.append(str(stage))
    result = json.loads(run(
        unit["python"], "-c", code, *args, unit["home"], config or unit["config"],
        cwd=unit["cwd"], env=env,
    ))
    if result["version"] != result["dist_version"]:
        raise UpgradeError("Package version and wheel metadata differ")
    return result


def pip_install(unit, wheel, stage=None):
    options = ["--target", str(stage)] if stage else ["--force-reinstall"]
    run(unit["python"], "-m", "pip", "--disable-pip-version-check", "install",
        "--no-index", "--no-deps", "--no-input", *options, str(wheel),
        cwd="/", env=clean_env(unit))


def stage_wheel(unit, wheel, stage, env, config):
    pip_install(unit, wheel, stage)
    # Only ATW changes. Check candidates against the dependency set already installed.
    run(unit["python"], "-m", "pip", "--disable-pip-version-check", "check", cwd="/",
        env={**clean_env(unit), "PYTHONPATH": str(stage)})
    info = probe(unit, env, config, stage)
    if Path(info["package"]) != stage / "atw":
        raise UpgradeError("Wheel must contain the ATW package")
    return info


def check_migration(unit, stage, env, database, expected):
    trial = stage / "migration.db"
    snapshot(database, trial)
    if not set(state(trial)["schema"]) <= set(expected):
        raise UpgradeError("Target wheel does not support the current database schema")
    run(
        unit["python"], "-c",
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from pathlib import Path; from atw.storage.db import Database; "
        "db = Database(Path(sys.argv[2])); db.connect(); db.close()",
        str(stage), str(trial), cwd=unit["cwd"], env=env,
    )
    if set(state(trial)["schema"]) != set(expected):
        raise UpgradeError("Candidate migration did not produce the expected schema")


def package_hash(package):
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(package)).encode() + b"\0")
            digest.update(file_hash(path).encode())
    return digest.hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db_reader(path):
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return closing(conn)


def snapshot(source, destination):
    with db_reader(source) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise UpgradeError("SQLite backup failed quick_check")


def state(path):
    with db_reader(path) as conn:
        session = conn.execute(
            "SELECT session_id, pid, service_version, ended_at FROM service_sessions "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        tasks = [row[0] for row in conn.execute("SELECT task_id FROM tasks WHERE state='MONITORING'")]
        mark = conn.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
        pending = [row[0] for row in conn.execute(
            "SELECT dedupe_key FROM notification_outbox WHERE status='pending'"
        )]
        schema = [row[0] for row in conn.execute("SELECT version FROM schema_migrations")]
    return dict(session=dict(session) if session else {}, tasks=tasks, mark=mark,
                pending=pending, schema=schema)


def verify(unit, path, before, version, timeout, expected_schema):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running = read_unit(unit["name"])
        if running["active"] == "failed":
            raise UpgradeError("systemd reports a failed ATW start")
        with db_reader(path) as conn:
            session = conn.execute(
                "SELECT session_id, pid, service_version, ended_at FROM service_sessions "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            sampled = {row[0] for row in conn.execute(
                "SELECT DISTINCT task_id FROM observations WHERE id > ?", (before["mark"],)
            )}
            outbox = {row[0] for row in conn.execute("SELECT dedupe_key FROM notification_outbox")}
            schema = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if (running["active"] == "active" and running["pid"] and session and
                session["pid"] == running["pid"] and not session["ended_at"] and
                session["session_id"] != before["session"].get("session_id") and
                session["service_version"] == version and set(expected_schema) <= schema and
                set(before["tasks"]) <= sampled and set(before["pending"]) <= outbox):
            service_env(running)
            print(f"Verified ATW {version}, PID {running['pid']}, "
                  f"{len(before['tasks'])} sampled task(s), {len(before['pending'])} outbox item(s)")
            return
        time.sleep(0.5)
    raise UpgradeError("Timed out verifying the running version, new samples and preserved outbox")


def systemctl(unit, action):
    run("systemctl", "--user", action, unit["name"])


def install_and_start(unit, wheel, record, artifact, before, timeout):
    if file_hash(wheel) != artifact["sha256"]:
        raise UpgradeError("Wheel failed hash verification")
    pip_install(unit, wheel)
    cfg = probe(unit, clean_env(unit), record["config"])
    if cfg["db"] != record["db"] or package_hash(Path(cfg["package"])) != artifact["package_sha256"]:
        raise UpgradeError("Installed package or database path differs from the upgrade record")
    systemctl(unit, "start")
    verify(unit, record["db"], before, artifact["version"], timeout, artifact["schema"])


def rollback(unit, directory, record, timeout, restore, required_pending=()):
    old = record["previous"]
    old_wheel = directory / "previous" / old["file"]
    backup = directory / "before.db"
    if file_hash(old_wheel) != old["sha256"] or file_hash(backup) != record["backup_sha256"]:
        raise UpgradeError("Rollback wheel or database backup failed hash verification")
    systemctl(unit, "stop")
    before = state(record["db"])
    live_schema, old_schema = frozenset(before["schema"]), frozenset(old["schema"])
    if restore:
        fd, archived = tempfile.mkstemp(prefix="pre-restore-", suffix=".db", dir=directory)
        os.close(fd)
        snapshot(record["db"], archived)
        print(f"Saved database before restore: {archived}", flush=True)
        snapshot(backup, record["db"])
        before = state(record["db"])
    elif live_schema != old_schema and (live_schema, old_schema) not in SAFE_DOWNGRADES:
        raise UpgradeError("Schema compatibility is unknown. Service is stopped. "
                           "Use --restore-db only after reviewing post-upgrade writes.")
    before["pending"] = set(before["pending"]) | set(required_pending)
    install_and_start(unit, old_wheel, record, old, before, timeout)


def upgrade(unit, args, current, env):
    directory = Path(tempfile.mkdtemp(prefix="upgrade-", dir=Path(current["db"]).parent))
    print(f"Upgrade record: {directory}", flush=True)
    record = {key: unit[key] for key in ("name", "python", "argv", "cwd")}
    record.update(db=current["db"], config=current["config"])
    for label, source in (("previous", args.previous_wheel), ("target", args.wheel)):
        folder = directory / label
        folder.mkdir()
        wheel = folder / source.name
        shutil.copyfile(source, wheel)
        with tempfile.TemporaryDirectory(prefix="wheel-check-", dir=directory) as temp:
            stage = Path(temp)
            info = stage_wheel(unit, wheel, stage, env, current["config"])
            pkg_hash = package_hash(Path(info["package"]))
            if label == "target":
                check_migration(unit, stage, env, current["db"], info["schema"])
        if info["db"] != current["db"]:
            raise UpgradeError("Candidate config resolves to a different database")
        if label == "previous" and (
            pkg_hash != package_hash(Path(current["package"])) or
            info["version"] != current["version"] or info["schema"] != current["schema"]
        ):
            raise UpgradeError("Previous wheel does not match the running installation")
        record[label] = dict(file=wheel.name, version=info["version"], schema=info["schema"],
                             sha256=file_hash(wheel), package_sha256=pkg_hash)
    backup = directory / "before.db"
    running = read_unit(unit["name"])
    if any(running[key] != unit[key] for key in ("argv", "cwd", "pid", "active")):
        raise UpgradeError("Unit changed during preflight")
    systemctl(unit, "stop")
    try:
        snapshot(current["db"], backup)
        record["backup_sha256"] = file_hash(backup)
        before = state(backup)
        if not set(before["schema"]) <= set(record["target"]["schema"]):
            raise UpgradeError("Database schema changed during preflight")
        record["baseline"] = before
        journal = directory / "record.json.tmp"
        journal.write_text(json.dumps(record, indent=2) + "\n")
        journal.replace(directory / "record.json")
    except (Exception, KeyboardInterrupt):
        systemctl(unit, "start")
        raise
    try:
        install_and_start(unit, wheel, record, record["target"], before, args.timeout)
    except (Exception, KeyboardInterrupt) as error:
        reason = "command timed out" if isinstance(error, subprocess.TimeoutExpired) else str(error)
        print(f"Upgrade failed: {reason}. Attempting rollback with the live database.", flush=True)
        rollback(unit, directory, record, args.timeout, restore=False,
                 required_pending=before["pending"])
        raise UpgradeError("Upgrade failed. Previous wheel restarted and verified.") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    install = sub.add_parser("upgrade")
    install.add_argument("--unit", default="atw.service")
    install.add_argument("--wheel", type=Path, required=True)
    install.add_argument("--previous-wheel", type=Path, required=True)
    undo = sub.add_parser("rollback")
    undo.add_argument("--record", type=Path, required=True)
    undo.add_argument("--restore-db", action="store_true")
    for command in (install, undo):
        command.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    if os.getuid() == 0 or args.timeout <= 0:
        raise UpgradeError("Run as the service user and use a positive verification timeout")
    os.umask(0o077)
    record = None
    if args.action == "rollback":
        args.record = args.record.resolve(strict=True)
        record = json.loads((args.record / "record.json").read_text())
        args.unit = record["name"]
    if not re.fullmatch(r"[\w.@-]+\.service", args.unit) or args.unit.startswith("-"):
        raise UpgradeError("Expected a systemd service unit name")
    unit = read_unit(args.unit)
    if record:
        if any(unit[key] != record[key] for key in ("python", "argv", "cwd")):
            raise UpgradeError("Unit differs from the upgrade record")
        db = record["db"]
    else:
        if unit["active"] != "active" or not unit["pid"]:
            raise UpgradeError("ATW must be active before upgrade")
        env = service_env(unit)
        current = probe(unit, env)
        db = current["db"]
        session = state(db)["session"]
        if session.get("pid") != unit["pid"] or session.get("service_version") != current["version"]:
            raise UpgradeError("Database does not belong to the running ATW process")
    with Path(db).with_name("atw-upgrade.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if record:
            if unit["active"] == "active" and unit["pid"]:
                actual = probe(unit, service_env(unit))
                if actual["db"] != record["db"] or actual["config"] != record["config"]:
                    raise UpgradeError("Running database or configuration differs from the upgrade record")
            rollback(unit, args.record, record, args.timeout, args.restore_db)
        else:
            upgrade(unit, args, current, env)


if __name__ == "__main__":
    try:
        main()
    except (UpgradeError, OSError, sqlite3.Error, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        # TimeoutExpired renders command arguments. Emit only the operation's safe status.
        print(f"ATW upgrade: {type(exc).__name__}" if isinstance(exc, subprocess.TimeoutExpired)
              else f"ATW upgrade: {exc}", file=sys.stderr)
        sys.exit(1)

from __future__ import annotations

from datetime import timedelta

from atw.models.task import Target
from atw.storage.db import Database
from atw.storage.records import TaskRecord
from atw.storage.repository import Repository
from atw.utils.timeutil import utc_now


def _record(task_id: str = "t1") -> TaskRecord:

    from atw.models.task import Evidence, NotificationSettings, RulesSpec

    return TaskRecord(
        task_id=task_id,
        name="n",
        project=None,
        description=None,
        run_started_at=utc_now(),
        state="MONITORING",
        revision=1,
        target=Target(kind="pid", pid=1, create_time_epoch=1.0),
        evidence=Evidence(run_directory="/tmp"),
        rules=RulesSpec(),
        notifications=NotificationSettings(),
        runtime={},
        created_at=utc_now(),
        updated_at=utc_now(),
        accepted_at=utc_now(),
        session_id="s",
        boot_id="b",
    )


def test_wal_and_migrations(tmp_path) -> None:
    db = Database(tmp_path / "atw.db")
    db.connect()
    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"
    versions = [row["version"] for row in db.query_all("SELECT version FROM schema_migrations")]
    assert versions == [1]
    db.close()
    db2 = Database(tmp_path / "atw.db")
    db2.connect()
    versions2 = [row["version"] for row in db2.query_all("SELECT version FROM schema_migrations")]
    assert versions2 == [1]
    db2.close()


def test_transaction_rollback(tmp_path) -> None:
    db = Database(tmp_path / "atw.db")
    db.connect()
    repo = Repository(db)
    try:
        with db.transaction() as conn:
            repo.insert_task(conn, _record("rolled-back"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert repo.get_task("rolled-back") is None
    db.close()


def test_terminal_state_is_immutable(tmp_path) -> None:
    db = Database(tmp_path / "atw.db")
    db.connect()
    repo = Repository(db)
    record = _record()
    with db.transaction() as conn:
        repo.insert_task(conn, record)
    now = utc_now()
    with db.transaction() as conn:
        assert repo.finalize(
            conn, task_id="t1", state="SUCCESS", reason="ok", evidence="e", runtime={}, now=now
        )
    with db.transaction() as conn:
        assert not repo.finalize(
            conn, task_id="t1", state="FAILED", reason="bad", evidence="e", runtime={}, now=now
        )
    assert repo.get_task("t1").state == "SUCCESS"
    db.close()


def test_retention_purges_old_rows(tmp_path) -> None:
    db = Database(tmp_path / "atw.db")
    db.connect()
    repo = Repository(db)
    record = _record()
    with db.transaction() as conn:
        repo.insert_task(conn, record)
        repo.add_observation(
            conn,
            task_id="t1",
            ts=utc_now() - timedelta(days=100),
            cpu_percent=1.0,
            memory_rss=1,
            gpu=None,
            process_alive=True,
            details={},
        )
    removed = repo.purge_retention(30)
    assert removed["observations"] == 1
    db.close()


def test_counts_and_history(tmp_path) -> None:
    db = Database(tmp_path / "atw.db")
    db.connect()
    repo = Repository(db)
    with db.transaction() as conn:
        repo.insert_task(conn, _record("a"))
        repo.insert_task(conn, _record("b"))
    assert repo.counts()["tasks"] == 2
    assert repo.counts()["monitoring"] == 2
    db.close()

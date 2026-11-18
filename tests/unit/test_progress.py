from datetime import timedelta

import pytest

from atw.models.progress import CsvProgress, ItemReport, QueueItem, QueueProgress
from atw.monitoring.progress import sample_progress, write_report
from atw.utils.timeutil import utc_now


@pytest.mark.parametrize(
    ("items", "expected"), [(10, 73.1), (2, 62.5)]
)
def test_queue_weights_attempts_and_missing_data(tmp_path, items, expected):
    start = utc_now() - timedelta(seconds=5)
    entries = [
        QueueItem(
            id=f"S{i}", total=200, unit="epoch",
            weight=3 if items == 2 and i == 0 else 1, required=i < items - 1,
            report=str(tmp_path / f"S{i}.json"),
        )
        for i in range(items)
    ]
    spec = QueueProgress(kind="queue", items=entries)

    def update(i, state, count, attempt=1, acceptance=None):
        report = ItemReport(
            task_id="batch", item_id=entries[i].id, attempt=attempt, state=state,
            completed=count, updated_at=utc_now(), acceptance=acceptance,
        )
        write_report(report, entries[i], [tmp_path], str(tmp_path))

    def sample(previous=None):
        return sample_progress(
            spec, task_id="batch", run_dir=str(tmp_path), roots=[tmp_path],
            started_at=start, now=utc_now(), previous=previous,
        )

    assert sample()["percent"] is None
    if items == 10:
        for i in range(7):
            update(i, "completed", 200, acceptance="accepted")
        update(7, "running", 62)
        update(8, "queued", 0)
        update(9, "queued", 0)
        active = 7
    else:
        update(0, "running", 100)
        update(1, "completed", 200, acceptance="accepted")
        active = 0
    first = sample()
    assert first["percent"] == pytest.approx(expected)
    assert first["accepted"] == (7 if items == 10 else 1)
    update(active, "failed", first["items"][active]["completed"])
    update(active, "running", 2, attempt=2)
    with pytest.raises(ValueError, match="旧 attempt"):
        update(active, "running", 120)
    retry = sample(first)
    assert retry["items"][active]["attempt"] == 2
    assert retry["percent"] < expected
    (tmp_path / f"S{active}.json").write_text("{", encoding="utf-8")
    snapshot = sample(retry)
    assert snapshot["status"] == "snapshot"
    assert snapshot["percent"] == retry["percent"]


def test_csv_ignores_partial_row_and_accepts_zero_based_counter(tmp_path):
    source = tmp_path / "results.csv"
    source.write_text(" epoch ,loss\n61,0.5\n62,", encoding="utf-8")
    spec = CsvProgress(kind="csv", path=str(source), column="epoch", counter_base=0, total=200, unit="epoch")
    sample = sample_progress(
        spec, task_id="csv", run_dir=str(tmp_path), roots=[tmp_path],
        started_at=utc_now() - timedelta(seconds=5), now=utc_now(),
    )
    assert sample["percent"] == 31

import datetime as dt
import threading
import time

from publisher.connectors.mock import MockConnector
from publisher.service import Publisher
from tests.publisher.conftest import connect_mock


class TracingMock(MockConnector):
    """Mock connector that records when each upload runs, to check concurrency."""

    def __init__(self):
        super().__init__()
        self.spans = []
        self.lock = threading.Lock()

    def upload(self, account, video_path, post, ctx):
        t0 = time.monotonic()
        try:
            return super().upload(account, video_path, post, ctx)
        finally:
            with self.lock:
                self.spans.append((account.id, t0, time.monotonic()))


def make_pub(data_dir, connector=None):
    c = connector or TracingMock()
    pub = Publisher(data_dir=data_dir, connectors={"mock": c}, url_provider=None, ffmpeg="")
    pub.retry_base_sec = 0.05
    return pub, c


def overlaps(spans, same_account: bool):
    n = 0
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if (a[0] == b[0]) == same_account and a[1] < b[2] and b[1] < a[2]:
                n += 1
    return n


def test_two_clips_to_three_accounts_in_parallel(data_dir, clip_source):
    pub, c = make_pub(data_dir)
    ids = [connect_mock(pub.accounts, h)["id"] for h in ("main", "alt", "brand")]
    job = pub.create_job(str(clip_source), account_ids=ids)
    assert len(job["targets"]) == 6
    pub.start()
    try:
        assert pub.wait_idle(20)
    finally:
        pub.stop()
    job = pub.jobs.get(job["id"])
    assert job["status"] == "published"
    assert all(t["url"].startswith("https://mock.local/") for t in job["targets"])
    assert overlaps(c.spans, same_account=False) > 0      # different accounts upload at the same time
    assert overlaps(c.spans, same_account=True) == 0      # one upload per account at a time
    assert len(list((data_dir / "mock_posts").rglob("*.json"))) == 6


def test_retry_then_success_and_permanent_failure(data_dir, clip_source):
    pub, _ = make_pub(data_dir)
    aid = connect_mock(pub.accounts, "x")["id"]
    job = pub.create_job(str(clip_source), targets=[
        {"clip_id": 1, "account_id": aid, "post": {"options": {"mock_fail_times": 2}}},
        {"clip_id": 2, "account_id": aid, "post": {"options": {"mock_fail": "permanent"}}}])
    pub.start()
    try:
        assert pub.wait_idle(20)
    finally:
        pub.stop()
    t1, t2 = pub.jobs.get(job["id"])["targets"]
    assert t1["status"] == "published" and t1["attempts"] == 3
    assert t2["status"] == "failed" and t2["attempts"] == 1 and "permanent" in t2["error"]
    assert pub.jobs.get(job["id"])["status"] == "partial"
    pub.retry(job["id"])
    assert pub.jobs.get(job["id"])["targets"][1]["status"] == "queued"


def test_auth_failure_marks_account_for_reconnect(data_dir, clip_source):
    pub, _ = make_pub(data_dir)
    aid = connect_mock(pub.accounts, "y")["id"]
    job = pub.create_job(str(clip_source), targets=[
        {"clip_id": 1, "account_id": aid, "post": {"options": {"mock_auth_fail": True}}}])
    pub.start()
    try:
        assert pub.wait_idle(10)
    finally:
        pub.stop()
    t = pub.jobs.get(job["id"])["targets"][0]
    assert t["status"] == "failed" and t["error"].startswith("Reconnect")
    assert pub.accounts.get_public(aid)["status"] == "reconnect_needed"


def test_scheduled_job_waits_until_due(data_dir, clip_source):
    pub, _ = make_pub(data_dir)
    aid = connect_mock(pub.accounts, "z")["id"]
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1.0)).isoformat()
    later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    job = pub.create_job(str(clip_source), account_ids=[aid], clip_ids=[1], scheduled_at=soon)
    far = pub.create_job(str(clip_source), account_ids=[aid], clip_ids=[2], scheduled_at=later)
    assert job["status"] == "scheduled"
    pub.start()
    try:
        time.sleep(0.4)
        assert pub.jobs.get(job["id"])["targets"][0]["status"] == "scheduled"
        deadline = time.time() + 10
        while pub.jobs.get(job["id"])["status"] != "published" and time.time() < deadline:
            time.sleep(0.05)
    finally:
        pub.stop()
    assert pub.jobs.get(job["id"])["status"] == "published"
    assert pub.jobs.get(far["id"])["status"] == "scheduled"


def test_cancel_and_restart_resumes(data_dir, clip_source):
    pub, _ = make_pub(data_dir)
    aid = connect_mock(pub.accounts, "r")["id"]
    job = pub.create_job(str(clip_source), account_ids=[aid])
    # Simulate a crash mid-upload: target left "uploading" with resume state on disk.
    pub.jobs.update_target(job["id"], "t1", status="uploading", state={"offset": 0.5}, attempts=1)
    other = pub.create_job(str(clip_source), account_ids=[aid])
    pub2, _ = make_pub(data_dir)
    assert pub2.jobs.get(job["id"])["targets"][0]["status"] == "queued"
    assert pub2.jobs.get(job["id"])["targets"][0]["state"] == {"offset": 0.5}
    pub2.cancel(other["id"])
    assert pub2.jobs.get(other["id"])["status"] == "cancelled"
    pub2.start()
    try:
        assert pub2.wait_idle(10)
    finally:
        pub2.stop()
    done = pub2.jobs.get(job["id"])
    assert done["status"] == "published" and done["targets"][0]["state"]["offset"] == 1.0
    assert pub2.jobs.get(other["id"])["status"] == "cancelled"

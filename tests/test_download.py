import pytest

from clipper import download
from clipper.download import download_clip, find_covering_raw


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(download, "is_valid_video", lambda p, f, min_sec=1.0: p.is_file())

    def boom(*a, **k):
        raise AssertionError("downloaded")

    monkeypatch.setattr(download.subprocess, "run", boom)


def test_trimmed_clip_reuses_covering_raw(tmp_path, cfg):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "clip_01_99500_130500.mp4").write_bytes(b"x")
    r = download_clip("u", {"id": 1, "start_sec": 104.0, "end_sec": 120.0}, raw, cfg, "ffmpeg", 600)
    assert r == {"path": str(raw / "clip_01_99500_130500.mp4"), "offset_sec": 99.5, "end_sec": 130.5}


def test_other_clip_or_uncovered_range_is_not_reused(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "clip_02_99500_130500.mp4").write_bytes(b"x")
    assert find_covering_raw(raw, {"id": 1, "start_sec": 104.0, "end_sec": 120.0}, "ffmpeg") is None
    (raw / "clip_01_99500_130500.mp4").write_bytes(b"x")
    assert find_covering_raw(raw, {"id": 1, "start_sec": 99.5, "end_sec": 120.0}, "ffmpeg") is None  # needs 99.4


def test_uncovered_range_still_downloads(tmp_path, cfg):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "clip_01_99500_130500.mp4").write_bytes(b"x")
    with pytest.raises(AssertionError, match="downloaded"):
        download_clip("u", {"id": 1, "start_sec": 104.0, "end_sec": 140.0}, raw, cfg, "ffmpeg", 600)

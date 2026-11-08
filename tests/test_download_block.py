import subprocess
import threading

from clipper import download as dl

CFG = {"download": {"padding_sec": 0.5, "max_height": 1080, "retries": 2, "parallel": 3}}
BOT = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies"


def fake_yt_dlp(monkeypatch, stderr):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(dl.subprocess, "run", run)
    monkeypatch.setattr(dl.time, "sleep", lambda seconds: None)
    return calls


def test_bot_checks_are_recognised_and_other_errors_are_not():
    assert dl.is_bot_block(BOT)
    assert not dl.is_bot_block("ERROR: Video unavailable")
    assert not dl.is_bot_block("")


def test_a_bot_check_is_not_retried(monkeypatch, tmp_path):
    calls = fake_yt_dlp(monkeypatch, BOT)
    clip = {"id": 1, "start_sec": 10.0, "end_sec": 40.0}
    out = dl.download_clip("https://youtu.be/abc", clip, tmp_path, CFG, "ffmpeg", 600.0)
    assert len(calls) == 1  # one request, not retries 1 and 2
    assert out.get("blocked") is True and out["error"] == dl.BOT_BLOCK_HELP


def test_once_blocked_the_other_clips_of_the_job_do_not_ask_youtube_again(monkeypatch, tmp_path):
    calls = fake_yt_dlp(monkeypatch, BOT)
    blocked = threading.Event()
    clip = {"id": 2, "start_sec": 10.0, "end_sec": 40.0}
    dl.download_clip("https://youtu.be/abc", clip, tmp_path, CFG, "ffmpeg", 600.0, blocked)
    other = dl.download_clip("https://youtu.be/abc", {**clip, "id": 3}, tmp_path, CFG, "ffmpeg", 600.0, blocked)
    assert len(calls) == 1 and other.get("blocked") is True

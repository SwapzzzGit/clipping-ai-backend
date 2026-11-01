import shutil
import subprocess

import pytest

from publisher.connectors import InstagramConnector, MockConnector, TikTokConnector, YouTubeConnector
from publisher.models import Limits, PostSpec
from publisher.variants import build_variants, fit_text, load_clips, prepare_video

CONNECTORS = {"youtube": YouTubeConnector(), "tiktok": TikTokConnector(), "instagram": InstagramConnector(),
              "mock": MockConnector()}


def accounts(*platforms):
    return [{"id": f"{p}-1", "platform": p, "display_name": p.title(), "options": {"hashtags": ["podcast"]}}
            for p in platforms]


def test_one_variant_per_clip_and_account(clip_source):
    vs = build_variants(str(clip_source), accounts("youtube", "tiktok", "instagram"), CONNECTORS)
    assert len(vs) == 6
    by = {(v["clip_id"], v["platform"]): v["post"] for v in vs}
    yt, tt, ig = by[(1, "youtube")], by[(1, "tiktok")], by[(1, "instagram")]
    assert yt["hashtags"][0] == "#Shorts" and "#Podcast" in yt["hashtags"]
    assert "https://youtu.be/vid123" in yt["caption"]
    assert "http" not in ig["caption"]                       # links are not clickable on Instagram
    assert tt["privacy"] == "private"                        # unaudited TikTok app
    assert len(yt["title"]) <= 100


def test_fit_text_enforces_limits():
    lim = Limits(max_duration_sec=60, title_max=10, caption_max=50, hashtag_max=2, required_hashtags=["#Shorts"])
    p = fit_text(PostSpec(title="A very long title indeed", caption="word " * 40, hashtags=["#a", "#b", "#c"]), lim)
    assert p.title == "A very lon" and p.hashtags == ["#Shorts", "#a"]
    assert len(p.caption_with_tags()) <= 50


def test_llm_copy_through_mock_provider(clip_source):
    from clipper.llm import LLM

    llm = LLM({"provider": "mock"}, overrides={"provider": "mock"})
    vs = build_variants(str(clip_source), accounts("tiktok"), CONNECTORS, clip_ids=[2], llm=llm)
    assert len(vs) == 1 and vs[0]["post"]["caption"]


def test_clip_filter_and_issues(clip_source):
    _, _, clips = load_clips(str(clip_source), [2])
    assert [c["id"] for c in clips] == [2]
    lim = {"mock": MockConnector()}
    lim["mock"].limits = Limits(max_duration_sec=40)
    vs = build_variants(str(clip_source), accounts("mock"), lim)
    assert vs[0]["issues"] == [] and "trimmed" in vs[1]["issues"][0]


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@pytest.mark.skipif(not _ffmpeg(), reason="ffmpeg not available")
def test_prepare_video_trims_long_clip(tmp_path):
    ff = _ffmpeg()
    src = tmp_path / "long.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=108x192:d=4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    lim = Limits(max_duration_sec=2)
    out = prepare_video(src, 4.0, lim, tmp_path / "fit", ff)
    assert out != src and out.is_file()
    probe = subprocess.run([ff, "-hide_banner", "-i", str(out)], capture_output=True, text=True).stderr
    assert "Duration: 00:00:01.9" in probe or "Duration: 00:00:02.0" in probe
    assert prepare_video(src, 1.5, Limits(max_duration_sec=60), tmp_path / "fit", ff) == src
